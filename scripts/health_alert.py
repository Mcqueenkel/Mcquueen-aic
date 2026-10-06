"""Alert when the AIC farmer is WEDGED. Modelled on Icarus' scripts/health_alert.py.

Same design rules as that one, and for the same reasons:

* **Alert on the state, never on the transition into it.** There is no "changed"
  check and no last-alerted file: every run that finds a fault reports it, and
  the timer interval IS the re-alert interval. A transition-gated alert fires for
  nobody - by the time anyone looks, the fault is already the steady state.
* **`systemctl is-active` is not the question.** The failure worth catching leaves
  the unit ACTIVE while the process does nothing.
* **Read what the bot actually did, from outside it.** Nothing here touches AIC;
  it reads the log AIC already writes and asks systemd a read-only question.

AIC's own checks differ from Icarus': there is no web UI to poll and no
``api_library``. What it has is one log, and the useful property that **it
announces every long sleep before taking it** - a rotation interval, an idle
career's next poll, a fan-cap wait. So a stale log whose last line is an
announced wait is healthy; a stale log that stopped anywhere else is a wedge.

    python scripts/health_alert.py --dry-run     # print the verdict, post nothing
    python scripts/health_alert.py               # post to Discord if wedged

Webhook, first one set wins: --webhook, then $AIC_ALERT_WEBHOOK (or $UMA_ALERT_WEBHOOK,
so one /etc/umabot.env serves both bots).

Exit 0 = healthy. Exit 1 = wedged, so `systemctl is-failed aic-health` carries the
verdict even when the webhook itself is what is broken.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

#: Lines AIC prints immediately before a deliberate sleep. A log that stops on one
#: of these is waiting on purpose, not wedged. Keep in step with the log calls in
#: flow.wait_for_finish, rotate.run and cli's fan-cap branch.
ANNOUNCED_WAIT = re.compile(
    r"(remaining; next check in"        # flow.wait_for_finish, before each poll
    r"|Next visit in"                   # rotate.run, before sleeping to a due time
    r"|Sleeping until the reset"        # cli, fan cap on aic run --loop
    r"|fan cap reached"                 # rotate, an account parked until the reset
    r"|Next attempt in)",               # cli, waiting on a refusal it cannot fix
    re.IGNORECASE)

#: The longest legitimate announced sleep: the fan cap waits out a whole game-day.
#: Past this, even an announced wait is a wedge.
ANNOUNCED_WAIT_CEILING = 25 * 3600

#: Lines that prove a cycle actually got somewhere. Used to decide whether an
#: ERROR in the log is stale history or the state the farmer is stuck in.
PROGRESS = re.compile(
    r"(=== cycle |=== visit |Banked .* fans|New career finishes in"
    r"|Idle career finishes in|career started"
    # An announced wait IS progress: the loop got past whatever the error was
    # and reached a scheduled decision. Without this, a farmer correctly parked
    # on a refusal it has already handled (e.g. 2509 -> "Next attempt in 1h")
    # is reported wedged on EVERY check, forever - the exact alert fatigue the
    # module docstring is about. MEASURED 2026-09-01 against the live unit.
    r"|Next attempt in|Next visit in|Sleeping until the reset"
    r")", re.IGNORECASE)

#: How much of the tail to read. The log is unrotated and grows for as long as the
#: unit runs, so this is never a whole-file read.
TAIL_BYTES = 256 * 1024

#: Bigger window for the productivity scan, which needs hours of history rather
#: than the last few lines. MEASURED: ~740 KB per 11 h of a busy (failing) log.
PRODUCTIVITY_BYTES = 4 * 1024 * 1024

#: A cycle ending in one of these did not start a career, and that is FINE - the
#: farm said why and parked itself. Non-progress WITH a reason is not a fault.
EXPLAINED = re.compile(r"(refused with 2509|fan cap reached|Next attempt in)",
                       re.IGNORECASE)

#: A cycle ending in one of these did not start a career and did NOT explain
#: itself. These are what the productivity check actually counts.
UNEXPLAINED = re.compile(r"(Visit to .* failed|career start was refused)",
                         re.IGNORECASE)

#: A career actually beginning - the whole point of the farm.
STARTED = re.compile(r"(New career finishes in|Independent training started"
                     r"|career started)", re.IGNORECASE)

#: How many unexplained failures inside the window make it a fault. One or two
#: are a blip the backoff already absorbs; a wall of them is a wedge.
MIN_UNEXPLAINED = 5

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# What the server said, and what to DO about it. An alert that says "709" and stops
# has moved the 3am lookup from the log to the wiki without shortening it.
RESULT_CODE_HELP = {
    "709": "viewer_id/auth mismatch - the carried identity is not valid for a live account on "
           "this Steam client. AIC REFUSES to adopt the server's viewer_id here, deliberately. "
           "Fix with an in-game Data Transfer onto this Steam client, or a fresh capture.",
    "1055": "viewer_id mismatch - usually after another process switched the in-game account. "
            "Check nothing else is holding the Steam slot (umabot.service).",
    "391": "the Steam session ticket is dead. If it repeats, the stored steam_refresh_token in "
           "auth_config.json is being rejected and nothing can re-mint unattended - aic/steam.py "
           "can only ask for a Guard code on a tty. Run `./aic.sh login` by hand once to store a "
           "fresh one. (UMA_STEAM_SHARED_SECRET is umabot's variable and has no effect on AIC.)",
    "394": "the Steam session ticket was invalidated, usually because the game was opened "
           "elsewhere on this Steam account.",
    "204": "stale APP-VER. Push the live Windows client's version: "
           "python scripts/push_app_ver.py --host <vps> --launch (in the Icarus checkout).",
    "501": "empty APP-VER. Set UMA_APP_VER before blaming the account.",
    "214": "resource-version mismatch. It self-heals, so seeing it LAST means the retry never landed.",
    "205": "the start was refused. Usually the race plan or the succession rank point - check the "
           "lines just above it. Scenario 4 (MANT) is not startable in idle mode.",
    "102": "the succession rank point the server expected did not match. The offset sweep "
           "normally learns it; a persistent 102 means the cached offset is wrong for this pair.",
}


def _age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.0f} min"


def tail(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - limit))
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    if len(raw) == limit:
        text = text.split("\n", 1)[-1]          # drop the half line at the cut
    return [ln for ln in text.splitlines() if ln.strip()]


def _systemctl(verb: str, unit: str) -> str:
    """`systemctl is-active`/`is-enabled` exit non-zero for most states, so read
    stdout, not the return code."""
    try:
        p = subprocess.run(["systemctl", verb, unit],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or "").strip()


def check_unit(unit: str):
    """Is the farmer running - and if not, was that somebody's decision?

    A unit that is DISABLED, MASKED or not installed is a configuration the
    operator chose, not a fault, and alerting on a chosen configuration is how
    people learn to ignore alerts (the same reasoning that makes UMA_NO_API_LIBRARY
    not a fault in Icarus' version). A crash never disables a unit - it leaves it
    enabled and failed - so nothing real is lost by staying quiet here.
    """
    enabled = _systemctl("is-enabled", unit)
    state = _systemctl("is-active", unit) or "unknown"
    if enabled in ("disabled", "masked", "") or enabled.startswith("not-found"):
        return f"{state} ({enabled or 'not installed'}, deliberately off)", None
    if state != "active":
        return state, f"the unit is **{state}**, not active. `journalctl -u {unit} -n 50`"
    return state, None


def recent_starts(path: Path, hours: float) -> int:
    """How many careers started in the last ``hours``. Cheap, and decisive.

    A farm that is starting careers is working, whatever the last line says.
    """
    if hours <= 0 or not path.exists():
        return 0
    cutoff = datetime.now() - timedelta(hours=hours)
    n = 0
    for ln in tail(path, PRODUCTIVITY_BYTES):
        if not STARTED.search(ln):
            continue
        m = _TS.match(ln)
        if not m:
            continue
        try:
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when >= cutoff:
            n += 1
    return n


def check_log(path: Path, stale_seconds: float, starts: int = 0):
    """Is the log still moving - or stopped somewhere it did not say it would stop?

    Returns (facts, problems).
    """
    facts, problems = {}, []
    if not path.exists():
        return {"log": f"{path} (absent)"}, [
            f"no log at {path}. AIC has not run since this state dir was created, or --log "
            f"points at the wrong profile."]

    idle = time.time() - path.stat().st_mtime
    facts["log"] = str(path)
    facts["last written"] = _age(idle) + " ago"

    lines = tail(path)
    last = lines[-1] if lines else ""
    facts["last line"] = (last[-140:] if len(last) > 140 else last) or "(empty)"

    if idle >= stale_seconds:
        if not ANNOUNCED_WAIT.search(last):
            problems.append(
                f"the log has not been written for {_age(idle)} and the last thing it said was "
                f"not a wait it announced:\n  `{last[-200:]}`\nThe process is up but has stopped "
                f"doing anything.")
        elif idle >= ANNOUNCED_WAIT_CEILING:
            problems.append(
                f"the log announced a wait {_age(idle)} ago and has said nothing since. Even the "
                f"longest legitimate wait (the fan cap, one game-day) is over by now.")

    # An error is only a fault if nothing has got anywhere since. A single failed
    # visit that the rotation recovered from is not worth waking anyone.
    last_error = last_progress = None
    for i, line in enumerate(lines):
        if " ERROR " in line or "CRITICAL" in line:
            last_error = (i, line)
        if PROGRESS.search(line):
            last_progress = i
    if last_error and (last_progress is None or last_progress < last_error[0]) \
            and not starts:
        # `starts` is the veto. This rule is a weak proxy for "stuck", and the
        # 30-minute check lands mid-login often enough that the newest line is a
        # tolerated error with the next progress line milliseconds away. It once
        # reported "careers started 70 in 3h" and "WEDGED" in the same message.
        line = last_error[1]
        facts["last error"] = line[-140:]
        codes = sorted({c for c in RESULT_CODE_HELP if re.search(rf"\b{c}\b", line)})
        detail = "\n".join(f"  {c}: {RESULT_CODE_HELP[c]}" for c in codes)
        problems.append(
            f"the last thing in the log is an error and nothing has progressed since:\n"
            f"  `{line[-250:]}`" + (f"\n{detail}" if detail else ""))
    return facts, problems


def check_productivity(path: Path, hours: float):
    """Is the farm actually STARTING careers - or only looking busy?

    The failure this exists for: 2026-09-02. The unit was active and the log grew
    all night (a visit every 60 s), and not one career started in 11 hours. Every
    other check passed, because "the log is moving" is not "the farm is working" -
    the lines being written were failures.

    The judgement is NOT "zero careers = broken". Zero is entirely correct when
    the server is refusing starts with 2509: the farm parks itself and waits.
    Alerting on that would be crying wolf at a farm doing the right thing. So
    this counts only non-progress that arrived with NO explanation.
    """
    facts, problems = {}, []
    if hours <= 0 or not path.exists():
        return facts, problems

    lines = tail(path, PRODUCTIVITY_BYTES)
    cutoff = datetime.now() - timedelta(hours=hours)
    window, oldest = [], None
    for line in lines:
        m = _TS.match(line)
        if not m:
            continue
        try:
            when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if oldest is None:
            oldest = when
        if when >= cutoff:
            window.append(line)

    # Only judge a window the log actually covers. Right after a restart or a log
    # rotation there is not enough history, and "no careers yet" means nothing.
    if oldest is None or oldest > cutoff:
        facts["productivity"] = f"not enough history yet (< {hours:g}h)"
        return facts, problems

    started = sum(1 for ln in window if STARTED.search(ln))
    explained = sum(1 for ln in window if EXPLAINED.search(ln))
    unexplained = sum(1 for ln in window if UNEXPLAINED.search(ln))
    facts["careers started"] = f"{started} in {hours:g}h"
    facts["parked (explained)"] = explained
    facts["failures"] = unexplained

    if started == 0 and unexplained >= MIN_UNEXPLAINED:
        problems.append(
            f"**no career has started in {hours:g}h** and {unexplained} cycle(s) "
            f"failed without explaining why. The unit is up and the log is "
            f"moving, so nothing else here would notice. Read the last "
            f"`Visit to ... failed` lines - a repeated 391 means the held Steam "
            f"ticket was cancelled; that now drops the session and re-mints by "
            f"itself, so if it is still repeating that recovery is not working.")
    return facts, problems


def build_message(host: str, unit: str, problems: list[str], facts: dict) -> dict:
    return {
        "username": "AIC health",
        "embeds": [{
            "title": f"AIC idle farmer is wedged on {host}",
            "description": "\n\n".join(f"- {p}" for p in problems)[:3900],
            "color": 0xE04B4B,
            "fields": [{"name": k, "value": str(v)[:1000], "inline": True}
                       for k, v in facts.items()],
            "footer": {"text": f"{unit} -- this repeats on every check until it is fixed"},
        }],
    }


#: See aic/notify.py - Cloudflare 403s the default urllib User-Agent (error 1010),
#: so an alert sent without one never arrives. Silently. Which for a health check
#: is the exact failure it exists to prevent.
USER_AGENT = "aic-health/1.0"


def post(url: str, payload: dict) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, OSError) as e:
        print(f"  webhook POST failed: {e}")
        return False


def default_log() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "state" / (os.environ.get("AIC_PROFILE") or "default") / "logs" / "aic.log"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--unit", default="aic")
    ap.add_argument("--log", default=None,
                    help="AIC's log (default: state/$AIC_PROFILE/logs/aic.log beside this repo)")
    ap.add_argument("--stale-minutes", type=float, default=75.0,
                    help="how long the log may be silent before it is a fault. Must exceed the "
                         "longest UNANNOUNCED gap: a rotation slot is career_minutes/N, and a "
                         "single-account rotation is a full 50-minute career (75)")
    ap.add_argument("--productive-hours", type=float, default=3.0,
                    help="alert if NO career has started in this many hours AND "
                         "cycles are failing without explanation. 0 disables. "
                         "Zero careers is not itself a fault: a server refusing "
                         "starts (2509) correctly produces none (3)")
    ap.add_argument("--webhook", default=None)
    ap.add_argument("--dry-run", action="store_true", help="print the verdict, post nothing")
    args = ap.parse_args()

    log_path = Path(args.log).expanduser() if args.log else default_log()
    host = os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?")
    problems: list[str] = []
    facts: dict = {"unit": args.unit}

    state, problem = check_unit(args.unit)
    facts["systemd"] = state or "?"
    if problem:
        problems.append(problem)
    elif state.startswith("active"):
        # Only meaningful while the unit is up. A farmer that is deliberately off
        # has a stale log BY DEFINITION, and calling that a wedge would alert every
        # 30 minutes about a switch somebody chose to leave down.
        starts = recent_starts(log_path, args.productive_hours)
        log_facts, log_problems = check_log(log_path, args.stale_minutes * 60,
                                            starts=starts)
        facts.update(log_facts)
        problems += log_problems

        prod_facts, prod_problems = check_productivity(log_path,
                                                       args.productive_hours)
        facts.update(prod_facts)
        problems += prod_problems
    else:
        facts["log"] = "not checked (unit is not running)"

    for key, value in facts.items():
        print(f"  {key:14} {value}")
    print()

    if not problems:
        print(f"HEALTHY -- {facts['systemd']}, nothing to report.")
        return 0

    print("WEDGED")
    for p in problems:
        print(f"  - {p}")

    if args.dry_run:
        print("\n(--dry-run: nothing posted)")
        return 1

    url = (args.webhook or os.environ.get("AIC_ALERT_WEBHOOK")
           or os.environ.get("UMA_ALERT_WEBHOOK"))
    if not url:
        print("\nNo webhook configured, so this alert reached nobody. Set AIC_ALERT_WEBHOOK "
              "(or UMA_ALERT_WEBHOOK) in /etc/umabot.env.")
        return 1
    print("\nalert posted" if post(url, build_message(host, args.unit, problems, facts))
          else "\nthe webhook POST failed -- this alert reached nobody")
    return 1


if __name__ == "__main__":
    sys.exit(main())

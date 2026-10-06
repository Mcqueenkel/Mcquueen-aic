"""The Discord control bot: who it obeys, what it parses, and what it reports.

Everything here is offline - no Discord, no token: decide() is the whole
security decision, the reports are pure over a fake farm directory, and the
two actions that touch the box (env edit, restart gate) are checked on temp files.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import discord_bot as bot  # noqa: E402

FAILURES: list[str] = []
ME, OTHER, CH, OTHER_CH = 111, 222, 999, 888


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILURES.append(label)


def cfg(**env):
    return bot.Config(env)


def test_gate() -> None:
    print("gate: fails closed, the user list is required, channels only narrow it")
    nobody = cfg()
    check("nothing configured -> obey no one", bot.decide("!status", ME, CH, nobody) is None)
    users = cfg(CONTROL_AUTHORIZED_USERS=str(ME))
    check("authorized user obeyed", bot.decide("!status", ME, CH, users) == ("status", ""))
    check("other user ignored", bot.decide("!status", OTHER, CH, users) is None)
    chan = cfg(CONTROL_ALLOWED_CHANNELS=str(CH))
    check("a channel alone obeys no one", bot.decide("!fans", OTHER, CH, chan) is None
          and bot.decide("!fans", ME, CH, chan) is None)
    for bad in ("<@123456789012345678>", "12a", "١٢"):
        try:
            cfg(CONTROL_AUTHORIZED_USERS=bad)
            check(f"bad id {bad!r} refused", False)
        except ValueError:
            check(f"bad id {bad!r} refused", True)
    check("ids split on commas and spaces", cfg(CONTROL_AUTHORIZED_USERS="1, 2 3").authorized == {1, 2, 3})
    both = cfg(CONTROL_AUTHORIZED_USERS=str(ME), CONTROL_ALLOWED_CHANNELS=str(CH))
    check("both set: right user, wrong channel -> ignored", bot.decide("!status", ME, OTHER_CH, both) is None)
    check("both set: wrong user, right channel -> ignored", bot.decide("!status", OTHER, CH, both) is None)
    d = lambda text: bot.decide(text, ME, CH, both)
    check("case-insensitive", d("!STATUS") == ("status", ""))
    check("chatter mentioning a command does nothing", d("please !restart") is None)
    check("unknown command ignored", d("!rm -rf /") is None)
    check("extra words on a plain command ignored", d("!status now") is None and d("!restart please") is None)
    check("!ace / !team are reports", d("!ace") == ("ace", "") and d("!team") == ("team", ""))
    check("toggles parse", d("!ace off") == ("toggle", "ace off") and d("!chores ON") == ("toggle", "chores on"))
    check("bad toggle value ignored", d("!team maybe") is None and d("!chores") is None)
    check("!log defaults to 15, takes a number", d("!log") == ("log", "15") and d("!log 25") == ("log", "25"))
    check("!log with junk ignored", d("!log; reboot") is None and d("!log ١") is None)
    check("!ask keeps the whole message", d("!ask why did McKing fail?") == ("ask", "why did McKing fail?"))
    check("!ask with nothing ignored", d("!ask") is None)


def test_chunks() -> None:
    print("chunks + redaction: Discord's 2000-char limit, no secrets out")
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = bot.chunks(text, size=500)
    check("every part fits", all(len(p) <= 500 for p in parts), str(max(map(len, parts))))
    check("nothing lost or reordered", "".join(p + "\n" for p in parts).split() == text.split())
    check("one huge line is split too", all(len(p) <= 100 for p in bot.chunks("y" * 450, size=100)))
    fenced = bot.chunks("```\n" + text + "\n```", size=500)
    check("a code block cut in two is closed and reopened",
          len(fenced) > 1 and all(p.count("```") % 2 == 0 for p in fenced)
          and all(len(p) <= 510 for p in fenced))
    r = bot.redact("Logging in with password: hunter2 to https://discord.com/api/webhooks/1/abc and "
                   "eyJhbGciOiJFZERTQSJ9.eyJpc3MiOiJyOkJBRjYwN.c2lnbmF0dXJl")
    check("redact: passwords, webhooks, tokens", "hunter2" not in r and "webhooks/1" not in r
          and "eyJhbG" not in r and r.count("<redacted>") == 3, r)


def farm(tmp: Path, today: str, now: float) -> None:
    """A fake /opt/aic: roster, link registry, state files and a log. The log stamps
    server-local time, so its lines are built from ``now`` (16:00 UTC)."""
    (tmp / "state" / "aic" / "logs").mkdir(parents=True)
    json.dump([{"trainer_id": "111", "name": "Oguri_0111", "password": "x", "trainee": "Oguri"},
               {"trainer_id": "222", "name": "Oguri_0222", "password": "x", "trainee": "Oguri"}],
              open(tmp / "reroll_accounts.json", "w"))
    json.dump([{"viewer_id": 111, "trainer_id": "111", "name": "McKing", "password": "x"},
               {"viewer_id": 793593252166, "name": "McDonald"},
               {"viewer_id": 768270152387, "name": "mcdonald"}],
              open(tmp / "state" / "accounts.json", "w"))
    s = tmp / "state" / "aic"
    json.dump({"111": {"day": today, "baseline": 1000, "latest": 2_001_000},
               "222": {"day": today, "baseline": 0, "latest": 500_000},
               "333": {"day": "2000-01-01", "baseline": 0, "latest": 9}}, open(s / "fan_day.json", "w"))
    json.dump({"111": {"day": today, "started": 2, "good": 1, "best": 17303, "running": True}},
              open(s / "ace_state.json", "w"))
    json.dump({"day": today, "requested": [111, 222]}, open(s / "club_state.json", "w"))
    at = lambda minutes: bot.datetime.fromtimestamp(now + minutes * 60).strftime("%Y-%m-%d %H:%M:%S")
    log = [
        f"[{at(-70)},000] INFO    aic.rotate: === visit 9 | Oguri_0222 (its visit 3) ===",
        f"[{at(-58)},000] INFO    aic.rotate: club: donate_multiple rc=1 - bulk-donated to 2 member request(s) (today 2/10)",
        f"[{at(-19)},000] INFO    aic.rotate: === visit 1 | McKing (its visit 1) ===",
        f"[{at(-18)},000] INFO    aic.rotate: team: current team (86,042) is already the best (class 3)",
        f"[{at(-17.5)},000] INFO    aic.rotate: team: 5 battle(s) this visit, +4,349 fans",
        f"[{at(-17.4)},000] INFO    aic.rotate: Banked 1,081,312 fans (rank 10).",
        f"[{at(-17.3)},000] INFO    aic.rotate: Ace Run trainee: card 100701 - fills an open sprint slot (est. +10,200 team points).",
        f"[{at(-17)},000] INFO    aic.flow: New career finishes in 49m 59s.",
        f"[{at(-16)},000] INFO    aic.rotate: === visit 2 | Oguri_0222 (its visit 1) ===",
        f"[{at(-15.5)},000] INFO    aic.rotate: club: donate_multiple rc=1 - bulk-donated to 9 member request(s) (today 9/10)",
        f"[{at(-15.4)},000] INFO    aic.rotate: club: donate_multiple rc=1 - bulk-donated to 0 member request(s) (today 9/10)",
        f"[{at(-15)},000] INFO    aic.rotate: Ace Run finished: rank_score 17,303 (ACE) - 1/3 runs today, 1 ace(s), best 17,303.",
        f"[{at(-14.8)},000] INFO    aic.rotate: Next visit in 5m 0s: McKing.",
    ]
    (s / "logs" / "aic.log").write_text("\n".join(log) + "\n", encoding="utf-8")


def test_reports() -> None:
    print("reports: built from the farm's own files")
    tmp = Path(tempfile.mkdtemp())
    now = bot.datetime(2026, 10, 4, 16, 0, tzinfo=bot.timezone.utc).timestamp()
    today = bot.game_day(now)
    check("game-day starts at 15:00 UTC", today == "2026-10-04" and
          bot.game_day(bot.datetime(2026, 10, 4, 14, 59, tzinfo=bot.timezone.utc).timestamp()) == "2026-10-03")
    farm(tmp, today, now)
    old = bot.AIC
    bot.AIC = tmp
    try:
        fans = bot.fans_text(now)
        check("fans: only today's rows, named, totalled", "2,500,000 across 2 accounts" in fans
              and "McKing" in fans and "Oguri_0222" in fans and "333" not in fans, fans)
        ace = bot.ace_text(now)
        check("ace: runs/aces/best + latest pick", "runs 2/3" in ace and "17,303" in ace
              and "(running)" in ace and "card 100701" in ace, ace)
        team = bot.team_text(now)
        check("team: battles, fans and score per account", "battles  5" in team and "86,042" in team, team)
        club = bot.club_text(now)
        check("club: donate phase once the whole roster requested", "donate phase: 2/2" in club, club)
        check("club: a visit running at the reset is credited, zero batches skipped",
              "11 gifts from 1 account" in club and "Oguri_0222" in club and "?" not in club, club)
        log = bot.log_text(3)
        check("log: last n lines without the date", log.count("\n") == 4 and "Next visit in" in log
              and "2026-10-04 " not in log, log)
        st = bot.status_text(now)
        check("status: today's counts (the pre-reset visit not counted)", "2 visits" in st
              and "1 careers started" in st and "1,081,312 fans" in st and "best 17,303, 1 aces" in st, st)
        nm = bot.names()
        check("names: a shared in-game name gets the id's last 4 digits",
              nm[793593252166] == "McDonald 2166" and nm[768270152387] == "mcdonald 2387", str(nm))
        if os.name != "nt":
            link = tmp / "state" / "aic" / "linked.json"
            os.symlink(tmp / "state" / "aic" / "club_state.json", link)
            check("a symlinked farm file is refused", bot.read_json(link, "refused") == "refused")
        json.dump({"career_minutes": 50, "accounts": [{"trainer_id": "222 333", "name": "Spaced"}]},
                  open(tmp / "reroll_accounts.json", "w"))
        check("names: the object-format roster and spaced ids work", bot.names().get(222333) == "Spaced")
    finally:
        bot.AIC = old


def test_actions() -> None:
    print("actions: env edit keeps 600; restart waits for a real gap; Claude in its own unit")
    tmp = Path(tempfile.mkdtemp())
    env = tmp / "umabot.env"
    env.write_text("AIC_CHORES=1\nAIC_ACE=1\nAIC_CAREER_WEBHOOK=https://x\n", encoding="utf-8")
    check("switch off changes the file", bot.set_env_value(env, "AIC_ACE", "0") is True)
    check("only that key changed", env.read_text().splitlines() ==
          ["AIC_CHORES=1", "AIC_ACE=0", "AIC_CAREER_WEBHOOK=https://x"], env.read_text())
    check("same value again -> no change", bot.set_env_value(env, "AIC_ACE", "0") is False)
    check("missing key is added", bot.set_env_value(env, "AIC_TEAM", "1") and "AIC_TEAM=1" in env.read_text())
    if os.name != "nt":
        check("file stays 600", stat.S_IMODE(env.stat().st_mode) == 0o600, oct(env.stat().st_mode))
    check("env_value / is_on read it as aic does", bot.env_value("AIC_ACE", env) == "0"
          and not bot.is_on(bot.env_value("AIC_MISSING", env)) and bot.is_on(" ON "))
    stamp = lambda t: bot.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    now = bot.time.time()
    check("gap: 5m sleep stamped now -> ~300 s left",
          295 <= bot.gap_left(f"[{stamp(now)},000] INFO    aic.rotate: Next visit in 5m 0s: McKing.", now) <= 300)
    check("gap: hours parse",
          bot.gap_left(f"[{stamp(now)},1] INFO    aic.rotate: Next visit in 1h 2m 3s: X.", now) > 3700)
    check("gap: any other line -> 0",
          bot.gap_left("[2026-10-04 15:00:00,000] INFO x: Logging in McKing ...") == 0)
    lf = tmp / "aic.log"
    for label, line, want in (
            ("busy (no gap) -> wait", "[2026-10-04 15:00:00,000] INFO x: Logging in McKing ...", False),
            ("gap about to end (2s) -> wait",
             f"[{stamp(now)},000] INFO    aic.rotate: Next visit in 2s: McKing.", False),
            ("gap of 5m -> go", f"[{stamp(now)},000] INFO    aic.rotate: Next visit in 5m 0s: McKing.", True)):
        lf.write_text(f"[{stamp(now - 9)},000] INFO    aic.rotate: \n{line}\n", encoding="utf-8")
        check(f"restart gate: {label}", asyncio.run(bot.wait_for_gap(lf, wait=0)) is want)
    c = cfg(CONTROL_AUTHORIZED_USERS="1")
    cmd = bot.claude_cmd(c, fresh=False)
    check("follow-up continues the conversation", cmd[cmd.index("claude") + 1] == "--continue", str(cmd))
    check("fresh conversation does not", "--continue" not in bot.claude_cmd(c, fresh=True))
    check("Claude runs in its own capped unit as claudeops (who has sudo)",
          cmd[0] == "systemd-run" and "--uid=claudeops" in cmd and "RuntimeMaxSec=1800" in cmd
          and "MemoryMax=900M" in cmd and "--working-directory=/home/claudeops/aic-ops" in cmd, str(cmd))
    check("the prompt is never on the command line", all(not a.startswith("why") for a in cmd))
    check("help text lists every command, fits one message, no code block",
          all(f"`!{c}" in bot.HELP for c in ("status", "fans", "ace", "team", "club", "log",
                                              "restart", "ask <message>", "ask new"))
          and len(bot.HELP) < 2000 and "```" not in bot.HELP)


def main() -> int:
    for fn in (test_gate, test_chunks, test_reports, test_actions):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all discord-bot checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""AIC Discord control bot - watch and steer the farm from Discord, 24/7 on the VPS.

A webhook only talks OUT, so this is a real bot: it holds a gateway connection
and acts on commands from YOU. Commands (case-insensitive, the whole message):

    !help                    this list
    !status                  service + health, today's visits / careers / failures
    !fans                    fans gained this game-day, per account + roster total
    !ace                     this week's Ace Runs per account (runs, aces, best rank score)
    !team                    Team Trials today: battles, fans, current team score
    !club                    club phase (requests up / roster) and donations today
    !log [n]                 the last n log lines (default 15, max 30)
    !restart                 safe restart of aic - waits for a gap between visits
    !ace on|off   !team on|off   !chores on|off
                             switch a feature in /etc/umabot.env at a gap, then restart
    !ask <message>           hand anything else to Claude on the VPS (follow-ups
                             continue the same conversation)
    !ask new <message>       start a fresh Claude conversation

SECURITY - this bot restarts the farm and !ask runs Claude with sudo, so !ask is
root on this box. It obeys ONLY the user ids in CONTROL_AUTHORIZED_USERS (required;
a malformed id stops it from starting), and with CONTROL_ALLOWED_CHANNELS set, only
in those channels too. It never runs a command you type - only the fixed actions
above; !ask's text goes to Claude as a prompt on stdin, never to a shell.

It runs as root from a root-owned copy (/opt/aic-discord/discord_bot.py, python -I),
and reads the farm's umabot-writable files without following symlinks.

Config (/etc/aic-discord.env, 600 root, loaded by deploy/vps/aic-discord.service):
    DISCORD_BOT_TOKEN, CONTROL_AUTHORIZED_USERS, CONTROL_ALLOWED_CHANNELS
    CLAUDE_USER (claudeops), CLAUDE_WORKDIR (/home/claudeops/aic-ops),
    CLAUDE_TIMEOUT (1800 s)
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

AIC = Path(os.environ.get("AIC_HOME") or "/opt/aic")
ENV_FILE = Path(os.environ.get("AIC_ENV_FILE") or "/etc/umabot.env")
RESET_HOUR_UTC = 15
TOGGLES = {"ace": "AIC_ACE", "team": "AIC_TEAM", "chores": "AIC_CHORES"}
MAX_LOG_LINES = 30
SAFE_RESTART_WAIT = 15 * 60          # longest wait for a gap between visits
MIN_GAP = 30                         # seconds of gap that must be left to restart
READ_CAP = 4 * 1024 * 1024           # the log rotates at 20 MB; a game-day is ~2-3 MB
CLAUDE_UNIT = "aic-claude"
_LINE_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\] +(\w+) +[\w.]+: ")
_GAP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+\] .*Next visit in "
                  r"(?:(\d+)h )?(?:(\d+)m )?(\d+)s")
_REDACT = re.compile(
    r"(?i)(password\W{0,3})\S+"
    r"|https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+"
    r"|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]+"                 # JWTs (the Steam refresh token)
    r"|[\w-]{24,}\.[\w-]{6}\.[\w-]{27,}")                  # Discord bot tokens


def state_dir() -> Path:
    return AIC / "state" / "aic"


def log_path() -> Path:
    return state_dir() / "logs" / "aic.log"


# --------------------------------------------------------------------------- #
# config + the security decision
# --------------------------------------------------------------------------- #
def _id_set(raw) -> set[int]:
    """Discord ids from a comma/space list. A bad token is an error, never skipped:
    a typo must not quietly drop a lock."""
    out = set()
    for part in re.split(r"[\s,]+", str(raw or "").strip()):
        if not part:
            continue
        if not (part.isascii() and part.isdigit()):
            raise ValueError(f"bad Discord id {part!r}")
        out.add(int(part))
    return out


class Config:
    def __init__(self, env: dict):
        self.token = (env.get("DISCORD_BOT_TOKEN") or "").strip()
        self.authorized = _id_set(env.get("CONTROL_AUTHORIZED_USERS"))
        self.channels = _id_set(env.get("CONTROL_ALLOWED_CHANNELS"))
        self.claude_user = (env.get("CLAUDE_USER") or "claudeops").strip()
        self.claude_workdir = (env.get("CLAUDE_WORKDIR") or f"/home/{self.claude_user}/aic-ops").strip()
        try:
            self.claude_timeout = int(env.get("CLAUDE_TIMEOUT") or 1800)
        except ValueError:
            self.claude_timeout = 1800


def decide(content, author_id, channel_id, cfg: Config):
    """The whole security decision, pure: ``(command, argument)`` or ``None``.

    The user list is mandatory (empty -> obey no one); a channel list only
    narrows it. Only known commands, as the whole message.
    """
    if not cfg.authorized or int(author_id) not in cfg.authorized:
        return None
    if cfg.channels and int(channel_id) not in cfg.channels:
        return None
    text = str(content or "").strip()
    if not text.startswith("!"):
        return None
    head, _, rest = text[1:].partition(" ")
    head, rest = head.lower(), rest.strip()
    if head in ("help", "status", "fans", "club", "restart") and not rest:
        return head, ""
    if head in TOGGLES:
        if not rest:
            return (head, "") if head in ("ace", "team") else None
        if rest.lower() in ("on", "off"):
            return "toggle", f"{head} {rest.lower()}"
        return None
    if head == "log":
        if not rest:
            return "log", "15"
        return ("log", rest) if rest.isascii() and rest.isdigit() else None
    if head == "ask" and rest:
        return "ask", rest
    return None


def chunks(text: str, size: int = 1900) -> list[str]:
    """Split for Discord's 2000-char limit, preferring line breaks; a ``` block cut
    in two is closed and reopened so both halves still render as code."""
    text = str(text or "").strip() or "(empty)"
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    fixed, open_ = [], False
    for c in [c.rstrip("\n") for c in out if c.strip()]:
        c = "```\n" + c if open_ else c
        open_ = c.count("```") % 2 == 1
        fixed.append(c + "\n```" if open_ else c)
    return fixed


def redact(text: str) -> str:
    return _REDACT.sub(lambda m: (m.group(1) or "") + "<redacted>", str(text or ""))


# --------------------------------------------------------------------------- #
# reading the farm's files (umabot-writable; this bot is root)
# --------------------------------------------------------------------------- #
def read_text(path, limit: int = READ_CAP) -> str:
    """The last ``limit`` bytes of a regular file. Never follows a symlink and never
    blocks on a FIFO, so a planted link can't make root read (or hang on) anything."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    with os.fdopen(os.open(path, flags), "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        fh.seek(max(0, st.st_size - limit))
        raw = fh.read(limit)
    text = raw.decode("utf-8", "replace")
    return text.split("\n", 1)[-1] if st.st_size > limit else text   # drop the cut line


def read_json(path, default):
    try:
        return json.loads(read_text(path, 1024 * 1024))
    except (OSError, ValueError):
        return default


def env_value(key: str, path: Path | None = None) -> str:
    try:
        for line in Path(path or ENV_FILE).read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return v.strip()
    except OSError:
        pass
    return ""


def is_on(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")    # as aic reads it


def game_day(now: float | None = None) -> str:
    t = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return (t - timedelta(hours=RESET_HOUR_UTC)).strftime("%Y-%m-%d")


def reset_at(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=RESET_HOUR_UTC, tzinfo=timezone.utc)


def day_start(now: float | None = None) -> str:
    """When this game-day began, written the way the log writes time (server-local)."""
    return reset_at(game_day(now)).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def roster() -> list[dict]:
    raw = read_json(AIC / "reroll_accounts.json", [])
    rows = raw.get("accounts") if isinstance(raw, dict) else raw
    return [a for a in rows or [] if isinstance(a, dict)]


def _vid(a: dict) -> int:
    d = re.sub(r"\D", "", str(a.get("viewer_id") or a.get("trainer_id") or ""))
    return int(d) if d else 0


def names() -> dict[int, str]:
    """viewer id -> label: the in-game name (a shared name gets the id's last four
    digits, as aic.account.names_by_viewer labels it in the log), else the roster
    nickname."""
    out = {v: str(a.get("name") or v) for a in roster() if (v := _vid(a))}
    accounts = read_json(AIC / "state" / "accounts.json", [])
    ingame = {v: str(a["name"]).strip() for a in (accounts if isinstance(accounts, list) else [])
              if isinstance(a, dict) and (v := _vid(a)) and str(a.get("name") or "").strip()}
    folded = [n.casefold() for n in ingame.values()]
    out.update({v: f"{n} {str(v)[-4:]}" if folded.count(n.casefold()) > 1 else n
                for v, n in ingame.items()})
    return out


def today_lines(now: float | None = None, path: Path | None = None) -> list[tuple[str, str, str]]:
    """``[(time, level, message)]`` for this game-day, oldest first, across the
    rotated log too. If a visit was already running at the reset, its header comes
    first with level 'PREV' so its later lines are credited to the right account."""
    start = day_start(now)
    lp = Path(path or log_path())
    rows, prev = [], None
    for p in (lp.with_name(lp.name + ".1"), lp):
        try:
            text = read_text(p)
        except OSError:
            continue
        for line in text.splitlines():
            m = _LINE_TS.match(line)
            if not m:
                continue
            msg = line[m.end():].rstrip()
            if m.group(1) >= start:
                rows.append((m.group(1)[11:19], m.group(2), msg))
            elif msg.startswith("=== visit"):
                prev = msg
    if prev and rows and not rows[0][2].startswith("=== visit"):
        rows.insert(0, ("", "PREV", prev))
    return rows


def _by_account(rows):
    """Yield ``(account label, time, message)`` - the label of the visit a line is in."""
    cur = "?"
    for t, _lvl, msg in rows:
        m = re.match(r"=== visit \d+ \| (.+?) \(its visit", msg)
        if m:
            cur = m.group(1)
            continue
        yield cur, t, msg


def _sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"? ({exc})"


# --------------------------------------------------------------------------- #
# read-only reports
# --------------------------------------------------------------------------- #
def status_text(now: float | None = None, rows=None) -> str:
    rows = today_lines(now) if rows is None else rows
    msgs = [m for _, lvl, m in rows if lvl != "PREV"]
    active = _sh("systemctl", "is-active", "aic")
    since = _sh("systemctl", "show", "aic", "-p", "ActiveEnterTimestamp", "--value")
    health = _sh("systemctl", "show", "aic-health", "-p", "Result", "--value")
    scores = [int(s.replace(",", "")) for s in
              (re.search(r"Ace Run finished: rank_score ([\d,]+)", m).group(1)
               for m in msgs if "Ace Run finished: rank_score" in m)]
    fans = sum(int(m.group(1).replace(",", "")) for m in
               (re.search(r"Banked ([\d,]+) fans", x) for x in msgs) if m)
    failed = [m for m in msgs if re.match(r"Visit to .* failed", m)]
    icon = "🟢" if active == "active" else "🔴"
    lines = [
        f"{icon} aic **{active}** since {since or '?'} | health check: {health or '?'}",
        f"Today (since the {RESET_HOUR_UTC}:00 UTC reset): "
        f"{sum(m.startswith('=== visit') for m in msgs)} visits, "
        f"{sum('New career finishes' in m for m in msgs)} careers started, "
        f"{fans:,} fans banked from careers, {len(failed)} failed visit(s)",
        f"Ace Runs finished: {len(scores)}"
        + (f" (best {max(scores):,}, {sum('(ACE)' in m for m in msgs)} aces)" if scores else ""),
    ]
    if failed:
        lines.append("Last failure: " + failed[-1][:150])
    if msgs:
        lines.append(f"Last log line ({rows[-1][0]} server time): {rows[-1][2][:150]}")
    else:
        lines.append("No log lines since the reset. Last line: " + log_text(1).strip("`\n"))
    return "\n".join(lines)


def fans_text(now: float | None = None) -> str:
    day = game_day(now)
    store = read_json(state_dir() / "fan_day.json", {}) or {}
    nm = names()
    rows = []
    for vid, rec in store.items():
        if isinstance(rec, dict) and rec.get("day") == day:
            try:
                rows.append((nm.get(int(vid), vid), max(0, int(rec["latest"]) - int(rec["baseline"]))))
            except (KeyError, TypeError, ValueError):
                continue
    if not rows:
        return f"No fan readings yet for game-day {day}."
    rows.sort(key=lambda r: -r[1])
    width = max(len(str(n)) for n, _ in rows)
    body = "\n".join(f"{str(n):<{width}}  {g:>12,}" for n, g in rows)
    return (f"**Fans this game-day ({day})** - {sum(g for _, g in rows):,} across {len(rows)} accounts\n"
            f"```\n{body}\n```")


def ace_period(now: float | None = None) -> str:
    """aic.ace.period_key: the ISO week of the game-day (Monday 15:00 UTC reset),
    or the game-day itself with AIC_ACE_PERIOD=day."""
    day = game_day(now)
    if (env_value("AIC_ACE_PERIOD") or "week").strip().lower() == "day":
        return day
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def ace_text(now: float | None = None, rows=None) -> str:
    period = ace_period(now)
    try:
        target = int(env_value("AIC_ACE_RUNS") or 5)
    except ValueError:
        target = 5
    st = read_json(state_dir() / "ace_state.json", {}) or {}
    nm = names()
    once = {v: int(a.get("ace_once") or 0) for a in roster() if (v := _vid(a)) and a.get("ace_once")}
    done = read_json(state_dir() / "ace_once.json", {}) or {}
    out = []
    for vid, rec in sorted(st.items(), key=lambda kv: str(nm.get(int(kv[0]), kv[0]))):
        if not isinstance(rec, dict) or rec.get("period") != period:
            continue
        runs = (f"one-time {int(done.get(str(vid)) or 0)}/{once[int(vid)]} done" if int(vid) in once
                else f"runs {rec.get('started', 0)}/{target}")
        out.append(f"{str(nm.get(int(vid), vid)):<18} {runs}  aces {rec.get('good', 0)}"
                   f"  best {int(rec.get('best') or 0):>6,}{'  (running)' if rec.get('running') else ''}")
    rows = today_lines(now) if rows is None else rows
    picks = [(a, re.sub(r".*Ace Run trainee: ", "", m)) for a, _, m in _by_account(rows)
             if "Ace Run trainee:" in m][-5:]
    text = f"**Ace Runs, {period}** (resets Monday 15:00 UTC)\n```\n" + ("\n".join(out) or "none yet") + "\n```"
    if picks:
        text += "Latest picks:\n" + "\n".join(f"- {a}: {p[:110]}" for a, p in picks)
    return text


def team_text(now: float | None = None, rows=None) -> str:
    rows = today_lines(now) if rows is None else rows
    s = defaultdict(lambda: {"battles": 0, "fans": 0, "score": 0, "edits": 0})
    for acct, _t, m in _by_account(rows):
        g = re.search(r"team: (\d+) battle\(s\) this visit, \+([\d,]+) fans", m)
        if g:
            s[acct]["battles"] += int(g.group(1))
            s[acct]["fans"] += int(g.group(2).replace(",", ""))
        g = re.search(r"current team \(([\d,]+)\)", m) or re.search(r"evaluation [\d,]+ -> ([\d,]+)", m)
        if g:
            s[acct]["score"] = int(g.group(1).replace(",", ""))
        if "team_edit rc=1" in m:
            s[acct]["edits"] += 1
    if not s:
        return "No Team Trials activity yet this game-day."
    body = "\n".join(f"{a:<18} battles {v['battles']:>2}  +{v['fans']:>6,} fans  team {v['score']:>7,}"
                     f"{'  (updated x' + str(v['edits']) + ')' if v['edits'] else ''}"
                     for a, v in sorted(s.items(), key=lambda kv: -kv[1]['score']))
    return (f"**Team Trials, game-day {game_day(now)}** - "
            f"{sum(v['battles'] for v in s.values())} battles\n```\n{body}\n```")


def club_text(now: float | None = None, rows=None) -> str:
    """Mirrors aic.chores._donate_phase: donate once the whole roster has requested
    today, or AIC_CLUB_REQUEST_WAIT (3h) after the reset."""
    now = time.time() if now is None else now
    day = game_day(now)
    st = read_json(state_dir() / "club_state.json", {}) or {}
    ids = {v for a in roster() if (v := _vid(a)) and a.get("club") is not False}
    asked = len(ids & {int(i) for i in st.get("requested") or [] if str(i).isdigit()}) \
        if st.get("day") == day else 0
    wait = float(env_value("AIC_CLUB_REQUEST_WAIT") or 3 * 3600)
    donate = not ids or asked >= len(ids) or now - reset_at(day).timestamp() >= wait
    rows = today_lines(now) if rows is None else rows
    donated = defaultdict(int)
    for acct, _t, m in _by_account(rows):
        g = re.search(r"bulk-donated to (\d+) member", m)
        if g and int(g.group(1)) > 0:
            donated[acct] += int(g.group(1))
    lines = [f"**Club, game-day {day}** - {'donate' if donate else 'request'} phase: "
             f"{asked}/{len(ids)} accounts have their request up"]
    if donated:
        lines.append(f"Donations today: {sum(donated.values())} gifts from {len(donated)} account(s)")
        lines.append("```\n" + "\n".join(f"{a:<18} {n:>3} gifts" for a, n in
                                         sorted(donated.items(), key=lambda kv: -kv[1])) + "\n```")
    else:
        lines.append("No donations yet today (they start once every request is up, "
                     f"or {wait / 3600:g}h after the reset).")
    return "\n".join(lines)


def log_text(n: int, path: Path | None = None) -> str:
    n = max(1, min(int(n), MAX_LOG_LINES))
    try:
        tail = [ln for ln in read_text(path or log_path(), 64 * 1024).splitlines() if ln.strip()][-n:]
    except OSError as exc:
        return f"(cannot read the log: {exc})"
    out = []
    for line in tail:
        m = _LINE_TS.match(line)
        out.append((f"{m.group(1)[11:19]} {line[m.end():]}" if m else line).rstrip()[:180])
    return redact("```\n" + "\n".join(out) + "\n```")


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def set_env_value(path: Path, key: str, value: str) -> bool:
    """Set ``key=value`` in an env file in place, keeping it 600. True if changed."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines()
    out, seen, changed = [], False, False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            seen = True
            if line != f"{key}={value}":
                changed = True
            out.append(f"{key}={value}")
        else:
            out.append(line)
    if not seen:
        out.append(f"{key}={value}")
        changed = True
    if changed:
        tmp = p.with_suffix(p.suffix + ".tmp")
        # 600 from the first byte: the file holds webhooks and the Steam secrets
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)                   # in case a stale .tmp pre-existed
        tmp.replace(p)
    return changed


def gap_left(line: str, now: float | None = None) -> float:
    """Seconds of the rotation's sleep still ahead, from a 'Next visit in ...' line
    (0 for any other line). The log stamps server-local time, as mktime reads it."""
    m = _GAP.match(line or "")
    if not m:
        return 0.0
    h, mi, s = (int(x or 0) for x in m.group(2, 3, 4))
    end = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + h * 3600 + mi * 60 + s
    return end - (time.time() if now is None else now)


def last_line(path: Path) -> str:
    try:
        return ([ln for ln in read_text(path, 4096).splitlines() if ln.strip()] or [""])[-1]
    except OSError:
        return ""


async def wait_for_gap(log_file: Path, wait: int, min_gap: int = MIN_GAP) -> bool:
    """True once the rotation is asleep with at least ``min_gap`` s left; False if
    that doesn't happen within ``wait`` s. Checks at least once."""
    deadline = time.time() + wait
    while gap_left(last_line(log_file)) < min_gap:
        if time.time() >= deadline:
            return False
        await asyncio.sleep(5)
    return True


async def safe_restart(log_file: Path | None = None, wait: int = SAFE_RESTART_WAIT,
                       before=None) -> str:
    """Restart aic only while the rotation sleeps, running ``before()`` (e.g. the env
    edit) right before it - so a timeout leaves everything untouched. A crashed or
    failed aic has no visit in flight and is restarted at once."""
    if await asyncio.to_thread(_sh, "systemctl", "is-active", "aic") == "active":
        if not await wait_for_gap(log_file or log_path(), wait):
            return (f"⚠️ no gap between visits in {wait // 60} min - nothing changed, "
                    f"not restarting; try again later.")
    else:
        await asyncio.to_thread(_sh, "systemctl", "reset-failed", "aic")
    if before:
        before()
    proc = await asyncio.create_subprocess_exec("systemctl", "restart", "aic")
    rc = await proc.wait()
    await asyncio.sleep(4)
    state = await asyncio.to_thread(_sh, "systemctl", "is-active", "aic")
    pid = await asyncio.to_thread(_sh, "systemctl", "show", "aic", "-p", "MainPID", "--value")
    return (f"{'✅' if rc == 0 and state == 'active' else '⚠️'} restarted between visits - "
            f"aic is {state} (pid {pid})")


def claude_cmd(cfg: Config, fresh: bool) -> list[str]:
    """Claude Code in print mode, in its own transient unit: one cgroup holds Claude
    and everything it starts (sudo children too), so a timeout kills all of it, and
    its memory is capped so an OOM takes Claude, never aic. The prompt goes in on
    STDIN, never the command line, so a message starting with '-' isn't a flag."""
    home = f"/home/{cfg.claude_user}"
    cmd = ["systemd-run", "--quiet", "--pipe", "--wait", "--collect",
           f"--unit={CLAUDE_UNIT}", f"--uid={cfg.claude_user}",
           f"--working-directory={cfg.claude_workdir}",
           f"--setenv=HOME={home}", f"--setenv=USER={cfg.claude_user}", "--setenv=LANG=C.UTF-8",
           "--setenv=PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
           "--setenv=DISABLE_AUTOUPDATER=1",  # pinned: newer builds need a CPU this VPS lacks
           "--setenv=NODE_OPTIONS=--max-old-space-size=512",
           "-p", f"RuntimeMaxSec={cfg.claude_timeout}", "-p", "MemoryMax=900M",
           "-p", "OOMScoreAdjust=900", "-p", "Nice=10", "-p", "CPUWeight=20",
           "--", "claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
    if not fresh:
        cmd.insert(cmd.index("claude") + 1, "--continue")
    return cmd


async def _run_claude(cfg: Config, fresh: bool, prompt: str) -> tuple[int | None, str]:
    """``(exit code, output)``; exit code None when it had to be killed."""
    proc = await asyncio.create_subprocess_exec(
        *claude_cmd(cfg, fresh), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input=prompt.encode("utf-8")),
                                        timeout=cfg.claude_timeout + 60)
    except asyncio.TimeoutError:   # RuntimeMaxSec should have ended it already
        await asyncio.to_thread(_sh, "systemctl", "kill", "--signal=SIGKILL", f"{CLAUDE_UNIT}.service")
        proc.kill()
        await proc.wait()
        return None, ""
    return proc.returncode, (out or b"").decode("utf-8", "replace").strip()


async def ask_claude(cfg: Config, text: str) -> str:
    fresh = text.lower().startswith("new ")
    prompt = text[4:].strip() if fresh else text
    if not prompt:
        return "Usage: `!ask <message>` or `!ask new <message>`"
    try:
        rc, text = await _run_claude(cfg, fresh, prompt)
        if rc and not fresh and "no conversation found" in text.lower():
            rc, text = await _run_claude(cfg, True, prompt)     # nothing to continue yet
    except FileNotFoundError as exc:
        return f"systemd-run is missing ({exc})."
    if rc is None:
        return f"⏱️ Claude did not finish within {cfg.claude_timeout // 60} min - stopped it."
    if rc and "already" in text and CLAUDE_UNIT in text:
        return "⏳ Claude is still working on an earlier request (the aic-claude unit is running)."
    if rc:
        return (text or "(no output - it may have hit the time limit)") + f"\n\n⚠️ Claude exited with code {rc}"
    return text or "(Claude returned nothing)"


# Plain Discord markdown, not a code block: a code block can't wrap long lines nicely.
HELP = """**AIC farm bot**
**Reports**
`!status` - is the farm running, today's visits, careers, failures
`!fans` - fans gained today, per account
`!ace` - this week's Ace Runs (runs, aces, best score)
`!team` - Team Trials today (battles, fans, team score)
`!club` - club phase and today's donations
`!log` or `!log 30` - the latest log lines (max 30)
**Control**
`!restart` - restart the farm safely (waits for a gap between visits)
`!ace on` / `!ace off` - also `!team` and `!chores`: switch a feature, then safe restart
**Claude**
`!ask <message>` - ask Claude on the VPS (follow-ups continue the chat)
`!ask new <message>` - start a fresh chat"""


# --------------------------------------------------------------------------- #
# the bot
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        cfg = Config(os.environ)
    except ValueError as exc:
        sys.exit(f"{exc} in /etc/aic-discord.env - refusing to start")
    if not cfg.token:
        sys.exit("DISCORD_BOT_TOKEN is not set")
    if not cfg.authorized:
        sys.exit("CONTROL_AUTHORIZED_USERS is not set - refusing to start (fail closed)")
    import discord

    intents = discord.Intents.default()
    intents.message_content = True          # privileged: enable it in the developer portal too
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    busy = asyncio.Lock()                   # one restart / toggle / !ask at a time
    reports = {"status": status_text, "fans": fans_text, "ace": ace_text,
               "team": team_text, "club": club_text}

    async def reply(channel, text: str, attach_over: int = 4):
        text = redact(text)
        parts = chunks(text)
        if len(parts) > attach_over:
            for p in parts[:2]:
                await channel.send(p)
            await channel.send("(full reply attached)",
                               file=discord.File(io.BytesIO(text.encode("utf-8")), "reply.txt"))
            return
        for p in parts:
            await channel.send(p)

    @client.event
    async def on_ready():
        print(f"[discord] logged in as {client.user} | users {sorted(cfg.authorized)} | "
              f"channels {sorted(cfg.channels) or 'any'}", flush=True)

    @client.event
    async def on_message(message):
        if message.author == client.user or message.author.bot:
            return
        action = decide(message.content, message.author.id, message.channel.id, cfg)
        if not action:
            return
        cmd, arg = action
        # never the !ask text: it may hold something secret, and the journal keeps it
        print(f"[discord] {cmd} {'' if cmd == 'ask' else arg!r} ({len(arg)} chars) "
              f"from {message.author.id}", flush=True)
        ch = message.channel
        try:
            if cmd == "help":
                await ch.send(HELP)
            elif cmd in reports:
                await reply(ch, await asyncio.to_thread(reports[cmd]))
            elif cmd == "log":
                await reply(ch, await asyncio.to_thread(log_text, int(arg)))
            elif cmd in ("restart", "toggle"):
                if busy.locked():
                    await ch.send("⏳ still busy with the previous restart / !ask.")
                    return
                async with busy:
                    if cmd == "toggle":
                        feature, state = arg.split()
                        key, val = TOGGLES[feature], "1" if state == "on" else "0"
                        if is_on(env_value(key)) == (state == "on"):
                            await ch.send(f"{feature} is already {state}.")
                            return
                        await ch.send(f"🔧 will set {key}={val} at the next gap between visits, "
                                      f"then restart aic…")
                        await ch.send(await safe_restart(before=lambda: set_env_value(ENV_FILE, key, val)))
                    else:
                        await ch.send("🔁 restarting aic at the next gap between visits…")
                        await ch.send(await safe_restart())
            elif cmd == "ask":
                if busy.locked():
                    await ch.send("⏳ Claude is still working on the previous request.")
                    return
                async with busy:
                    await ch.send("🤖 asking Claude on the VPS… (this can take a few minutes)")
                    async with ch.typing():
                        answer = await ask_claude(cfg, arg)
                    await reply(ch, answer)
        except Exception as exc:  # noqa: BLE001 - a control bot must never crash
            print(f"[discord] {cmd} failed: {type(exc).__name__}", flush=True)
            await ch.send(redact(f"⚠️ {cmd} failed: {exc}"))

    client.run(cfg.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Per-account, per-game-day fan cap - the one ops feature Icarus has and this did not.

Ported from Icarus' ``scripts/multi_account_run.py`` (``_daily_baseline`` /
``sweep_day_fans.json``): same rollover, same restart-safety, deliberately the
same on-disk shape, because the bug it exists to prevent is the same one.

**The target is fans GAINED THIS GAME-DAY, not a lifetime total.** Lifetime is in
the hundreds of millions, so an absolute number is meaningless; what is wanted is
"farm ~33M into this account today, then leave it alone".

**The baseline is PERSISTED, keyed by viewer_id.** Keeping it only in the running
process resets it to the current total on every restart, so a mid-day restart
farms the whole target a *second* time on top of what the day already had.
MEASURED in Icarus 2026-08-31: a restart made one account farm 33M again over
~9.7M already gained -> ~43M in one day. It rolls over on its own at the 15:00
UTC reset because ``day`` changes and no stored record matches - no timer needed.

THE CAP IS OFF, AND THE ACCOUNTING IS NOT
-----------------------------------------
``AIC_FAN_TARGET`` was removed from the unit on 2026-09-05 because at idle-career
throughput it could never fire. MEASURED over three days on a 12-account roster:
~900,000 fans per career (781k-1,047k observed) and ~18.75 careers per account
per game-day = **~17M**; even at the theoretical ceiling of 28.8 careers it is
~26M. The target was 33M, carried over from the turn-by-turn bot, which banked
more per run. So the cap never once ran in 781 careers - it was a number that
looked like a safety limit and was really just decoration.

What survives is the per-game-day tally, which is load-bearing for something
else: :func:`day_breakdown` sums it into the roster fan total on the Discord
career card. That is why this module still exists and why :func:`check` records
before it consults the goal.

Setting ``AIC_FAN_TARGET`` again re-arms the cap with no other change:

    Environment=AIC_FAN_TARGET=15000000     # ~2/3 of a day, if a cap is wanted
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .logging_setup import get

log = get("fancap")

#: The in-game daily reset, in UTC hours. Shifting a moment back by this folds
#: reset-time-today .. reset-time-tomorrow onto one date key.
GAME_DAY_RESET_UTC_HOUR = 15

#: Stop this many fans BEFORE the target, so the career started at the last
#: uncapped visit can finish without pushing past it. Icarus uses 1.1M for a
#: turn-by-turn career (~0.7-1.0M each).
#:
#: ponytail: idle-career fan yield is NOT measured yet (see HANDOFF §7's open
#: question), so this is a knob, not a constant. Read one G4 cycle's banked fans
#: and set AIC_FAN_STOP_MARGIN to a bit over it.
DEFAULT_STOP_MARGIN = 1_100_000

STORE_NAME = "fan_day.json"


class FanCapReached(RuntimeError):
    """This account has farmed its day's worth. Not an error - a schedule signal.

    Callers should skip the account and come back, not retry: ``seconds_to_reset``
    is how long until the cap lifts on its own.
    """

    def __init__(self, message: str, seconds_to_reset: float):
        super().__init__(message)
        self.seconds_to_reset = seconds_to_reset
        #: Same value under the name flow.StartDeferred uses, so the loop drivers
        #: can handle "come back later" once instead of once per cause.
        self.seconds = seconds_to_reset


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip().replace("_", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        if raw:
            log.warning("%s=%r is not a number; ignoring it.", name, os.environ.get(name))
        return default


def target() -> int:
    """The day's fan target, or 0 when no cap is configured."""
    return max(0, _env_int("AIC_FAN_TARGET"))


def stop_margin(goal: int = 0) -> int:
    """The stop margin, never large enough to cap an account before it farms.

    A margin >= the target makes ``gained >= goal - margin`` true at zero fans, so
    every account is capped on every visit and NOTHING is ever farmed - and the
    message blames the cap, so it reads like the target working. Easy to hit with
    a small target and the 1.1M default (AIC_FAN_TARGET=1000000 does it).
    """
    margin = max(0, _env_int("AIC_FAN_STOP_MARGIN", DEFAULT_STOP_MARGIN))
    if goal and margin >= goal:
        log.warning("AIC_FAN_STOP_MARGIN (%s) is not below AIC_FAN_TARGET (%s); it "
                    "would cap every account before a single career ran. Using %s.",
                    f"{margin:,}", f"{goal:,}", f"{goal // 10:,}")
        margin = goal // 10
    return margin


def day_key(now: float | None = None) -> str:
    """The game-day a moment belongs to: the DATE of the most recent 15:00 UTC reset."""
    t = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return (t - timedelta(hours=GAME_DAY_RESET_UTC_HOUR)).date().isoformat()


def seconds_to_reset(now: float | None = None) -> float:
    """Seconds until the next 15:00 UTC rollover."""
    t = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    nxt = t.replace(hour=GAME_DAY_RESET_UTC_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= t:
        nxt += timedelta(days=1)
    return (nxt - t).total_seconds()


def store_path(settings: Settings) -> Path:
    return settings.state_dir / STORE_NAME


def baseline(settings: Settings, viewer_id: int, lifetime_now: int,
             day: str | None = None, just_banked: int = 0) -> int:
    """Lifetime-fan count at the START of this account's current game-day.

    First call of the day for this viewer_id records ``lifetime_now`` and returns
    it. Every later call that day - including after a restart - returns the
    recorded value, so the day continues where it left off.

    ``just_banked`` is the fans of a career banked in THIS visit, before this
    reading. It only matters when there is no previous reading to carry over (a
    first-ever visit, or state lost to a rebuild): the day then starts below that
    career, so it still counts today instead of vanishing into the baseline.
    """
    day = day or day_key()
    path = store_path(settings)
    try:
        store = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError):
        store = {}
    if not isinstance(store, dict):
        store = {}

    rec = store.get(str(viewer_id))
    if isinstance(rec, dict) and rec.get("day") == day and "baseline" in rec:
        start = int(rec["baseline"])                         # restart mid-day: continue the day
        if int(rec.get("latest") or 0) == int(lifetime_now):
            return start                                     # nothing new to record
        rec["latest"] = int(lifetime_now)
    else:
        # First visit of a NEW game-day. Carry the previous day's last reading
        # forward as the baseline instead of the current total, so fans banked
        # across the 15:00 reset still count today - e.g. a career that started
        # before the reset and is banked just after it. Without this, that
        # career's fans land in the baseline and the account shows +0 today (the
        # "added to the list but no fans" the operator hit). No double-count: the
        # previous day tallied only up to its own ``latest``.
        prev_latest = None
        if isinstance(rec, dict):
            try:
                prev_latest = int(rec.get("latest"))
            except (TypeError, ValueError):
                prev_latest = None
        if prev_latest is not None and 0 <= prev_latest <= int(lifetime_now):
            start = prev_latest
        else:
            # No reading to carry over (first-ever visit, or the state was lost
            # - MEASURED 2026-10-04 after the server rebuild: every account's
            # first career of the day showed +0). The career banked in this very
            # visit is still today's.
            start = max(0, int(lifetime_now) - max(0, int(just_banked or 0)))
        store[str(viewer_id)] = {"day": day, "baseline": start,
                                 "latest": int(lifetime_now)}
    # ``latest`` is what makes :func:`day_total` possible: each account's most
    # recent lifetime reading, refreshed on every visit, so the roster total is
    # a sum over this file rather than a second store the loop has to maintain.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # A cap that cannot persist is still better than no cap for this process,
        # but it is no longer restart-safe - say so rather than fail the run.
        log.warning("could not write %s (%s); the day baseline will reset on restart.",
                    path, exc)
    return start


def day_breakdown(settings: Settings,
                  day: str | None = None) -> list[tuple[int, int]]:
    """``[(viewer_id, fans gained today)]``, biggest gain first.

    Each account contributes what it had gained as of its last visit, so a row
    is current to within one lap of the rotation. Records from an earlier
    game-day are skipped rather than carried forward - the 15:00 UTC rollover
    empties this the same way it resets the per-account cap.

    The per-account numbers are already in this file (``baseline`` is where the
    account's game-day started, ``latest`` its newest reading), so reporting
    them needs no second store: this is the one traversal, and
    :func:`day_total` is its sum.
    """
    day = day or day_key()
    try:
        store = json.loads(store_path(settings).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(store, dict):
        return []

    rows: list[tuple[int, int]] = []
    for viewer, rec in store.items():
        if not isinstance(rec, dict) or rec.get("day") != day:
            continue
        try:
            gained = int(rec.get("latest") or 0) - int(rec.get("baseline") or 0)
            rows.append((int(viewer), max(0, gained)))
        except (TypeError, ValueError):
            continue
    # Biggest first, viewer id breaking ties so the order is stable between
    # posts and a reader can follow one account down the list.
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def day_total(settings: Settings, day: str | None = None) -> tuple[int, int]:
    """``(fans gained today across the roster, accounts counted)``."""
    rows = day_breakdown(settings, day)
    return sum(gained for _, gained in rows), len(rows)


def check(settings: Settings, viewer_id: int, lifetime_now: int) -> None:
    """Record the day's fan gain, and raise :class:`FanCapReached` if capped.

    Called at the single point every start routes through
    (``flow.start_independent``), *after* any finished career has been banked -
    so the fans just earned count toward today before the next start is judged.

    With no ``AIC_FAN_TARGET`` set - the default, see the module docstring - this
    only records and logs. It never stops a start.
    """
    if not viewer_id:
        log.debug("no viewer_id on the account state; fans not accounted.")
        return
    if lifetime_now <= 0:
        # user_info.fan absent or zero. Refusing to start on a reading we do not
        # trust would stop the farm dead; carrying on only risks overshoot.
        log.warning("lifetime fan total reads %s - skipping fan accounting this "
                    "cycle.", lifetime_now)
        return

    # Recorded BEFORE the goal is consulted, so the day's tally - and the roster
    # total on the Discord card that reads it - stays correct with the cap off.
    day = day_key()
    start = baseline(settings, viewer_id, lifetime_now, day)
    gained = max(0, int(lifetime_now) - start)

    goal = target()
    if not goal:
        log.info("Fans: %s lifetime, +%s today (day %s).",
                 f"{lifetime_now:,}", f"{gained:,}", day)
        return

    margin = stop_margin(goal)
    remaining = max(0, goal - margin - gained)

    if gained >= goal - margin:
        wait = seconds_to_reset()
        raise FanCapReached(
            f"fan cap reached for {viewer_id}: +{gained:,} today (target {goal:,}, "
            f"stopping {margin:,} short). Resets in {wait / 3600:.1f} h (day {day}).",
            wait,
        )

    log.info("Fans: %s lifetime, +%s today, ~%s more to reach +%s (day %s).",
             f"{lifetime_now:,}", f"{gained:,}", f"{remaining:,}", f"{goal:,}", day)

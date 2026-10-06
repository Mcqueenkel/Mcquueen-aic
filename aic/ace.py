"""Ace Run: S-rank Grand Live trainees for Team Trials, a few a week.

Each week (from Monday's game-day, i.e. Monday 15:00 UTC) an account does
exactly ``RUNS`` (5) Ace Runs, whatever rank they reach, then goes back to fan
farming for the rest of the week (``AIC_ACE_PERIOD=day``: RUNS every game-day). A finish at ``rank_score >= MIN_SCORE`` (14,500 = S, master.mdb
single_mode_rank id 15) is counted and logged as an "ace", but does not change
how many runs are done. An Ace Run is the same idle (Auto Training) career aic
always runs, with four changes:

* the trainee is a character NOT on the account's Team Trials team (and not
  already Ace-Run this week) - the one whose aptitudes would add the most to the
  team (:func:`pick_trainee`), since a team fields each character only once;
* scenario ``SCENARIO`` (3, Grand Live), chosen only if the account has it;
* no race schedule - only the career's own goal races, so every other turn
  trains (fan careers enter 44-52 races, which is why they finish at C/C+);
* the borrowed sixth card is ``FRIEND_CARD`` (30052, SSR Light Hello),
  refreshing the offer list up to ``RELOADS`` times to find it;
* leftover skill points are spent at the finish (aic.skills), which adds
  their grade_value straight onto the rank_score.

MEASURED 2026-10-02 (Mizura, El Condor Pasa): the first Ace Run finished at
rank 16 (S+), rank_score 17,303 with 25 skills bought - fan careers on the same
account finish at C/C+ (~4,500-5,000).

Off unless ``AIC_ACE`` is set; ``AIC_ACE_ONLY=<viewer id>`` limits it to one
account for a trial. State lives in ``ace_state.json`` next to the other
per-account files.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from functools import lru_cache
from pathlib import Path

from .config import Settings
from .fancap import day_key
from .logging_setup import get
from .racedata import find_master_mdb

log = get("ace")

STORE_NAME = "ace_state.json"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


SCENARIO = _int("AIC_ACE_SCENARIO", 3)
#: Ace Runs per account per PERIOD - then fan farming until the next one.
RUNS = _int("AIC_ACE_RUNS", 5)
#: "week" (default): RUNS a week, resetting with Monday's game-day (Monday 15:00
#: UTC). "day": RUNS every game-day, the old behaviour.
PERIOD = (os.environ.get("AIC_ACE_PERIOD") or "week").strip().lower()
#: For log lines: "this week" / "today".
PER = "today" if PERIOD == "day" else "this week"
#: An "ace" finish, for the count and the log only.
MIN_SCORE = _int("AIC_ACE_MIN_SCORE", 14_500)
FRIEND_CARD = _int("AIC_ACE_FRIEND_CARD", 30052)
RELOADS = _int("AIC_ACE_RELOADS", 10)
#: An Ace Run START that errors this many times in a game-day hands the account
#: back to fan farming until the reset, so a Grand-Live-specific refusal can
#: never park an account in the rotation's failure backoff all day.
MAX_FAILED_STARTS = 2


def enabled(viewer_id: int) -> bool:
    if (os.environ.get("AIC_ACE") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    only = (os.environ.get("AIC_ACE_ONLY") or "").strip()
    return not only or only == str(viewer_id)


def _path(settings: Settings) -> Path:
    return settings.state_dir / STORE_NAME


def _load(settings: Settings) -> dict:
    try:
        return json.loads(_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(settings: Settings, state: dict) -> None:
    p = _path(settings)
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("could not write %s (%s)", p.name, exc)


def period_key(now: float | None = None) -> str:
    """The Ace Run period a moment belongs to: the ISO week of its game-day, so a
    new week starts with Monday's game-day (Monday 15:00 UTC, the daily reset
    hour). ``AIC_ACE_PERIOD=day`` goes back to one per game-day."""
    day = day_key(now)
    if PERIOD == "day":
        return day
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _today(state: dict, viewer_id: int, now: float | None = None) -> dict:
    """This account's record for the current period (week). ``running`` (an Ace
    Run is in flight) survives the reset: a career started before it still has to
    be counted - and get its skills bought - when it ends after. ``failed`` counts
    per GAME-DAY, so a bad day parks the account until the next daily reset, not
    for the rest of the week."""
    rec = state.setdefault(str(viewer_id), {})
    period, day = period_key(now), day_key(now)
    if rec.get("period") != period:
        running = bool(rec.get("running"))
        rec.clear()
        rec.update(period=period, started=0, good=0, best=0, running=running)
    if rec.get("failed_day") != day:
        rec["failed_day"], rec["failed"] = day, 0
    return rec


def status(settings: Settings, viewer_id: int, now: float | None = None) -> dict:
    return dict(_today(_load(settings), viewer_id, now))


def wanted(settings: Settings, viewer_id: int, now: float | None = None,
           once: int = 0) -> bool:
    """Should this account's NEXT career be an Ace Run? ``once`` > 0 is a
    one-time job (roster ``ace_once``): that many FINISHED Ace Runs in total,
    whatever the day, instead of RUNS per game-day."""
    if not enabled(viewer_id):
        return False
    rec = status(settings, viewer_id, now)
    if int(rec.get("failed") or 0) >= MAX_FAILED_STARTS:
        return False
    if once:
        return once_done(settings, viewer_id) < once
    return rec["started"] < RUNS


#: Finished Ace Runs of one-time accounts, ``{viewer_id: count}``. Its own file:
#: ace_state.json's per-account records are wiped at every reset.
ONCE_NAME = "ace_once.json"


def _once_load(settings: Settings) -> dict:
    try:
        d = json.loads((settings.state_dir / ONCE_NAME).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def once_done(settings: Settings, viewer_id: int) -> int:
    try:
        return int(_once_load(settings).get(str(viewer_id)) or 0)
    except (TypeError, ValueError):
        return 0


def record_once_finish(settings: Settings, viewer_id: int) -> int:
    """Count one finished Ace Run toward a one-time job; returns the new total."""
    d = _once_load(settings)
    d[str(viewer_id)] = once_done(settings, viewer_id) + 1
    p = settings.state_dir / ONCE_NAME
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("could not write %s (%s)", p.name, exc)
    return int(d[str(viewer_id)])


def is_running(settings: Settings, viewer_id: int) -> bool:
    """Was the career this account is about to end started as an Ace Run?"""
    return bool(_load(settings).get(str(viewer_id), {}).get("running"))


def record_start(settings: Settings, viewer_id: int, now: float | None = None,
                 chara: int = 0) -> dict:
    state = _load(settings)
    rec = _today(state, viewer_id, now)
    rec["started"] += 1
    rec["running"] = True
    if chara:
        rec["charas"] = sorted(set(rec.get("charas") or []) | {int(chara)})
    _save(settings, state)
    return dict(rec)


# --------------------------------------------------------------------------- #
# which trainee
# --------------------------------------------------------------------------- #
#: rank_score an Ace Run trainee is expected to reach, to weigh a candidate
#: against the team's current members. MEASURED: the first Ace Run made 17,303.
EST_SCORE = 17_000
DIST_NAME = {1: "sprint", 2: "mile", 3: "medium", 4: "long", 5: "dirt"}


@lru_cache(maxsize=1)
def _aptitudes(mdb: str) -> dict[int, dict]:
    """``{card_id: {proper_*: aptitude}}`` from card_rarity_data. A card's
    aptitudes are the same at every star level (checked across the table)."""
    from .chores import DIST_FIELD, STYLE_FIELD
    fields = sorted(set(DIST_FIELD.values()) | set(STYLE_FIELD.values())
                    | {"proper_ground_turf", "proper_ground_dirt"})
    con = sqlite3.connect(f"file:{mdb}?mode=ro", uri=True)
    try:
        return {int(row[0]): dict(zip(fields, row[1:])) for row in con.execute(
            f"SELECT card_id, {', '.join(fields)} FROM card_rarity_data")}
    finally:
        con.close()


def card_aptitudes(mdb_path=None) -> dict[int, dict]:
    found = find_master_mdb(mdb_path)
    if not found:
        return {}
    try:
        return _aptitudes(str(found))
    except sqlite3.Error as exc:
        log.warning("could not read card aptitudes from %s (%s)", found, exc)
        return {}


def pick_trainee(settings: Settings, viewer_id: int, load_index: dict,
                 now: float | None = None, apts: dict | None = None,
                 exclude=(), reuse_week: bool = False) -> tuple[int, str]:
    """The owned trainee card whose Ace Run would strengthen Team Trials most.

    Never a character already on the team (a team fields each uma once, so
    another copy adds nothing), one already Ace-Run this week, one in ``exclude``
    (the Ace Run that just ended this visit - not on the team YET, since the
    team chore runs at the start of a visit), or one that cannot start an idle
    career (selection.unstartable_reason). Characters that already HAVE an ace
    (a trainee at MIN_SCORE+) come last, so each run makes a new character's
    ace. Each candidate is scored as a trainee at ``EST_SCORE`` with its card's
    base aptitudes, by the formula the team uses (chores.member_point): the gain
    is its whole point where a distance has a free slot, else what it adds over
    that distance's weakest member. Returns ``(card_id, why)``, or ``(0, "")``.
    """
    from .chores import DIST_FIELD, TEAM_UNITS, best_style, member_point
    from .selection import unstartable_reason
    from .state import read_account

    apts = card_aptitudes() if apts is None else apts
    owned = read_account(load_index).owned_card_ids
    trained = {int(c.get("trained_chara_id") or 0): c
               for c in load_index.get("trained_chara") or [] if isinstance(c, dict)}
    team_class = (load_index.get("team_stadium_user") or {}).get("team_class") or 1
    units = TEAM_UNITS.get(int(team_class), 3)

    on_team: set[int] = set()
    members: dict[int, list[int]] = {d: [] for d in DIST_FIELD}
    for slot in load_index.get("team_data_array") or []:
        c = trained.get(int(slot.get("trained_chara_id") or 0))
        dist = int(slot.get("distance_type") or 0)
        if not c or dist not in members:
            continue
        on_team.add(int(c.get("card_id") or 0) // 100)
        style = int(slot.get("running_style") or 0) or best_style(c)
        members[dist].append(member_point(c, dist, style))
    week = set() if reuse_week else set(status(settings, viewer_id, now).get("charas") or [])
    skip = week | {int(u) for u in exclude} | on_team
    has_ace = {int(c.get("card_id") or 0) // 100 for c in trained.values()
               if not c.get("owner_viewer_id")
               and int(c.get("rank_score") or 0) >= MIN_SCORE}

    best = (False, -10 ** 9, 0, "")          # (no ace yet, gain, card, why)
    for card in sorted(set(owned)):
        uma = card // 100
        if uma in skip or card not in apts or unstartable_reason(card):
            continue
        fresh = uma not in has_ace
        pseudo = {**apts[card], "rank_score": EST_SCORE}
        style = best_style(pseudo)
        for dist in DIST_FIELD:
            pt = member_point(pseudo, dist, style)
            have = members[dist]
            if len(have) < units:
                gain, how = pt, f"fills an open {DIST_NAME[dist]} slot"
            else:
                gain, how = pt - min(have), f"beats the weakest {DIST_NAME[dist]} member"
            if (fresh, gain) > best[:2]:
                best = (fresh, gain, card, f"{how} (est. {gain:+,} team points)"
                        + ("" if fresh else "; every fresher uma is used"))
    return best[2], best[3]


def clear_running(settings: Settings, viewer_id: int) -> None:
    """Forget an in-flight Ace Run that ended outside the bot."""
    state = _load(settings)
    rec = state.get(str(viewer_id))
    if rec and rec.get("running"):
        rec["running"] = False
        _save(settings, state)
        log.info("The Ace Run that was running is gone; not counting it.")


def record_failed_start(settings: Settings, viewer_id: int, now: float | None = None) -> dict:
    state = _load(settings)
    rec = _today(state, viewer_id, now)
    rec["failed"] = int(rec.get("failed") or 0) + 1
    _save(settings, state)
    if rec["failed"] >= MAX_FAILED_STARTS:
        log.warning("Ace Runs failed to start %s times today; fan farming until "
                    "the reset.", rec["failed"])
    return dict(rec)


def record_finish(settings: Settings, viewer_id: int, summary: dict,
                  now: float | None = None) -> dict:
    """Count a finished Ace Run. ``summary`` is career.end_career's."""
    state = _load(settings)
    rec = _today(state, viewer_id, now)
    rec["running"] = False
    try:
        score = int((summary or {}).get("rank_score") or 0)
    except (TypeError, ValueError):
        score = 0
    rec["best"] = max(int(rec.get("best") or 0), score)
    if score >= MIN_SCORE:
        rec["good"] += 1
    _save(settings, state)
    log.info("Ace Run finished: rank_score %s (%s) - %s/%s runs %s, %s ace(s), "
             "best %s.", f"{score:,}",
             "ACE" if score >= MIN_SCORE else f"below {MIN_SCORE:,}",
             rec["started"], RUNS, PER, rec["good"], f"{rec['best']:,}")
    return dict(rec)

"""When each account's career started, when it is due - written down and kept.

The rotation used to schedule on blind fixed slots: ``career_minutes / N`` apart,
round-robin, forever. It already *had* the real answer - :class:`aic.state.CareerState`
carries the server's own ``start_time``/``end_time`` for every career, and
``visit()`` reads it on every visit - and then threw it away for every roster
bigger than one. Three things went wrong as a result:

* **A slipped slot cost a whole career.** Arriving early found a career still
  running, so the visit did nothing - and that account was not due again for a
  full lap. It sat FINISHED and idle for up to 50 minutes, every lap, forever.
* **50 minutes was an assumption.** ``CAREER_MINUTES`` is a constant; if a real
  career runs 48 or 52, a fixed schedule drifts against reality in one direction
  and never recovers.
* **A restart forgot everything.** The slot origin was rebuilt from
  ``time.monotonic()``, which lines up with nothing that is already running.

So: record what the server said, per account, and schedule from that.

**Which clock to trust.** Not a local stopwatch, and not the server's ``end_time``
epoch either. ``CareerState.seconds_remaining`` is already corrected for a local
clock that disagrees with the server's (it measures "what was left when the
server told us" minus "how long we have run since"), so a deadline built from it
survives a wrong :class:`~aic.state.ServerClock` offset - which is a real risk,
since that offset is *derived* and can fall back to an assumed +3600.
``due_at`` is therefore local epoch, computed from ``seconds_remaining``. The
server's own timestamps are recorded beside it for reading, not for arithmetic.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .logging_setup import get

log = get("clock")

STORE_NAME = "career_clock.json"

#: Furthest ahead a due time is ever allowed to sit. A career is ~50 minutes; the
#: only legitimately long park is the fan cap waiting out one game-day. Past this
#: the reading is wrong - a bad ServerClock offset inflates seconds_remaining, and
#: an unbounded due_at would retire the account from the rotation for good.
MAX_DUE_AHEAD = 25 * 3600

#: Slack after a career's clock runs out before visiting, absorbing residual
#: client/server skew. Same value the single-account loop has always used.
FINISH_GRACE = 30.0


def store_path(settings: Settings) -> Path:
    return settings.state_dir / STORE_NAME


def _iso(epoch: float | None) -> str:
    """A readable UTC stamp, for the human reading the file. Never parsed back."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def read_all(settings: Settings) -> dict:
    path = store_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        log.warning("could not read %s (%s); scheduling from scratch.", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write(settings: Settings, store: dict) -> None:
    path = store_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # PID in the temp name: a fixed one is a collision between any two
        # writers, and the loser corrupts the winner's file.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # Losing this costs one misaligned lap after a restart, not correctness.
        log.warning("could not write %s (%s); a restart will not remember this "
                    "schedule.", path, exc)


def record(settings: Settings, viewer_id: int, *, remaining_seconds: float | None,
           started_at: float | None = None, finishes_at: float | None = None,
           source: str = "", label: str = "") -> float | None:
    """Write down where this account's career is, and return its local due time.

    ``remaining_seconds`` is the drift-safe measurement (see the module
    docstring); it is what ``due_at`` is built from. ``started_at`` /
    ``finishes_at`` are the server's own epochs, stored for reading.

    Returns the ``due_at`` it recorded, or ``None`` when there was no clock to
    record - a caller with no clock should fall back rather than invent one.
    """
    if not viewer_id:
        return None
    store = read_all(settings)
    previous = store.get(str(viewer_id))
    previous = previous if isinstance(previous, dict) else {}

    due_at = None
    if remaining_seconds is not None:
        # Bounded both ways. A negative reading is "due now"; an absurd one is a
        # bad clock, and parking an account 40 days out would retire it silently.
        ahead = min(max(0.0, float(remaining_seconds)) + FINISH_GRACE, MAX_DUE_AHEAD)
        due_at = time.time() + ahead
    else:
        # No clock this time (a failed visit). "We could not look" is not the same
        # as "there is nothing there": keep what was already known rather than
        # overwriting a live career's finish time with null, which a restart would
        # then read as "no record" and re-stagger.
        due_at = previous.get("due_at")
        if not isinstance(due_at, (int, float)):
            due_at = None
        started_at = started_at or None
        finishes_at = finishes_at or None

    entry = {
        "name": label,
        "trainer_id": str(viewer_id),
        # What the scheduler uses. Local epoch.
        "due_at": due_at,
        "due_at_utc": _iso(due_at),
        # What the server said. For reading, never for arithmetic.
        "career_started_at_utc": (_iso(started_at)
                                  or previous.get("career_started_at_utc", "")),
        "career_finishes_at_utc": (_iso(finishes_at)
                                   or previous.get("career_finishes_at_utc", "")),
        "duration_seconds": (round(finishes_at - started_at)
                             if started_at and finishes_at else None),
        "remaining_at_record": (round(remaining_seconds)
                                if remaining_seconds is not None else None),
        "recorded_at_utc": _iso(time.time()),
        "source": source,
    }
    store[str(viewer_id)] = entry
    _write(settings, store)
    return due_at


def due_at(settings: Settings, viewer_id: int, store: dict | None = None) -> float | None:
    """The recorded local due time for this account, or ``None`` if unknown.

    A due time more than a day old is discarded: it predates something that went
    unobserved (a long outage, a career ended by hand), and acting on it would
    mean stampeding the whole roster at once on the next start.
    """
    store = read_all(settings) if store is None else store
    entry = store.get(str(viewer_id))
    if not isinstance(entry, dict):
        return None
    value = entry.get("due_at")
    if not isinstance(value, (int, float)) or value != value:      # None / NaN
        return None
    now = time.time()
    if value < now - 86400:
        return None                       # predates something unobserved
    if value > now + MAX_DUE_AHEAD:
        return None                       # a clock this wrong is not a schedule
    return float(value)


def describe(settings: Settings, viewer_id: int, store: dict | None = None) -> str:
    """One line about this account's recorded career, for a plan or a status."""
    store = read_all(settings) if store is None else store
    entry = store.get(str(viewer_id))
    if not isinstance(entry, dict):
        return "no career recorded yet"
    started = entry.get("career_started_at_utc") or "?"
    finishes = entry.get("career_finishes_at_utc") or "?"
    return f"started {started}, finishes {finishes} ({entry.get('source') or '?'})"

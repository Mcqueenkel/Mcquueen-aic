"""Carats per account for the game-day, for the Discord career card.

Same shape as fancap's fan_day.json: each account's reading at the START of its
game-day (the previous day's last reading carried over, so carats claimed in the
first visit after the 15:00 UTC reset still count today) and its LATEST reading,
taken whenever a career is collected. ``total = free + paid``; paid carats are
real money and are only ever reported here, never spent (see flow._recover_tp).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .fancap import day_key
from .logging_setup import get

log = get("carats")

STORE_NAME = "carat_day.json"


def _path(settings) -> Path:
    return Path(settings.state_dir) / STORE_NAME


def _load(settings) -> dict:
    try:
        store = json.loads(_path(settings).read_text(encoding="utf-8"))
        return store if isinstance(store, dict) else {}
    except (OSError, ValueError):
        return {}


def record(settings, viewer_id: int, free: int, paid: int, day: str | None = None) -> None:
    """Note this account's carats now. Never raises."""
    if not viewer_id:
        return
    day = day or day_key()
    store = _load(settings)
    total = int(free) + int(paid)
    rec = store.get(str(viewer_id))
    if isinstance(rec, dict) and rec.get("day") == day:
        start = rec.get("start", total)
    else:
        # A new game-day starts from the previous day's last reading, if any.
        start = int(rec.get("total")) if isinstance(rec, dict) and isinstance(rec.get("total"), int) else total
    store[str(viewer_id)] = {"day": day, "start": int(start), "total": total,
                             "free": int(free), "paid": int(paid)}
    try:
        p = _path(settings)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("could not write %s (%s)", STORE_NAME, exc)


#: An account not read for this many game-days is off the roster; drop it.
KEEP_DAYS = 7


def day_rows(settings, day: str | None = None) -> list[tuple[int, int, int, int, int]]:
    """``[(viewer_id, total now, change today, free, paid)]``, most carats first.

    Every account read in the last KEEP_DAYS game-days: one not yet visited
    since the reset counts at its last reading with a change of 0, so the
    farm-wide total is the whole roster from the first post of the day."""
    day = day or day_key()
    oldest = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
    rows = []
    for viewer, rec in _load(settings).items():
        if not isinstance(rec, dict) or str(rec.get("day") or "") < oldest:
            continue
        try:
            total = int(rec["total"])
            change = total - int(rec.get("start", total)) if rec.get("day") == day else 0
            rows.append((int(viewer), total, change,
                         int(rec.get("free") or 0), int(rec.get("paid") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


if __name__ == "__main__":
    import tempfile
    from types import SimpleNamespace
    s = SimpleNamespace(state_dir=tempfile.mkdtemp())
    d1, d2 = "2026-10-01", "2026-10-02"
    record(s, 1, 1000, 50, day=d1)
    record(s, 1, 900, 50, day=d1)                       # TP refill spent 100
    assert day_rows(s, d1) == [(1, 950, -100, 900, 50)]
    record(s, 2, 10, 0, day=d1)
    assert [r[:3] for r in day_rows(s, d2)] == [(1, 950, 0), (2, 10, 0)]   # not visited yet: +0
    record(s, 1, 2000, 50, day=d2)                      # gift box claimed right after the reset
    assert day_rows(s, d2)[0] == (1, 2050, 1100, 2000, 50), day_rows(s, d2)
    record(s, 3, 5, 0, day=d2)                          # first-ever reading starts at itself
    assert [r[:3] for r in day_rows(s, d2)] == [(1, 2050, 1100), (2, 10, 0), (3, 5, 0)]
    assert [r[0] for r in day_rows(s, "2026-10-09")] == [1, 3]             # 2 unseen 8 days: dropped
    print("carats self-check ok")

"""Support-card fan-bonus data, read from the game's ``master.mdb``.

The "Fan Bonus" support-card effect (ファン数ボーナス) is **effect type 16** in
``support_card_effect_table`` - confirmed against the game's own effect-name
strings (``text_data`` category 151, where index 16 is "Fan Bonus"; the similar
index 15 is "Race Bonus", a different effect). Its value is defined at level
checkpoints (``init`` = level 1, then ``limit_lv5`` … ``limit_lv50``), with
``-1`` marking a level that is not a checkpoint. The in-between value is a linear
interpolation of the two surrounding checkpoints.

A card's *current* fan bonus depends on both its level and its uncap:

* ``support_card_limit`` gives the max level per ``(rarity, limit_break_count)``
  - SSR 30/35/40/45/50, SR 25/30/35/40/45, R 20/25/30/35/40.
* ``support_card_level`` maps cumulative ``total_exp`` to a level per rarity, so
  the owned card's ``exp`` resolves to its actual level (never above the cap its
  uncap allows).

Everything is cached to JSON so master.mdb is only needed once, exactly as
``racedata`` does it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import get
from .racedata import find_master_mdb  # generic master.mdb locator

log = get("supportdata")

#: The support-card effect type that is the Fan Bonus (verified via text_data).
FAN_BONUS_EFFECT_TYPE = 16

#: Effect-table columns and the level each represents.
_EFFECT_LEVELS = [1] + list(range(5, 55, 5))
_EFFECT_COLUMNS = ["init"] + [f"limit_lv{lv}" for lv in range(5, 55, 5)]

CACHE_NAME = "support_fan_bonus.json"


class SupportDataUnavailable(RuntimeError):
    """master.mdb could not be found or read, and no cache exists."""


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #

def _checkpoints(row: sqlite3.Row) -> list[list[int]]:
    """Defined (level, value) fan-bonus checkpoints; ``-1`` columns dropped."""
    out = []
    for lv, col in zip(_EFFECT_LEVELS, _EFFECT_COLUMNS):
        val = row[col]
        if val is not None and val >= 0:
            out.append([lv, int(val)])
    return out


def extract(mdb_path: Path) -> dict:
    """Pull fan-bonus curves, level caps and the exp→level table from the mdb.

    Copied to a temp file and opened read-only: the game may hold the original,
    and we must never write to it.
    """
    tmp = Path(tempfile.gettempdir()) / "aic_master_copy.mdb"
    try:
        if not tmp.exists() or tmp.stat().st_mtime < mdb_path.stat().st_mtime:
            shutil.copy2(mdb_path, tmp)
    except OSError as exc:
        raise SupportDataUnavailable(f"could not copy {mdb_path}: {exc}") from exc

    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SupportDataUnavailable(f"could not open {tmp}: {exc}") from exc

    con.row_factory = sqlite3.Row
    try:
        fan_bonus: dict[str, dict] = {}
        for r in con.execute(
                "SELECT d.id, d.rarity, e.* FROM support_card_data d "
                "JOIN support_card_effect_table e "
                "  ON e.id = d.effect_table_id AND e.type = ? ",
                (FAN_BONUS_EFFECT_TYPE,)):
            curve = _checkpoints(r)
            if curve:
                fan_bonus[str(r["id"])] = {"rarity": int(r["rarity"]), "curve": curve}

        level_caps = {
            str(r["rarity"]): [r["limit_0"], r["limit_1"], r["limit_2"],
                               r["limit_3"], r["limit_4"]]
            for r in con.execute("SELECT * FROM support_card_limit")
        }

        # Which character(s) each support card counts as, for the game's "no
        # two of the same uma" rule. Normally one, straight off the card row -
        # exact, unlike matching names, and it covers cards too new to be in
        # Icarus' support_list.json. ``support_card_group`` then adds the few
        # cards that count as SEVERAL characters at once: 30081 (Team Sirius,
        # 6 members) and 30067 (3 members) today.
        card_charas: dict[str, list[int]] = {}
        for r in con.execute("SELECT id, chara_id FROM support_card_data"):
            if r["chara_id"]:
                card_charas[str(r["id"])] = [int(r["chara_id"])]
        for r in con.execute("SELECT support_card_id, chara_id "
                             "FROM support_card_group"):
            bucket = card_charas.setdefault(str(r["support_card_id"]), [])
            if int(r["chara_id"]) not in bucket:
                bucket.append(int(r["chara_id"]))

        exp_levels: dict[str, list[list[int]]] = {}
        for r in con.execute("SELECT rarity, level, total_exp "
                             "FROM support_card_level ORDER BY rarity, level"):
            exp_levels.setdefault(str(r["rarity"]), []).append(
                [int(r["total_exp"] or 0), int(r["level"])])
    finally:
        con.close()

    return {
        "source": str(mdb_path),
        "fan_bonus": fan_bonus,
        "level_caps": level_caps,
        "exp_levels": exp_levels,
        "card_charas": card_charas,
    }


# --------------------------------------------------------------------------- #
# the data
# --------------------------------------------------------------------------- #

@dataclass
class SupportData:
    fan_bonus: dict = field(default_factory=dict)
    level_caps: dict = field(default_factory=dict)
    exp_levels: dict = field(default_factory=dict)
    #: support_card_id -> the character id(s) it counts as (usually one).
    card_charas: dict = field(default_factory=dict)
    source: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.fan_bonus and self.level_caps and self.exp_levels)

    def max_level(self, rarity: int, limit_break_count: int) -> int:
        caps = self.level_caps.get(str(int(rarity)))
        if not caps:
            return 0
        lb = max(0, min(int(limit_break_count or 0), len(caps) - 1))
        return int(caps[lb])

    def level_for(self, rarity: int, exp: int, limit_break_count: int) -> int:
        """Current level from cumulative exp, clamped to the uncap's cap."""
        table = self.exp_levels.get(str(int(rarity))) or []
        level = 1
        for total_exp, lv in table:
            if int(exp or 0) >= total_exp:
                level = lv
            else:
                break
        return min(level, self.max_level(rarity, limit_break_count) or level)

    def fan_bonus_at_level(self, card_id: int, level: int) -> float | None:
        """Interpolated fan-bonus percent for a card at a given level."""
        entry = self.fan_bonus.get(str(int(card_id)))
        if not entry:
            return None
        curve = entry["curve"]
        if not curve:
            return None
        if level < curve[0][0]:
            # BELOW the unlock level the effect does not exist yet. This read
            # `<=` and returned the unlock value, so a card whose Fan Bonus
            # unlocks at level 45 was scored its full value at level 1. 111 of
            # the 172 cards with a fan-bonus curve unlock above level 1 (at
            # 5/10/15/25/30/45), so the ranking credited phantom bonus on any
            # of them held under-levelled - exactly what a farm account is full
            # of - and `build_fan_deck` picked decks on those phantom numbers.
            return 0.0
        if level == curve[0][0]:
            return float(curve[0][1])
        if level >= curve[-1][0]:
            return float(curve[-1][1])
        for (l0, v0), (l1, v1) in zip(curve, curve[1:]):
            if l0 <= level <= l1:
                return v0 + (v1 - v0) * (level - l0) / (l1 - l0)
        return float(curve[-1][1])

    def card_fan_bonus(self, card_id: int, exp: int,
                       limit_break_count: int) -> tuple[int, float] | None:
        """``(level, fan_bonus_percent)`` for an owned card, or ``None``.

        ``None`` means the card has no Fan Bonus effect at all. The level and
        percent both reflect the card's actual exp and uncap.
        """
        entry = self.fan_bonus.get(str(int(card_id)))
        if not entry:
            return None
        level = self.level_for(entry["rarity"], exp, limit_break_count)
        pct = self.fan_bonus_at_level(card_id, level)
        return (level, pct) if pct is not None else None


def load(settings, mdb_path: str | os.PathLike | None = None,
         refresh: bool = False) -> SupportData:
    """Fan-bonus data, extracting from master.mdb if the cache is absent/stale."""
    cache = Path(settings.state_dir).parent / CACHE_NAME

    if not refresh and cache.is_file():
        try:
            with open(cache, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            data = SupportData(
                fan_bonus=raw.get("fan_bonus") or {},
                level_caps=raw.get("level_caps") or {},
                exp_levels=raw.get("exp_levels") or {},
                card_charas=raw.get("card_charas") or {},
                source=str(raw.get("source") or cache),
            )
            if data.usable:
                return data
            log.debug("cached support data at %s is incomplete; re-extracting", cache)
        except (OSError, ValueError, TypeError) as exc:
            log.debug("could not read %s (%s); re-extracting", cache, exc)

    found = find_master_mdb(mdb_path or os.environ.get("AIC_MASTER_MDB"))
    if not found:
        raise SupportDataUnavailable(
            "master.mdb not found. Support-card fan bonus cannot be computed "
            "without it. Point at it with the AIC_MASTER_MDB env var."
        )

    log.info("Extracting support-card fan bonus from %s ...", found)
    raw = extract(found)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        log.info("Cached support fan bonus to %s", cache)
    except OSError as exc:
        log.warning("could not cache support data (%s); will re-extract next run", exc)

    return SupportData(fan_bonus=raw["fan_bonus"], level_caps=raw["level_caps"],
                       exp_levels=raw["exp_levels"],
                       card_charas=raw.get("card_charas") or {},
                       source=raw["source"])

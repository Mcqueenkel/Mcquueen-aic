"""Race rules read from the game's ``master.mdb``.

An idle career's ``race_array`` is not free-form: each trainee has its own
**route**, and that route's goal races are mandatory. A saved in-game agenda is
generic - it is not bound to a trainee - so reusing one built for another
character produces a race list missing that trainee's goals, which the server
refuses with 205.

Everything needed to build a correct list per trainee is in ``master.mdb``:

    single_mode_chara_program   chara_id -> program_group(s) it may enter
    single_mode_program         program_id -> group, race_permission, month/half
    single_mode_route           chara_id -> race_set_id (its route)
    single_mode_route_race      race_set_id -> the goal races, by turn

``race_permission`` is an **enum**, not a bitmask (verified against 213 real
agenda entries, zero violations):

    1 -> junior only        {year 1}
    2 -> classic only       {year 2}
    3 -> classic or senior  {year 2, 3}
    4 -> senior only        {year 3}

Permission is necessary but not sufficient: a program also has to sit on a turn
the career can actually enter a race on. ``single_mode_turn.race_entry_type``
says which those are, and the junior year does not start in January - turn 1 is
**April**, and April to June are doubled (two turns per half-month) for the
pre-debut stretch. All four turn sets agree that year 1 blocks (4,1), (4,2),
(5,1), (5,2) and (6,1), and that years 2 and 3 block nothing. Verified against
13,804 entries from 442 real race lists with zero violations.

One junior program does sit at (6,1), so permission alone would let it through.

The extracted tables are cached as JSON so master.mdb is only needed once.
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

log = get("racedata")

#: Where the game keeps master.mdb, most likely first.
_MDB_CANDIDATES = (
    r"C:\Program Files (x86)\Steam\steamapps\common\UmamusumePrettyDerby"
    r"\UmamusumePrettyDerby_Data\Persistent\master\master.mdb",
    r"~\AppData\LocalLow\Cygames\Umamusume\master\master.mdb",
    # Steam on Linux (Proton): the install dir, then the wine-prefix LocalLow.
    "~/.local/share/Steam/steamapps/common/UmamusumePrettyDerby"
    "/UmamusumePrettyDerby_Data/Persistent/master/master.mdb",
    "~/.steam/steam/steamapps/common/UmamusumePrettyDerby"
    "/UmamusumePrettyDerby_Data/Persistent/master/master.mdb",
    "~/.local/share/Steam/steamapps/compatdata/3224770/pfx/drive_c/users"
    "/steamuser/AppData/LocalLow/Cygames/Umamusume/master/master.mdb",
)

#: race_permission -> the career years that program may be run in.
PERMISSION_YEARS = {1: {1}, 2: {2}, 3: {2, 3}, 4: {3}}

#: Turns per career year. 78 turns total: 24 junior, 24 classic, 24 senior,
#: then the finals.
TURNS_PER_YEAR = 24

#: How many races to schedule when filling. Fans come from racing, so more is
#: better for farming; captured player-built agendas run to about this size and
#: the server has accepted 43.
DEFAULT_RACE_TARGET = 35

#: route_race rows use condition_type 1 to mean "condition_id is a program id".
_CONDITION_TYPE_PROGRAM = 1

#: Route targets above this are scenario finals (URA etc.), not real programs.
_PSEUDO_PROGRAM_FLOOR = 10000

CACHE_NAME = "race_rules.json"


class RaceDataUnavailable(RuntimeError):
    """master.mdb could not be found or read, and no cache exists."""


def _steam_library_candidates() -> list[str]:
    """Steam-library copies of master.mdb on mounted drives.

    The game may live in a Steam library on a data drive rather than the OS
    disk - including the very drive this project sits on, which resolves the
    same way whether that drive is mounted on Linux or lettered on Windows.
    """
    suffix = ("SteamLibrary/steamapps/common/UmamusumePrettyDerby"
              "/UmamusumePrettyDerby_Data/Persistent/master/master.mdb")
    from .config import PROJECT_ROOT

    candidates = [str(PROJECT_ROOT.parent.parent / suffix)]
    import glob as _glob
    for root in ("/run/media/*/*", "/media/*/*", "/mnt/*"):
        candidates.extend(_glob.glob(f"{root}/{suffix}"))
    return candidates


def find_master_mdb(explicit: str | os.PathLike | None = None) -> Path | None:
    explicit = explicit or os.environ.get("AIC_MASTER_MDB")
    for candidate in ([explicit] if explicit else []) + list(_MDB_CANDIDATES) \
            + _steam_library_candidates():
        if not candidate:
            continue
        try:
            path = Path(candidate).expanduser()
        except RuntimeError:
            # A foreign-OS candidate ("~\AppData\..." read on Linux) parses as
            # ~unknown-user, which expanduser() raises on rather than skips.
            continue
        if path.is_file():
            return path
    return None


def tp_restore_items(mdb_path: str | os.PathLike | None = None) -> dict[int, int]:
    """``item id -> TP restored``, read from master.mdb.

    ``item_category = 20`` is the TP set; 21 is RP and must stay out of it (see
    the note on :data:`aic.config.TP_RESTORE`). Falls back to that static pair
    when master.mdb is unreadable, which is correct but misses event items.
    """
    from .config import TP_RESTORE

    found = find_master_mdb(mdb_path)
    if not found:
        return dict(TP_RESTORE)
    try:
        con = sqlite3.connect(f"file:{found}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id, effect_value_1 FROM item_data "
                "WHERE item_category = 20 AND effect_value_1 > 0").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("could not read TP items from %s (%s); using the built-in "
                    "list %s", found, exc, sorted(TP_RESTORE))
        return dict(TP_RESTORE)
    return {int(i): int(v) for i, v in rows} or dict(TP_RESTORE)


def year_for_turn(turn: int) -> int:
    """Career year a turn falls in, clamped to 3 (the finals count as senior)."""
    return max(1, min(3, (int(turn) - 1) // TURNS_PER_YEAR + 1))


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #

def extract(mdb_path: Path) -> dict:
    """Pull the four tables we need out of master.mdb.

    Copied to a temp file and opened read-only: the game may hold the original,
    and we must never write to it.
    """
    tmp = Path(tempfile.gettempdir()) / "aic_master_copy.mdb"
    try:
        if not tmp.exists() or tmp.stat().st_mtime < mdb_path.stat().st_mtime:
            shutil.copy2(mdb_path, tmp)
    except OSError as exc:
        raise RaceDataUnavailable(f"could not copy {mdb_path}: {exc}") from exc

    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RaceDataUnavailable(f"could not open {tmp}: {exc}") from exc

    con.row_factory = sqlite3.Row
    try:
        # fans = what winning pays (single_mode_fan_count order 1); need_fan =
        # the fan count required to enter at all. Both drive race selection:
        # more fans is the whole point of the schedule, and a race the trainee
        # cannot yet enter is a wasted slot.
        programs = {
            str(r["id"]): {
                "group": int(r["program_group"] or 0),
                "permission": int(r["race_permission"] or 0),
                "month": int(r["month"] or 0),
                "half": int(r["half"] or 0),
                "fans": int(r["win_fans"] or 0),
                "need_fan": int(r["need_fan_count"] or 0),
            }
            for r in con.execute(
                'SELECT p.id, p.program_group, p.race_permission, p.month, '
                '       p.half, p.need_fan_count, '
                '       (SELECT f.fan_count FROM single_mode_fan_count f '
                '        WHERE f.fan_set_id = p.fan_set_id AND f."order" = 1) '
                '       AS win_fans '
                'FROM single_mode_program p')
        }

        chara_groups: dict[str, list[int]] = {}
        for r in con.execute("SELECT chara_id, program_group, program_group_2 "
                             "FROM single_mode_chara_program"):
            bucket = chara_groups.setdefault(str(r["chara_id"]), [])
            for value in (r["program_group"], r["program_group_2"]):
                if value and value not in bucket:
                    bucket.append(int(value))

        routes = {str(r["chara_id"]): int(r["race_set_id"])
                  for r in con.execute(
                      "SELECT chara_id, race_set_id FROM single_mode_route "
                      "ORDER BY priority")}

        route_races: dict[str, list[dict]] = {}
        for r in con.execute(
                "SELECT race_set_id, turn, condition_type, condition_id "
                "FROM single_mode_route_race ORDER BY race_set_id, turn"):
            if int(r["condition_type"] or 0) != _CONDITION_TYPE_PROGRAM:
                continue
            pid = int(r["condition_id"] or 0)
            if not pid or pid >= _PSEUDO_PROGRAM_FLOOR:
                continue
            route_races.setdefault(str(r["race_set_id"]), []).append(
                {"program_id": pid, "turn": int(r["turn"] or 0)})

        # A slot counts as raceable if any turn landing on it allows race
        # entry. Stored as "year:month:half" so it survives a JSON round-trip.
        race_turns = sorted({
            f'{r["year"]}:{r["month"]}:{r["half"]}'
            for r in con.execute(
                "SELECT year, month, half FROM single_mode_turn "
                "WHERE race_entry_type != 0")
        })
    finally:
        con.close()

    return {
        "source": str(mdb_path),
        "programs": programs,
        "chara_groups": chara_groups,
        "routes": routes,
        "route_races": route_races,
        "race_turns": race_turns,
    }


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #

@dataclass
class RaceRules:
    programs: dict = field(default_factory=dict)
    chara_groups: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    route_races: dict = field(default_factory=dict)
    race_turns: list = field(default_factory=list)
    source: str = ""

    @property
    def usable(self) -> bool:
        if not (self.programs and self.routes and self.race_turns):
            return False
        # A cache written before fan data was extracted would silently make
        # every race look worth zero fans, so treat it as stale.
        sample = next(iter(self.programs.values()))
        return "fans" in sample and "need_fan" in sample

    def can_race_on(self, year: int, month: int, half: int) -> bool:
        """Whether a career can enter a race on this slot at all.

        Distinct from permission: the junior year's pre-debut turns accept no
        race entry regardless of which programs master.mdb lists there.
        """
        return f"{int(year)}:{int(month)}:{int(half)}" in self._raceable

    @property
    def _raceable(self) -> set:
        cached = getattr(self, "_raceable_set", None)
        if cached is None:
            cached = set(self.race_turns)
            object.__setattr__(self, "_raceable_set", cached)
        return cached

    def program(self, program_id: int) -> dict | None:
        return self.programs.get(str(int(program_id)))

    def allowed_years(self, program_id: int) -> set[int]:
        entry = self.program(program_id)
        if not entry:
            return set()
        return PERMISSION_YEARS.get(entry["permission"], set())

    def groups_for(self, chara_id: int) -> set[int]:
        """Program groups this character may enter. 0 is open to everyone."""
        return {0} | set(self.chara_groups.get(str(int(chara_id))) or [])

    def route_targets(self, chara_id: int) -> list[dict]:
        """This trainee's mandatory goal races, as ``{year, program_id}``.

        Empty for a character with no explicit route (the game gives those a
        default set we do not need to reproduce).

        A goal that offers a CHOICE is listed as several races on one turn -
        Daiwa Scarlet's "Japanese Oaks or Tokyo Yushun" (year 2, late May). Only
        the first (route order) is kept: one of them meets the goal, and two
        races on one turn is a bare 205 (MEASURED 2026-10-04: an Ace Run of
        card 100901 refused with both in a goals-only plan).
        """
        race_set = self.routes.get(str(int(chara_id)))
        if race_set is None:
            return []
        targets = []
        seen = set()
        turns_taken = set()
        for row in self.route_races.get(str(race_set)) or []:
            pid = row["program_id"]
            year = year_for_turn(row["turn"])
            # A program's own permission wins over the turn-derived year when
            # the two disagree (a goal race listed at a finals turn).
            allowed = self.allowed_years(pid)
            if allowed and year not in allowed:
                year = min(allowed)
            key = (year, pid)
            if key in seen:
                continue
            slot = self._slot(year, pid)
            if slot and slot in turns_taken:
                continue                  # the other branch of a choice goal
            seen.add(key)
            if slot:
                turns_taken.add(slot)
            targets.append({"year": year, "program_id": pid})
        return targets

    def is_legal(self, chara_id: int, year: int, program_id: int) -> bool:
        entry = self.program(program_id)
        if not entry:
            return False
        if year not in PERMISSION_YEARS.get(entry["permission"], set()):
            return False
        if not self.can_race_on(year, entry["month"], entry["half"]):
            return False
        return entry["group"] in self.groups_for(chara_id)

    def filter_races(self, chara_id: int, race_array) -> tuple[list[dict], list[dict]]:
        """Split a race list into ``(legal, rejected)`` for this trainee."""
        legal, rejected = [], []
        for entry in race_array or []:
            try:
                year = int(entry["year"])
                pid = int(entry["program_id"])
            except (KeyError, TypeError, ValueError):
                continue
            (legal if self.is_legal(chara_id, year, pid) else rejected).append(
                {"year": year, "program_id": pid})
        return legal, rejected

    def place(self, program_id: int) -> int:
        """The career year this program runs in, when that is unambiguous.

        Returns 0 when the program may be run in more than one year (a
        classic-or-senior race), so the caller has to say which.
        """
        entry = self.program(program_id)
        if not entry:
            return 0
        years = [y for y in PERMISSION_YEARS.get(entry["permission"], ())
                 if self.can_race_on(y, entry["month"], entry["half"])]
        return years[0] if len(years) == 1 else 0

    def fan_reward(self, program_id: int) -> int:
        """Fans awarded for winning this program."""
        entry = self.program(program_id)
        return int(entry["fans"]) if entry else 0

    def need_fans(self, program_id: int) -> int:
        """Fans the trainee must already have to enter."""
        entry = self.program(program_id)
        return int(entry["need_fan"]) if entry else 0

    def _slot(self, year: int, program_id: int):
        entry = self.program(program_id)
        if not entry:
            return None
        return (int(year), entry["month"], entry["half"])

    def fill_race_array(self, chara_id: int, races,
                        target: int = DEFAULT_RACE_TARGET,
                        protect=None) -> list[dict]:
        """Bring a race list to ``target`` with the best-paying legal races.

        ``protect`` is the set of ``(year, program_id)`` that must never be
        dropped - the trainee's route goals plus any hand-picked races. When
        omitted it defaults to this trainee's route goals, so a plain call still
        keeps every mandatory race. Everything *not* protected is optional: it
        is added to raise the fan haul and trimmed to hit the target.

        Slots are walked in career order and the highest-paying race the trainee
        can actually enter is taken in each. Chronological order is what makes
        ``need_fan_count`` meaningful: entry requirements are checked against the
        fans accumulated so far. At most one race per month/half, so two never
        collide on a turn.
        """
        if protect is None:
            protected = {(t["year"], t["program_id"])
                         for t in self.route_targets(chara_id)}
        else:
            protected = {(int(y), int(p)) for y, p in protect}
        chosen = [dict(r) for r in races]
        taken = {s for s in (self._slot(e["year"], e["program_id"])
                             for e in chosen) if s}

        groups = self.groups_for(chara_id)
        by_slot: dict[tuple, list[int]] = {}
        for pid_text, program in self.programs.items():
            if program["group"] not in groups:
                continue
            for year in PERMISSION_YEARS.get(program["permission"], ()):
                if not self.can_race_on(year, program["month"], program["half"]):
                    continue
                by_slot.setdefault(
                    (year, program["month"], program["half"]),
                    []).append(int(pid_text))

        def in_order(plan):
            return sorted(plan, key=lambda e: (self._slot(e["year"], e["program_id"])
                                               or (e["year"], 0, 0),
                                               e["program_id"]))

        def affordable(plan):
            """Whether every race can be entered by the time it comes round."""
            earned = 0
            for entry in in_order(plan):
                if self.need_fans(entry["program_id"]) > earned:
                    return False
                earned += self.fan_reward(entry["program_id"])
            return True

        # Fill EVERY free slot first, taking the best race affordable by then.
        # Filling to the target directly would starve year 3, because a
        # chronological walk reaches the target before it gets there - and
        # year 3 is where the 30,000-fan races are.
        earned = 0
        for slot in sorted(set(by_slot) | taken):
            if slot in taken:
                earned += max((self.fan_reward(e["program_id"]) for e in chosen
                               if self._slot(e["year"], e["program_id"]) == slot),
                              default=0)
                continue
            options = [pid for pid in by_slot.get(slot, [])
                       if self.need_fans(pid) <= earned]
            if not options:
                continue
            best = max(options, key=lambda p: (self.fan_reward(p), -p))
            taken.add(slot)
            chosen.append({"year": slot[0], "program_id": best})
            earned += self.fan_reward(best)

        # Then trim to the target, dropping the lowest-fan races first. Goals
        # and hand-picked races are never dropped. The only affordability
        # constraint kept here is that every GOAL race must still be enterable -
        # a career gains fans from training and events too, not just from the
        # scheduled races, so the pessimistic "funded only by earlier races"
        # model is right for *adding* a race but too strict for keeping the
        # count down. Optional races that end up unenterable simply drop out.
        def goals_enterable(plan):
            earned = 0
            for entry in in_order(plan):
                key = (entry["year"], entry["program_id"])
                if key in protected and self.need_fans(entry["program_id"]) > earned:
                    return False
                earned += self.fan_reward(entry["program_id"])
            return True

        droppable = sorted(
            (e for e in chosen if (e["year"], e["program_id"]) not in protected),
            key=lambda e: (self.fan_reward(e["program_id"]), -e["program_id"]))
        for entry in droppable:
            if len(chosen) <= target:
                break
            trial = [e for e in chosen if e is not entry]
            if goals_enterable(trial):
                chosen = trial

        return in_order(chosen)

    def build_race_array(self, chara_id: int, extra=None) -> list[dict]:
        """A race list valid for this trainee.

        Its mandatory goal races first, then any of ``extra`` (typically a saved
        agenda or hand-picked races) that is legal, not already present, **and
        does not land on a turn already taken**. Two races on one turn is a bare
        205 from the server - a saved agenda routinely collides with a route
        goal, which is exactly what refused an established account's start.
        """
        targets = self.route_targets(chara_id)
        chosen = {(t["year"], t["program_id"]) for t in targets}
        # Goal races claim their turns first and are never displaced.
        taken = {s for s in (self._slot(t["year"], t["program_id"])
                             for t in targets) if s}
        result = list(targets)

        legal, _ = self.filter_races(chara_id, extra or [])
        for entry in legal:
            key = (entry["year"], entry["program_id"])
            if key in chosen:
                continue
            slot = self._slot(entry["year"], entry["program_id"])
            if slot and slot in taken:
                continue          # that turn already has a race
            chosen.add(key)
            if slot:
                taken.add(slot)
            result.append(entry)

        result.sort(key=lambda e: (e["year"], e["program_id"]))
        return result


# --------------------------------------------------------------------------- #
# loading / caching
# --------------------------------------------------------------------------- #

def load(settings, mdb_path: str | os.PathLike | None = None,
         refresh: bool = False) -> RaceRules:
    """Race rules for this profile, extracting from master.mdb if needed.

    The JSON cache means master.mdb only has to be present once - useful since
    it lives in the Steam install and moves with the game.
    """
    cache = Path(settings.state_dir).parent / CACHE_NAME

    if not refresh and cache.is_file():
        try:
            with open(cache, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            rules = RaceRules(**{k: raw.get(k) or {} for k in
                                 ("programs", "chara_groups", "routes", "route_races")},
                              race_turns=raw.get("race_turns") or [],
                              source=str(raw.get("source") or cache))
            if rules.usable:
                return rules
            # ``usable`` also covers a cache written before race_turns existed,
            # which would otherwise silently keep allowing pre-debut turns.
            log.debug("cached race rules at %s are incomplete; re-extracting", cache)
        except (OSError, ValueError, TypeError) as exc:
            log.debug("could not read %s (%s); re-extracting", cache, exc)

    found = find_master_mdb(mdb_path or os.environ.get("AIC_MASTER_MDB"))
    if not found:
        raise RaceDataUnavailable(
            "master.mdb not found. Race lists cannot be built for the trainee "
            "without it.\n"
            "  Looked in:\n    " + "\n    ".join(_MDB_CANDIDATES) + "\n"
            "  Point at it with --master-mdb PATH or the AIC_MASTER_MDB env var."
        )

    log.info("Extracting race rules from %s ...", found)
    raw = extract(found)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        log.info("Cached race rules to %s", cache)
    except OSError as exc:
        log.warning("could not cache race rules (%s); will re-extract next run", exc)

    return RaceRules(
        programs=raw["programs"], chara_groups=raw["chara_groups"],
        routes=raw["routes"], route_races=raw["route_races"],
        race_turns=raw["race_turns"], source=raw["source"],
    )

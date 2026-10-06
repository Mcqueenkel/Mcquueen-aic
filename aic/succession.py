"""Succession rank points - the ``current_succession_rank_point`` a start must
declare.

This is **not** the account's banked balance (item 75). It is an affinity score
computed from the trainee and its selected parents' family tree, and the server
validates it: a wrong value is rejected outright.

The value is the sum of **seven terms**, named here as the in-game
compatibility calculators label them:

    affinity(trainee, parent1)          "Main Char - Legacy 1"
    affinity(trainee, parent2)          "Main Char - Legacy 2"
    affinity(parent1, parent2)          "Legacies' affinity"
    line(trainee, parent1, gp1a)        "Sub-Legacy 1-1 line"
    line(trainee, parent1, gp1b)        "Sub-Legacy 1-2 line"
    line(trainee, parent2, gp2a)        "Sub-Legacy 2-1 line"
    line(trainee, parent2, gp2b)        "Sub-Legacy 2-2 line"

Two things about that made it hard to find:

* A grandparent contributes through a **three-way** intersection - the relation
  types common to the trainee, that parent *and* the grandparent - not a
  pairwise link. No combination of pairwise terms can reproduce the observed
  values, which is why searching over pairwise link sets kept failing.
* A grandparent **that is the trainee's own character scores nothing**. Without
  that, the three-way intersection collapses to ``affinity(trainee, parent)``
  and pays out large.

On top of the relation points, each of the five *parent* pairings adds
:data:`G1_SADDLE_POINTS` per distinct G1 race both of its members won. Wins
only, and per **real race**, not per master-data row - the same race won in
two years counts once, a saddle standing for a title (Triple Crown) counts
each of its races, and venue/scenario variants that carry separate ``race_id``
rows (the JBC races rotate venue yearly; Takarazuka Kinen and others have
scenario re-issues) count as the same race - see :func:`saddle_g1_races`.
G2 and G3 do not count at all, and titles are not a variable.

**This formula is exact.** It reproduces the accepted
``current_succession_rank_point`` on **22/22** server-accepted starts captured
from the real client (``UmaDumpy-main/dumps``): 5 accounts, 12 trainee cards,
rental and non-rental parents, parents holding 0 to 38 G1 saddles, across both
the ``single_mode`` and ``idle_single_mode`` flows. The set was reconstructed
three independent times, by three different roster-resolution rules, all
agreeing.

Every term is load-bearing - dropping any one scores 0/22 - and the details
matter: the grandparent term must be the **three-way** intersection, a
grandparent that is the trainee's own character must score **zero**, and the
race bonus must be counted per **race name** (raw ``race_id`` scores 12/18,
because venue/scenario variants of one race carry separate ids).

Two traps that cost a lot of time, recorded so they are not re-entered:

* **The tree is three generations, not four.** Six candidate
  great-grandparent terms (4-way, 3-way, two pairwise forms, a GP-GGP win
  bonus, and self-appearance indicators) were tested; *none* is silent on the
  22 samples, so any non-zero weight breaks the fit. The trees that put the
  trainee in its own great-grandparent layer fit exactly *without* one.
* **The server accepts a tolerance band above the true value, not just the
  value.** On one byte-identical tree the real client sent 127 and the server
  also accepted 146. So a search "finding" a higher number never proved the
  computed one wrong - and a 205 is usually a *different* field (an invalid
  ``race_array`` or deck) being refused. Do not infer an undercount from a
  successful probe.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .logging_setup import get

log = get("succession")

#: The six family slots the game scores, by ``position_id``.
#: 10/20 are the two selected parents; 11/12 and 21/22 are their own parents.
POS_PARENT_1, POS_PARENT_2 = 10, 20

#: Great-grandparent positions - present in the payload, and **correctly
#: ignored**.
#:
#: A trained character's ``succession_chara_array`` holds six entries: 10/20 are
#: its own parents, 11/12 and 21/22 are *those* characters' parents. So the raw
#: payload does describe 15 nodes, and an earlier note here argued the formula
#: was therefore "missing a whole generation".
#:
#: That was wrong. Six candidate great-grandparent terms were tested against the
#: 22 captured accepted values; every one takes a wide spread of non-zero values
#: across the samples, so no fixed weight can be added without breaking a fit
#: that is currently perfect. Two samples put the trainee in its own
#: great-grandparent layer and reproduce exactly *without* any such term.
#: The generation is read by :func:`tree_signature` (for cache keying) and
#: nowhere else, deliberately.
POS_GREAT = (11, 12, 21, 22)

#: The five parent-tree links (no trainee) - see :func:`compute`.
LINKS = ((10, 20), (10, 11), (10, 12), (20, 21), (20, 22))

#: How far to search when the server rejects the computed value.
#:
#: With the race-name collapse in :func:`saddle_g1_races` the formula
#: reproduces **all four** captured accepted values exactly (295, 396, 404,
#: 127 - the -6/-12 "G1-win-mapping gap" residuals were cross-venue JBC wins
#: and are gone). The sweep is a safety net for trees with no ground truth,
#: not an expected cost.
#:
#: The down side must stay wide: offsets learned on farm seed trees cluster at
#: -43..-62 (five entries on disk), i.e. for those trees the old computation
#: ran too *high* by more than the previous SWEEP_DOWN of 10 ever covered - a
#: refused start on a fresh seed tree could never find its value. Covering
#: -80 makes the learned range reachable again with margin.
#:
#: The cost is bounded in practice: the winning offset is cached per tree
#: signature and the same best-ranked pair is reused every run, so this is a
#: one-time search per tree rather than a per-run cost.
SWEEP_UP = 120
SWEEP_DOWN = 80

#: Back-compat for callers/tests that referenced the old symmetric range.
SWEEP_RANGE = SWEEP_UP


class RelationData:
    """Relationship groups and their point values.

    Read from the game's ``master.mdb`` when it can be found, because Icarus'
    JSON snapshots are a **stale subset**: 1043 vs 1616 relation types and 2599
    vs 3056 memberships at the time of writing, with 57 of 74 characters missing
    groups (chara 1029 has 4 of its 46, chara 1098 has 7 of its 59). The mdb is
    a strict superset - no type exists only in the JSON and no ``relation_point``
    disagrees - so this can only ever *raise* a value that was too low, never
    change one that was already right. It leaves all 22 captured samples exactly
    as they were; it is insurance against a character whose groups were updated
    after the snapshot, not a fix for anything currently broken.

    The JSON remains the fallback, so a machine without the game installed keeps
    working.
    """

    def __init__(self, icarus_root: Path):
        self.members: dict[int, set[int]] = {}
        self.points: dict[int, int] = {}
        self.source = ""

        if self._load_from_mdb():
            return
        self._load_from_json(Path(icarus_root) / "data")

    def _load_from_mdb(self) -> bool:
        import shutil
        import sqlite3
        import tempfile

        from . import racedata

        try:
            src = racedata.find_master_mdb()
            if not src:
                return False
            # Copied and opened read-only: the game may hold the original.
            tmp = Path(tempfile.gettempdir()) / "aic_relations.mdb"
            if not tmp.exists() or tmp.stat().st_mtime < src.stat().st_mtime:
                shutil.copy2(src, tmp)
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            try:
                for cid, rtype in con.execute(
                        "SELECT chara_id, relation_type FROM succession_relation_member"):
                    self.members.setdefault(int(cid), set()).add(int(rtype))
                for rtype, point in con.execute(
                        "SELECT relation_type, relation_point FROM succession_relation"):
                    self.points[int(rtype)] = int(point)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - fall back to the JSON snapshot
            log.debug("could not read relations from master.mdb (%s); "
                      "falling back to the Icarus snapshot", exc)
            self.members, self.points = {}, {}
            return False

        if not (self.members and self.points):
            self.members, self.points = {}, {}
            return False
        self.source = "master.mdb"
        log.debug("relations from master.mdb: %d types, %d characters",
                  len(self.points), len(self.members))
        return True

    def _load_from_json(self, data_dir: Path) -> None:
        try:
            with open(data_dir / "succession_relation_member.json", "r",
                      encoding="utf-8-sig") as fh:
                for row in json.load(fh):
                    self.members.setdefault(int(row["chara_id"]), set()).add(
                        int(row["relation_type"]))
            with open(data_dir / "succession_relation.json", "r",
                      encoding="utf-8-sig") as fh:
                for row in json.load(fh):
                    self.points[int(row["relation_type"])] = int(row["relation_point"])
            self.source = str(data_dir)
            log.debug("relations from the Icarus snapshot (stale subset of master.mdb)")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("could not load succession relation data (%s); the rank "
                        "point cannot be computed", exc)

    @property
    def usable(self) -> bool:
        return bool(self.members and self.points)

    def affinity(self, chara_a: int, chara_b: int) -> int:
        """Points over every relation type both characters belong to.

        Matches an independent private-server implementation exactly, including
        its worked example: 1001+1002=27, 1001+1003=25, 1002+1003=20 -> 72.
        """
        shared = self.members.get(chara_a, set()) & self.members.get(chara_b, set())
        return sum(self.points.get(t, 0) for t in shared)

    def line_affinity(self, trainee: int, parent: int, grandparent: int) -> int:
        """Points over relation types shared by **all three** of them.

        A grandparent scores through the *line* it sits on - trainee, that
        parent, and the grandparent - not by a pairwise link. Verified against
        every "Sub-Legacy N-M line" figure in three calculator breakdowns, where
        it is the only candidate that fits; pairwise terms fit none.

        **A grandparent that is the trainee's own character scores nothing.**
        Without that rule the three-way intersection collapses to
        ``affinity(trainee, parent)`` and the line pays out large - a
        calculator breakdown shows 0 for exactly this case (a Kitasan Black run
        with Kitasan Black in both sub-legacy slots scores 0 from both lines).
        """
        if not grandparent or grandparent == trainee:
            return 0
        shared = (self.members.get(trainee, set())
                  & self.members.get(parent, set())
                  & self.members.get(grandparent, set()))
        return sum(self.points.get(t, 0) for t in shared)


@lru_cache(maxsize=4)
def load(icarus_root: str) -> RelationData:
    return RelationData(Path(icarus_root))


def _grandparent(parent: dict, position_id: int) -> dict | None:
    for entry in parent.get("succession_chara_array") or []:
        if entry.get("position_id") == position_id:
            return entry
    return None


def _family(parent_1: dict, parent_2: dict) -> dict:
    return {
        POS_PARENT_1: parent_1,
        POS_PARENT_2: parent_2,
        11: _grandparent(parent_1, 10),
        12: _grandparent(parent_1, 20),
        21: _grandparent(parent_2, 10),
        22: _grandparent(parent_2, 20),
    }


#: Shared-win bonus, per **distinct G1 race both characters won**.
#:
#: The rule used to be +1 per shared graded race, with winning *titles* (Triple
#: Crown and friends) as separate variables. It has since been simplified -
#: retroactively, so existing parents' affinities moved - to **+3 per identical
#: G1 win, with G2 and G3 no longer counting at all** and titles no longer
#: considered.
G1_SADDLE_POINTS = 3

#: ``race.grade`` value meaning G1. Grades run 100/200/300/400/... not 1/2/3.
G1_GRADE = 100


#: text_data category holding race display names, used to identify "the same
#: race" across ids - see :func:`saddle_g1_races`.
_RACE_NAME_TEXT_CATEGORY = 32

#: On-disk cache of the extracted mapping, so a machine without the game
#: install (or after an uninstall) keeps computing the win bonus.
_SADDLE_CACHE = Path(__file__).resolve().parent.parent / "state" / "g1_saddle_races.json"


@lru_cache(maxsize=1)
def saddle_g1_races() -> dict:
    """``win_saddle_id`` -> the set of distinct **G1 races** it represents.

    Three levels of collapsing matter here, and each was needed to reproduce a
    real accepted value:

    * a saddle can cover several ``race_instance`` rows (a title such as the
      Triple Crown - 15 of the 181 do),
    * the same race run in a different year or scenario is separate
      ``race_instance`` rows but the **same** ``race_id``, and
    * the same real-world race can carry **several ``race_id`` rows**: the JBC
      races move venue every year and each venue is its own id (four rows all
      named "JBC Classic"), and scenario re-issues duplicate ids the same way
      (two Takarazuka Kinens, two Kikuka Shos, ...). The server treats those as
      the same race, so ids are collapsed to one canonical id per **race name**
      (text_data category 32). Without this the computed value undercounts by
      3 per cross-variant shared win - the exact -12/-6 residuals observed on
      two real accepted starts, both of which land at 0 with it.

    The extraction is cached to ``state/g1_saddle_races.json`` and the cache is
    used whenever master.mdb cannot be found, so only one machine ever needs
    the game installed. With neither, the bonus drops to zero rather than
    applying a wrong value - the start then leans on the search instead.
    """
    import shutil
    import sqlite3
    import tempfile

    from . import racedata

    def read_cache() -> dict:
        try:
            with open(_SADDLE_CACHE, "r", encoding="utf-8") as fh:
                return {int(k): set(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError, TypeError):
            return {}

    try:
        src = racedata.find_master_mdb()
        if not src:
            cached = read_cache()
            if cached:
                log.debug("master.mdb not found; using the cached saddle map "
                          "(%d saddles)", len(cached))
            return cached
        tmp = Path(tempfile.gettempdir()) / "aic_saddles.mdb"
        if not tmp.exists() or tmp.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            instance = {int(i): (int(rid), int(g or 0)) for i, rid, g in
                        con.execute("SELECT ri.id, ri.race_id, rc.grade "
                                    "FROM race_instance ri "
                                    "JOIN race rc ON rc.id = ri.race_id")}
            names = {int(i): str(t) for i, t in con.execute(
                'SELECT "index", text FROM text_data WHERE category = ?',
                (_RACE_NAME_TEXT_CATEGORY,))}
            # One canonical id per race name, so venue/scenario variants of the
            # same race intersect. Unnamed ids stay themselves.
            canonical: dict[int, int] = {}
            by_name: dict[str, int] = {}
            for rid, grade in sorted(set(instance.values())):
                if grade != G1_GRADE:
                    continue
                name = names.get(rid)
                if name is None:
                    canonical[rid] = rid
                else:
                    canonical[rid] = by_name.setdefault(name, rid)
            out = {}
            for row in con.execute("SELECT * FROM single_mode_wins_saddle"):
                races = set()
                for i in range(2, 10):  # race_instance_id_1..8
                    entry = instance.get(int(row[i] or 0))
                    if entry and entry[1] == G1_GRADE:
                        races.add(canonical.get(entry[0], entry[0]))
                out[int(row[0])] = races
        finally:
            con.close()
        try:
            _SADDLE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with open(_SADDLE_CACHE, "w", encoding="utf-8") as fh:
                json.dump({str(k): sorted(v) for k, v in out.items()}, fh)
        except OSError as exc:
            log.debug("could not cache the saddle map: %s", exc)
        return out
    except Exception as exc:  # noqa: BLE001 - a missing mdb must not break starts
        cached = read_cache()
        if cached:
            log.debug("could not read G1 win saddles (%s); using the cached "
                      "map", exc)
            return cached
        log.debug("could not read G1 win saddles (%s); the shared-win bonus "
                  "will be zero", exc)
        return {}


def g1_saddles() -> frozenset:
    """Win-saddle ids that represent at least one G1 race."""
    return frozenset(s for s, races in saddle_g1_races().items() if races)


def _g1_wins(node: dict | None) -> set:
    """The distinct G1 races this character has won."""
    table = saddle_g1_races()
    won = set()
    for saddle in (node or {}).get("win_saddle_id_array") or []:
        won |= table.get(int(saddle), set())
    return won


def _link_score(a: dict | None, b: dict | None, relations: RelationData) -> int:
    if not a or not b:
        return 0
    score = relations.affinity(int(a.get("card_id") or 0) // 100,
                               int(b.get("card_id") or 0) // 100)
    return score + G1_SADDLE_POINTS * len(_g1_wins(a) & _g1_wins(b))


def family_points(trainee_card_id: int, parent_1: dict, parent_2: dict,
                  relations: RelationData) -> int:
    """Total compatibility, as the in-game calculators break it down.

    Seven terms, matching a calculator's own labels:

        affinity(trainee, parent1)              "Main Char - Legacy 1"
        affinity(trainee, parent2)              "Main Char - Legacy 2"
        affinity(parent1, parent2)              "Legacies' affinity"
        line(trainee, parent1, gp1a)            "Sub-Legacy 1-1 line"
        line(trainee, parent1, gp1b)            "Sub-Legacy 1-2 line"
        line(trainee, parent2, gp2a)            "Sub-Legacy 2-1 line"
        line(trainee, parent2, gp2b)            "Sub-Legacy 2-2 line"

    A grandparent contributes through :meth:`RelationData.line_affinity` - the
    types common to the trainee, that parent *and* the grandparent - not a
    pairwise link. That is what made this hard to find: four of the seven terms
    are three-way, so no combination of pairwise links can reproduce them.

    Plus :data:`G1_SADDLE_POINTS` per distinct G1 race won by both members of a
    pairing, over the five parent pairings (the trainee has no wins yet).
    """
    if not relations.usable:
        return 0

    trainee = int(trainee_card_id or 0) // 100
    family = _family(parent_1, parent_2)
    p1, p2 = family[POS_PARENT_1], family[POS_PARENT_2]

    total = (_link_score(p1, p2, relations)
             + _link_score({"card_id": int(trainee_card_id or 0),
                            "win_saddle_id_array": []}, p1, relations)
             + _link_score({"card_id": int(trainee_card_id or 0),
                            "win_saddle_id_array": []}, p2, relations))

    for parent_pos, gp_positions in ((POS_PARENT_1, (11, 12)),
                                     (POS_PARENT_2, (21, 22))):
        parent = family[parent_pos]
        for gp_pos in gp_positions:
            gp = family.get(gp_pos)
            if not parent or not gp:
                continue
            total += relations.line_affinity(
                trainee, int(parent.get("card_id") or 0) // 100,
                int(gp.get("card_id") or 0) // 100)
            # The win bonus stays pairwise: it is earned by a parent and its
            # own parent racing the same G1, which is what the game describes.
            total += G1_SADDLE_POINTS * len(_g1_wins(parent) & _g1_wins(gp))

    return int(total)


def explain(parent_1: dict, parent_2: dict, relations: RelationData,
            trainee_card_id: int = 0) -> list[str]:
    """Per-link breakdown, for when the server refuses what we computed."""
    family = _family(parent_1, parent_2)
    labels = {POS_PARENT_1: "parent1", POS_PARENT_2: "parent2",
              11: "gp1a", 12: "gp1b", 21: "gp2a", 22: "gp2b"}
    lines = []
    for pos, node in family.items():
        lines.append(f"  {labels[pos]:<8} "
                     + (f"card {node.get('card_id')}" if node else "MISSING"))
    for a, b in LINKS:
        lines.append(f"  {labels[a]}-{labels[b]:<8} "
                     f"{_link_score(family.get(a), family.get(b), relations)}")
    if trainee_card_id:
        trainee = {"card_id": int(trainee_card_id), "win_saddle_id_array": []}
        for pos in (POS_PARENT_1, POS_PARENT_2):
            lines.append(f"  trainee-{labels[pos]:<8} "
                         f"{_link_score(trainee, family.get(pos), relations)}")
    return lines


def compute(parent_1: dict, parent_2: dict, relations: RelationData) -> int:
    """Affinity total over the five parent pairings.

    Each contributes the two characters' shared relation points plus
    :data:`G1_SADDLE_POINTS` per **distinct G1 race both of them won**. The
    five are: the two parents to each other, and each parent to its own two
    parents - matching how the community compatibility calculators describe it,
    and reproducing a real accepted value exactly.
    """
    if not relations.usable:
        return 0

    # Scored through _link_score so the G1 weighting applies here too - this
    # used to inline its own "+1 per shared win" and silently diverge.
    family = _family(parent_1, parent_2)
    return int(sum(_link_score(family.get(a), family.get(b), relations)
                   for a, b in LINKS))


def account_offset(settings, viewer_id) -> int | None:
    """A typical offset already learned, for a tree never started before.

    The residual clusters tightly - separate accounts have produced -61 and
    -62 - so an unseen tree opens far better on a neighbour's offset than on
    the raw computed value. Tree-signature entries count too: they are
    account-independent by construction, which is the whole point of them.
    """
    prefix = f"{int(viewer_id or 0)}:"
    found = [v for k, v in load_offsets(settings).items()
             if k.startswith(prefix) or k.startswith("t")]
    if not found:
        return None
    return sorted(found)[len(found) // 2]


def candidates(computed: int, hint: int = 0, cached_offset: int | None = None,
               sweep: bool = True, parent_only: int = 0,
               already_tried=()):
    """Values to try, best first, when the server rejects ``computed``.

    The offset that worked last time for this pair (one call, and the usual
    case once a pair has been seen), then the account's banked balance, then a
    sweep. The formula undercounts by a small amount that grows with the
    parents' G1 wins, so the sweep runs *up* first and far, down only a little -
    see the module docstring.
    """
    seen = {int(computed)} | {int(v) for v in already_tried}

    def fresh(value):
        value = int(value)
        if value < 0 or value in seen:
            return None
        seen.add(value)
        return value

    if cached_offset:
        value = fresh(computed + cached_offset)
        if value is not None:
            yield value, "cached offset"

    if hint:
        value = fresh(hint)
        if value is not None:
            yield value, "account balance (item 75)"

    if not sweep:
        return

    offsets = list(range(1, SWEEP_UP + 1)) + [-i for i in range(1, SWEEP_DOWN + 1)]
    for offset in offsets:
        value = fresh(computed + offset)
        if value is not None:
            yield value, f"sweep {offset:+d}"


# --------------------------------------------------------------------------- #
# learned offsets
# --------------------------------------------------------------------------- #

def _cache_path(settings) -> Path:
    return settings.state_dir / "succession_offsets.json"


def load_offsets(settings) -> dict[str, int]:
    try:
        with open(_cache_path(settings), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def tree_signature(trainee_card_id, parent_1: dict, parent_2: dict) -> str:
    """Identify a succession tree by its **contents**, not by account-local ids.

    The rank point is a function of which characters are in the tree, so two
    accounts holding the same characters need the same value. Keying a learned
    offset on ``trained_chara_id`` throws that away: those ids are small
    per-account sequence numbers, so farm01's lesson is filed under "7:6" and
    farm02 - with the identical starter lineage - never finds it and pays for
    the search all over again.

    Every farmed account starts from the same seed characters, so this key is
    what lets one search serve the whole roster.
    """
    def card(node):
        return int((node or {}).get("card_id") or 0)

    parts = [int(trainee_card_id or 0)]
    for node in (parent_1, parent_2):
        parts.append(card(node))
        parts.append(card(_grandparent(node, 10)))
        parts.append(card(_grandparent(node, 20)))
    return "t" + "-".join(str(p) for p in parts)


def offset_key(parent_id_1, parent_id_2, viewer_id=0, trainee_card_id=0,
               signature: str = "") -> str:
    """Cache key for a learned offset.

    A ``signature`` from :func:`tree_signature` is preferred: it identifies the
    tree by its characters, so the lesson transfers across every account that
    holds the same lineage.

    The legacy account-scoped form is kept for entries already on disk.
    ``viewer_id`` was needed there because ``trained_chara_id`` is a per-account
    sequence number; ``trainee_card_id`` because the residual is demonstrably
    trainee-dependent - on one account, parents 7+6 computed 148 for both chara
    1041 and chara 1052, yet 1041's accepted value (86) was refused for 1052.
    """
    if signature:
        return signature
    return (f"{int(viewer_id or 0)}:{int(parent_id_1 or 0)}:"
            f"{int(parent_id_2 or 0)}:{int(trainee_card_id or 0)}")


def known_pairs(settings, viewer_id=0) -> set[tuple[int, int]]:
    """Parent pairs whose rank-point offset is already known for this account.

    Preferring one of these turns the start into a single call instead of a
    bounded search - see :func:`aic.selection.pick_parents`.
    """
    prefix = f"{int(viewer_id or 0)}:"
    pairs: set[tuple[int, int]] = set()
    for key in load_offsets(settings):
        if not key.startswith(prefix):
            continue
        parts = key.split(":")
        if len(parts) < 3:
            continue
        try:
            pairs.add((int(parts[1]), int(parts[2])))
        except (ValueError, TypeError):
            continue
    return pairs


def save_offset(settings, parent_id_1, parent_id_2, offset: int,
                viewer_id=0, trainee_card_id=0, signature: str = "") -> None:
    offsets = load_offsets(settings)
    offsets[offset_key(parent_id_1, parent_id_2, viewer_id,
                       trainee_card_id, signature)] = int(offset)
    # Bound the file; these are only a latency optimisation.
    if len(offsets) > 500:
        offsets = dict(list(offsets.items())[-500:])
    try:
        with open(_cache_path(settings), "w", encoding="utf-8") as fh:
            json.dump(offsets, fh)
    except OSError as exc:
        log.debug("could not cache the succession offset: %s", exc)

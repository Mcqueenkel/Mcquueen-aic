"""An explicit --races list is the schedule; the saved agenda does not join it.

THE BUG, measured live 2026-09-05: one identical 43-pick spec deployed to 14
accounts produced schedules of 44, 45, 46, 47 and 52 races. Every account holds
8 saved race agendas in-game, and one of them was being merged into the built
array alongside the picks - so "run the same schedule everywhere" was impossible
by construction, and careers ran races the operator never chose.

Second bug pinned here: the "legal agenda extras" counter was
``len(built) - len(targets) - len(picked)``, which reports NEGATIVE numbers
(-5, -4 and -2 all appeared in the live log) whenever build_race_array dedupes
an overlap between the picks and the trainee's compulsory goal races.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def r(year, pid):
    return {"year": year, "program_id": pid}


class Rules:
    """Enough of racedata.RaceRules to drive build_schedule's merge logic."""

    def __init__(self, targets):
        self._targets = targets

    def goal_races(self, chara_id):
        return list(self._targets)

    route_targets = goal_races

    def place(self, program_id):
        return 1

    def filter_races(self, chara_id, races):
        return list(races), []

    def build_race_array(self, chara_id, races):
        """Goal races plus the supplied ones, deduped by (year, program_id)."""
        out, seen = [], set()
        for e in list(self._targets) + list(races):
            key = (e["year"], e["program_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(e))
        return out

    def fan_reward(self, program_id):
        return 1000

    def fill_race_array(self, chara_id, built, target=0, protect=()):
        return list(built)


def build(picked, agenda, targets, goals_only=False):
    """Run the REAL independent.build_schedule against fake race rules."""
    import types
    from aic import independent

    pre = types.SimpleNamespace(
        last_policy=None,
        race_deck=lambda n: ({"deck_num": 0, "deck_name": "saved", "race_array": list(agenda)}
                             if agenda else None))
    built = independent.build_schedule(pre, rules=Rules(targets), chara_id=1001,
                                       extra_races=[dict(x) for x in picked],
                                       goals_only=goals_only).race_array
    chosen = {(x["year"], x["program_id"]) for x in picked} | \
             {(t["year"], t["program_id"]) for t in targets}
    extras = sum(1 for x in built
                 if (x["year"], x["program_id"]) not in chosen)
    return built, extras


PICKS = [r(1, 637), r(1, 629), r(2, 181), r(3, 8), r(3, 9)]
AGENDA = [r(1, 900), r(2, 901), r(3, 902)]          # races the operator never chose
GOALS = [r(1, 637), r(2, 500), r(3, 501)]           # note: 1/637 overlaps a pick


def test_picks_replace_the_agenda() -> None:
    print("an explicit --races list displaces the saved agenda")
    built, extras = build(PICKS, AGENDA, GOALS)
    ids = {(x["year"], x["program_id"]) for x in built}
    check("no agenda race is scheduled",
          not ids & {(1, 900), (2, 901), (3, 902)}, str(sorted(ids)))
    check("every pick is scheduled",
          all((x["year"], x["program_id"]) in ids for x in PICKS), str(sorted(ids)))
    check("the trainee's goal races are still compulsory",
          (2, 500) in ids and (3, 501) in ids, str(sorted(ids)))
    check("extras count is zero, not negative", extras == 0, str(extras))


def test_the_overlap_no_longer_goes_negative() -> None:
    """1/637 is both a pick and a goal race - the old formula's failure case."""
    print("an overlapping pick does not produce a negative count")
    built, extras = build(PICKS, AGENDA, GOALS)
    old_formula = len(built) - len(GOALS) - len(PICKS)
    check("the OLD formula would have gone negative here", old_formula < 0,
          str(old_formula))
    check("the new count is never negative", extras >= 0, str(extras))


def test_no_picks_still_uses_the_agenda() -> None:
    print("with no --races the saved agenda is still the default")
    built, extras = build([], AGENDA, GOALS)
    ids = {(x["year"], x["program_id"]) for x in built}
    check("agenda races are scheduled",
          {(1, 900), (2, 901), (3, 902)} <= ids, str(sorted(ids)))
    check("and counted as extras", extras == 3, str(extras))


def test_goals_only_drops_the_agenda() -> None:
    print("goals_only (Ace Runs): the route's goal races and nothing else")
    built, extras = build([], AGENDA, GOALS, goals_only=True)
    ids = {(x["year"], x["program_id"]) for x in built}
    check("exactly the goal races", ids == {(t["year"], t["program_id"]) for t in GOALS},
          str(sorted(ids)))
    check("no agenda extras", extras == 0, str(extras))


def test_choice_goal_keeps_one_race_per_turn() -> None:
    """Daiwa Scarlet: 'Japanese Oaks or Tokyo Yushun' sits on ONE turn (y2 late May)."""
    print("a choice goal (two races on one turn) keeps only the first")
    from aic import racedata

    def prog(month, half):
        return {"month": month, "half": half, "permission": 2, "group": 0,
                "fans": 1000, "need_fan": 0}
    rules = racedata.RaceRules(
        programs={"162": prog(4, 1), "165": prog(5, 2), "166": prog(5, 2), "167": prog(10, 2)},
        routes={"1009": 7},
        # turn 34 = year 2 (24 turns a year); 165 and 166 share year-2 May half 2
        route_races={"7": [{"program_id": 162, "turn": 31}, {"program_id": 165, "turn": 34},
                           {"program_id": 166, "turn": 34}, {"program_id": 167, "turn": 44}]},
        race_turns=["2:4:1", "2:5:2", "2:10:2"])
    goals = [t["program_id"] for t in rules.route_targets(1009)]
    check("Oaks kept, the Derby branch dropped", goals == [162, 165, 167], str(goals))
    built = rules.build_race_array(1009, [])
    slots = [rules._slot(e["year"], e["program_id"]) for e in built]
    check("no two races on one turn in the built list", len(slots) == len(set(slots)), str(slots))


def test_same_picks_give_the_same_size_per_trainee() -> None:
    """The whole point: identical picks must not vary by account."""
    print("identical picks give identical schedules")
    a, _ = build(PICKS, AGENDA, GOALS)
    b, _ = build(PICKS, [r(1, 111), r(2, 222)], GOALS)   # a different agenda
    c, _ = build(PICKS, [], GOALS)                        # no agenda at all
    sizes = {len(a), len(b), len(c)}
    check("three different saved agendas, one schedule size", len(sizes) == 1,
          str(sizes))
    check("and the same races", {(x["year"], x["program_id"]) for x in a}
          == {(x["year"], x["program_id"]) for x in b}, "sets differ")


def main() -> int:
    for fn in (test_picks_replace_the_agenda,
               test_the_overlap_no_longer_goes_negative,
               test_no_picks_still_uses_the_agenda,
               test_goals_only_drops_the_agenda,
               test_choice_goal_keeps_one_race_per_turn,
               test_same_picks_give_the_same_size_per_trainee):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all race-schedule checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

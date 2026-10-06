"""Skill buying: the right variants, best value first, never past the points.

The rules come from three real purchases (dumps 20260713_192951/0393-0394,
20260723_070635/0407-0408, 20260724_124011/0417-0418): a rarity-2 tip buys the
gold together with its white base, a rarity-1 tip the ○ (and its ◎ only as a
pair), never an upgrade without its base. The replay at the bottom runs the
picker against those captures and the real master.mdb when they are on disk.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import skills  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


#: Shaped like the real groups: 20033 white 200332 + gold 200331, 20005 ○ 200052
#: + ◎ 200051 + × 200053, 20124 ○ 201242 + ◎ 201241, 20022 a lone white.
COST = {200331: 180, 200332: 180, 200333: 100, 200051: 110, 200052: 90,
        200053: 50, 201241: 140, 201242: 130, 200221: 100}
GROUPS = {
    20033: {(2, 2): (200331, 508), (1, 1): (200332, 217), (1, -1): (200333, -262)},
    20005: {(1, 2): (200051, 174), (1, 1): (200052, 129), (1, -1): (200053, -129)},
    20124: {(1, 2): (201241, 262), (1, 1): (201242, 217)},
    20022: {(1, 1): (200221, 129)},
    20099: {(1, -1): (200991, -100)},                      # negative-only group
}
TIPS = [{"group_id": 20033, "rarity": 2, "level": 3},
        {"group_id": 20005, "rarity": 1, "level": 1},
        {"group_id": 20124, "rarity": 1, "level": 5},
        {"group_id": 20022, "rarity": 1, "level": 0},
        {"group_id": 20099, "rarity": 1, "level": 1}]
UPGRADE_OF = {200331: 200332, 200051: 200052, 201241: 201242}


def with_tables():
    skills.load_tables = lambda *a, **k: (COST, GROUPS)


def ids_of(picked):
    return [p["skill_id"] for p in picked]


def test_off_by_default() -> None:
    print("off unless AIC_BUY_SKILLS is set")
    import os
    old = os.environ.pop("AIC_BUY_SKILLS", None)
    try:
        check("unset means off", skills.buy_enabled() is False)
        for val, want in (("1", True), ("on", True), ("0", False), ("maybe", False)):
            os.environ["AIC_BUY_SKILLS"] = val
            check(f"{val!r} -> {want}", skills.buy_enabled() is want)
    finally:
        os.environ.pop("AIC_BUY_SKILLS", None)
        if old is not None:
            os.environ["AIC_BUY_SKILLS"] = old


def test_variants() -> None:
    print("upgrades only with their base; never a negative skill")
    with_tables()
    ids = ids_of(skills.pick(TIPS, 10_000))
    for up, base in UPGRADE_OF.items():
        if up in ids:
            check(f"{up} comes with its base {base}", base in ids, str(ids))
    check("gold tip buys gold + white", {200331, 200332} <= set(ids), str(ids))
    check("no × skill, no negative-only group", not {200053, 200333, 200991} & set(ids), str(ids))
    check("each group bought once", len(ids) == len(set(ids)), str(ids))
    # base already learned -> the upgrade alone
    ids2 = ids_of(skills.pick(TIPS, 10_000, owned_ids=[200332]))
    check("owned white -> gold alone", 200331 in ids2 and 200332 not in ids2, str(ids2))
    ids3 = ids_of(skills.pick(TIPS, 10_000, owned_ids=[200331]))
    check("owned gold -> nothing more from that group", not {200331, 200332} & set(ids3), str(ids3))


def planned_cost(picked, tips=TIPS, groups=GROUPS, cost=COST):
    """The exact price: gold at the rarity-2 tip level, rarity-1 skills at the
    rarity-1 tip level (0 if none)."""
    level = {(t["group_id"], 2 if t["rarity"] == 2 else 1): t["level"] for t in tips}
    where = {sid: (g, rar) for g, vs in groups.items() for (rar, _), (sid, _) in vs.items()}
    return sum(skills._price(cost[p["skill_id"]],
                             level.get((where[p["skill_id"]][0], where[p["skill_id"]][1]), 0))
               for p in picked)


def test_budget_and_value() -> None:
    print("never past the budget at the exact price; best value per point first")
    with_tables()
    check("price formula: 180 at hint 3 -> 126, at 5 -> 108",
          (skills._price(180, 3), skills._price(180, 5), skills._price(90, 0)) == (126, 108, 90))
    for budget in (0, 89, 90, 200, 400, 10_000):
        picked = skills.pick(TIPS, budget)
        spend = planned_cost(picked)
        check(f"budget {budget}: plans {spend} <= {budget}", spend <= budget, str(ids_of(picked)))
    # the white under a gold is priced at the RARITY-1 tip level (none here -> full)
    gold_only_tip = [{"group_id": 20033, "rarity": 2, "level": 3}]
    check("gold pair: gold at its hint, white at full price (126 + 180 = 306)",
          ids_of(skills.pick(gold_only_tip, 306)) == [200332, 200331]
          and ids_of(skills.pick(gold_only_tip, 305)) == [200332],
          f"{ids_of(skills.pick(gold_only_tip, 306))} / {ids_of(skills.pick(gold_only_tip, 305))}")
    check("negative budget buys nothing", skills.pick(TIPS, -5) == [])
    # 400 points: gold pair (508 for 288 discounted) beats spreading on ◎
    ids = ids_of(skills.pick(TIPS, 400))
    check("the gold pair is taken first", {200331, 200332} <= set(ids), str(ids))
    base_ids = ids_of(skills.pick(TIPS, 10_000, discounted=False))
    check("base-price mode still respects the rules", 200331 in base_ids and 200332 in base_ids,
          str(base_ids))
    whites = ids_of(skills.pick(TIPS, 10_000, discounted=False, whites_only=True))
    check("whites-only: plain bases, no upgrade, no gold",
          sorted(whites) == [200052, 200221, 200332, 201242], str(whites))


def test_owned_and_lone() -> None:
    print("owned upgrades end the group; a lone gold is buyable on its own")
    with_tables()
    # owning the ◎ of 20124 (which replaced its ○) leaves nothing to buy there
    ids = ids_of(skills.pick([{"group_id": 20124, "rarity": 1, "level": 1}], 10_000,
                             owned_ids=[201241]))
    check("owned ◎ -> the replaced ○ is not bought again", ids == [], str(ids))
    GROUPS[20205] = {(2, 2): (202051, 370)}                   # Runaway: gold, no base
    COST[202051] = 200
    try:
        ids = ids_of(skills.pick([{"group_id": 20205, "rarity": 2, "level": 0}], 10_000))
        check("lone gold bought alone", ids == [202051], str(ids))
        ids = ids_of(skills.pick([{"group_id": 20205, "rarity": 2, "level": 0}], 10_000,
                                 whites_only=True))
        check("...but never in the whites-only retry", ids == [], str(ids))
    finally:
        GROUPS.pop(20205)
        COST.pop(202051)


def test_missing_mdb() -> None:
    print("no master.mdb means no purchase, not a crash")
    skills.load_tables = lambda *a, **k: ({}, {})
    check("empty tables buy nothing", skills.pick(TIPS, 10_000) == [])


def test_buy_from() -> None:
    print("buy_from: retries at base price once, never raises")
    with_tables()
    chara = {"skill_point": 10_000, "skill_tips_array": TIPS, "skill_array": []}
    calls = []

    class Client:
        def __init__(self, fail_first=0):
            self.fail = fail_first

        def gain_skills(self, arr, current_turn):
            calls.append((list(arr), current_turn))
            if self.fail:
                self.fail -= 1
                raise RuntimeError("refused")
            return {"data": {}}

    n = skills.buy_from(types.SimpleNamespace(client=Client()), chara, 78)
    check("bought in one call", n > 0 and len(calls) == 1 and calls[0][1] == 78, str(calls))
    calls.clear()
    n = skills.buy_from(types.SimpleNamespace(client=Client(fail_first=1)),
                        {**chara, "skill_point": 300}, 78)
    check("a refusal retries once, whites only at base price",
          len(calls) == 2 and n == len(calls[1][0])
          and all(p["skill_id"] in (200052, 200221, 200332, 201242) for p in calls[1][0]),
          f"{n} {calls}")
    real_load = skills.load_tables
    skills.load_tables = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad row"))
    try:
        check("a broken skill table never raises out of buy_from",
              skills.buy_from(types.SimpleNamespace(client=Client()), chara, 78) == 0)
    finally:
        skills.load_tables = real_load
    calls.clear()
    n = skills.buy_from(types.SimpleNamespace(client=Client(fail_first=2)), chara, 78)
    check("two refusals -> 0, no raise", n == 0 and len(calls) == 2, str(n))
    check("junk chara -> 0", skills.buy_from(types.SimpleNamespace(client=Client()), None, 1) == 0)


def test_replay_real_purchases() -> None:
    print("replay: the three real purchases (skipped when captures/mdb are absent)")
    mdb = Path(r"C:/Users/victo/Downloads/New folder/lmao-repo-linux-port/bot data/master.mdb")
    dumps = Path(r"C:/Users/victo/OneDrive/Documents/UmaDumpy-main/dumps")
    if not mdb.is_file() or not dumps.is_dir():
        print("  skip (no master.mdb / dumps on this machine)")
        return
    skills._tables.cache_clear()
    skills.load_tables = lambda *a, **k: skills._tables(str(mdb))
    con = sqlite3.connect(f"file:{mdb}?mode=ro", uri=True)
    group_of = dict(con.execute("SELECT id, group_id FROM skill_data"))
    rate_of = dict(con.execute("SELECT id, group_rate FROM skill_data"))
    rarity_of = dict(con.execute("SELECT id, rarity FROM skill_data"))
    cost = dict(con.execute("SELECT id, need_skill_point FROM single_mode_skill_need_point"))
    con.close()

    def exact(ids, tips):
        level = {(t["group_id"], 2 if t["rarity"] == 2 else 1): t["level"] for t in tips}
        return sum(skills._price(cost[s], level.get((group_of[s], 2 if rarity_of[s] == 2 else 1), 0))
                   for s in ids)
    for run, pre, buy in (("20260723_070635", 407, 408), ("20260724_124011", 417, 418),
                          ("20260713_192951", 393, 394)):
        load = lambda n: json.loads((dumps / run / "transactions" / f"{n:04d}.json")
                                    .read_text(encoding="utf-8"))
        ci = load(pre)["response"]["data"]["data"]["chara_info"]
        b = load(buy)
        body = b["request"]["data"]
        body = body.get("data", body)
        real = [g["skill_id"] for g in body["gain_skill_info_array"]]
        charged = ci["skill_point"] - b["response"]["data"]["data"]["chara_info"]["skill_point"]
        tips = ci["skill_tips_array"]
        check(f"{run}: price formula reproduces the real charge {charged} exactly",
              exact(real, tips) == charged, str(exact(real, tips)))
        owned = [s["skill_id"] for s in ci.get("skill_array") or []]
        picked = ids_of(skills.pick(tips, ci["skill_point"], owned))
        bad = [s for s in picked if rate_of[s] == 2 and rarity_of[s] == 1
               and not any(group_of[o] == group_of[s] and rate_of[o] == 1 for o in picked + owned)]
        check(f"{run}: no ◎ without its ○ ({len(picked)} picked)", not bad, str(bad))
        planned = exact(picked, tips)
        check(f"{run}: plans {planned} within {ci['skill_point']} points", planned <= ci["skill_point"])
        overlap = len(set(picked) & set(real))
        check(f"{run}: overlaps the real client's choice ({overlap}/{len(real)})",
              overlap >= len(real) // 2, str(sorted(set(real) - set(picked))))


def main() -> int:
    for fn in (test_off_by_default, test_variants, test_budget_and_value, test_owned_and_lone,
               test_missing_mdb, test_buy_from, test_replay_real_purchases):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all skill-purchase checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

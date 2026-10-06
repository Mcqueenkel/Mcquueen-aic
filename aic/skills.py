"""Spend a finished career's leftover skill points, if asked to.

**Off unless ``AIC_BUY_SKILLS`` is set.** For a farm chasing fans this buys
nothing: the races were simulated during the 50-minute run and are already
scored by the time these points can be spent, and the trained character is
deleted soon after when storage fills (:mod:`aic.veterans`). It is here because
of one thing it *does* change - see below.

WHERE THIS RUNS, AND WHY THE ORDER MATTERS
------------------------------------------
The real client's sequence at the end of a career is

    last race  ->  gain_skills  ->  factor_select  ->  finish

and the middle step is not cosmetic: **the factor pool is drawn from the skills
the character owns at that moment.** VERIFIED in a capture (dumps 20260713,
transactions 0393-0395): one ``gain_skills`` call turned 2482 points into 23
skills, and 9 of the 17 factors ``factor_select`` then offered decode to skills
bought twelve seconds earlier. So buying before the factor roll is the only
reason to buy at all - it widens what the veteran can pass on.

MEASURED on this farm 2026-09-05: an idle career reaches the finish holding
**2604 unspent points and 25 buyable groups**, so the auto-trainer does not
spend them during the run. The budget is real.

WHAT A TIP BUYS (matches the real client, captures 20260713/0394,
20260723_070635/0408, 20260724_124011/0418)
-------------------------------------------------------------------
``skill_tips_array`` rows are ``{group_id, rarity, level}``. In a group,
``group_rate`` 1 is the base (○ / white), 2 the upgrade (◎, or the gold skill
when rarity 2) and -1 the negative (×). An upgrade is only bought TOGETHER with
its base - the client sends gold+white and ◎+○ as pairs - so a rarity-2 tip buys
the gold and its white, a rarity-1 tip buys the ○ (plus the ◎ when it is
offered and affordable). Picking the "lowest id" instead, as this used to, chose
the upgrade WITHOUT its base in 300 of 339 groups.

Skills are ranked by ``grade_value`` per point - grade_value is what a skill adds
to the trained character's rank_score, which is the point of buying on an
Ace Run (aic.ace).

PRICE (exact)
-------------
``floor(need_skill_point * (100 - pct) / 100)`` with pct 0/10/20/30/35/40 for
hint levels 0-5 reproduces the server's charge on all three captured purchases
EXACTLY (2482, 2678, 2985). A gold is priced at its group's rarity-2 tip level;
every rarity-1 skill (○, ◎, and the white under a gold) at the group's rarity-1
tip level, or 0 when the group has no rarity-1 tip. If the server still refuses,
:func:`buy_from` retries once with plain whites only, at base price.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache

from .logging_setup import get
from .racedata import find_master_mdb

log = get("skills")


def buy_enabled() -> bool:
    """``AIC_BUY_SKILLS`` - off unless explicitly set to a true-ish value."""
    raw = (os.environ.get("AIC_BUY_SKILLS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


#: Hint-level discount in percent - the server's own (module doc).
DISCOUNT_PCT = {0: 0, 1: 10, 2: 20, 3: 30, 4: 35, 5: 40}


@lru_cache(maxsize=1)
def _tables(mdb: str) -> tuple[dict, dict]:
    """``({skill_id: cost}, {group_id: {(rarity, group_rate): (skill_id, grade)}})``."""
    con = sqlite3.connect(f"file:{mdb}?mode=ro", uri=True)
    try:
        cost = {int(i): int(p) for i, p in con.execute(
            "SELECT id, need_skill_point FROM single_mode_skill_need_point "
            "WHERE need_skill_point > 0")}
        groups: dict[int, dict] = {}
        for sid, gid, rarity, rate, grade in con.execute(
                "SELECT id, group_id, rarity, group_rate, grade_value "
                "FROM skill_data WHERE group_id > 0"):
            groups.setdefault(int(gid), {})[(int(rarity), int(rate))] = (int(sid), int(grade or 0))
    finally:
        con.close()
    return cost, groups


def load_tables(mdb_path: str | os.PathLike | None = None):
    found = find_master_mdb(mdb_path)
    if not found:
        return {}, {}
    try:
        return _tables(str(found))
    except sqlite3.Error as exc:
        log.warning("could not read the skill tables from %s (%s); "
                    "not buying skills.", found, exc)
        return {}, {}


def _price(cost: int, level: int, discounted: bool = True) -> int:
    if not discounted:
        return cost
    return cost * (100 - DISCOUNT_PCT[min(max(int(level), 0), 5)]) // 100


def _units(skill_tips_array, owned_ids, cost, groups, discounted, whites_only=False):
    """Purchase units per offered group: ``(grade, price, [skill ids], group_id)``.

    A unit is the base alone, base + upgrade (◎, or the gold) bought together, an
    upgrade alone when the base is already learned, or a lone gold (a group with
    no base variant, e.g. Runaway). Nothing is offered past what is owned: a group
    whose upgrade (or gold) is learned has nothing left, and a learned base is
    never bought again. Gold kept at group_rate 3 is not offered (the chain to it
    is unverified). ``whites_only`` keeps plain base whites - the safest set.
    """
    owned = {int(i) for i in owned_ids}
    level: dict[tuple[int, int], int] = {}          # (group, 1|2) -> hint level
    offered: dict[int, set[int]] = {}
    for tip in skill_tips_array or []:
        if not isinstance(tip, dict):
            continue
        try:
            gid = int(tip.get("group_id") or 0)
            rarity = 2 if int(tip.get("rarity") or 1) == 2 else 1
            lv = int(tip.get("level") or 0)
        except (TypeError, ValueError):
            continue
        level[(gid, rarity)] = lv
        offered.setdefault(gid, set()).add(rarity)

    units = []
    for gid, rarities in offered.items():
        variants = groups.get(gid) or {}
        top = max((rate for (_, rate), (sid, _) in variants.items()
                   if sid in owned and rate > 0), default=0)
        base = variants.get((1, 1)) or variants.get((2, 1))
        ups = []
        if 2 in rarities and variants.get((2, 2)):
            ups.append(((2, 2), variants[(2, 2)]))                 # gold
        if 1 in rarities and variants.get((1, 2)):
            ups.append(((1, 2), variants[(1, 2)]))                 # ◎
        options = []
        if base and top == 0:
            bkey = (1, 1) if variants.get((1, 1)) else (2, 1)
            options.append([(bkey, base)])
            if not whites_only:
                options += [[(bkey, base), up] for up in ups]
        elif not whites_only and top == 1:
            options += [[up] for up in ups]                        # base learned
        elif not whites_only and base is None and top == 0:
            options += [[up] for up in ups if up[0][0] == 2]       # lone gold
        for opt in options:
            ids = [sid for _, (sid, _) in opt]
            if any(s not in cost or s in owned for s in ids):
                continue
            price = sum(_price(cost[sid], level.get((gid, rar), 0), discounted)
                        for (rar, _), (sid, _) in opt)
            # an upgrade REPLACES its base on the sheet: value is the top skill's
            units.append((opt[-1][1][1], price, ids, gid))
    return units


def pick(skill_tips_array, budget: int, owned_ids=(), mdb_path=None,
         discounted: bool = True, whites_only: bool = False) -> list[dict]:
    """Which skills to buy within ``budget``, best rank-score per point first.

    See :func:`_units` for what a group can buy. Units are taken by
    ``grade_value`` per point, one per group. ``discounted=False`` prices at base
    cost (always affordable); ``whites_only`` restricts to plain base skills.
    """
    cost, groups = load_tables(mdb_path)
    if not cost or not groups or budget <= 0:
        return []
    units = _units(skill_tips_array, owned_ids, cost, groups, discounted, whites_only)

    # Best value per point; a group is bought once (its richest affordable unit).
    units.sort(key=lambda u: (-u[0] / max(u[1], 1), -u[0]))
    picked, spent, groups_done = [], 0, set()
    for grade, price, ids, gid in units:
        if gid in groups_done or grade <= 0 or spent + price > budget:
            continue
        picked.extend({"skill_id": s, "level": 1} for s in ids)
        spent += price
        groups_done.add(gid)
    if picked:
        log.info("Buying %s skill(s) for about %s of %s points.",
                 len(picked), f"{spent:,}", f"{budget:,}")
    return picked


def buy_from(session, chara: dict, turn: int, mdb_path=None) -> int:
    """Spend the points in ``chara``. Returns how many skills were bought.

    Takes the chara_info block directly rather than a CareerState:
    ``enrich_from_chara_info`` copies out a few scalars and discards the rest,
    so the tips list and the point balance only exist at the call site.

    Never raises: the career is finished and banked by the time this runs, so a
    failure here must not cost it. Mirrors how ``factor_select`` is guarded.
    """
    if not isinstance(chara, dict):
        return 0
    try:
        budget = int(chara.get("skill_point") or 0)
    except (TypeError, ValueError):
        return 0

    try:
        owned = [s.get("skill_id") for s in (chara.get("skill_array") or [])
                 if isinstance(s, dict) and s.get("skill_id")]
        picked = pick(chara.get("skill_tips_array"), budget, owned, mdb_path)
    except Exception as exc:  # noqa: BLE001 - a bad table row must not block the finish
        log.warning("could not plan the skill purchase (%s); finishing without it.", exc)
        return 0
    if not picked:
        log.info("Nothing to buy: %s points, %s group(s) offered.",
                 f"{budget:,}", len(chara.get("skill_tips_array") or []))
        return 0

    try:
        session.client.gain_skills(picked, current_turn=turn)
    except Exception as exc:  # noqa: BLE001 - never cost a banked career
        # A refusal must not leave the points unspent: retry once with the
        # safest set there is - plain whites, at base price.
        try:
            safe = pick(chara.get("skill_tips_array"), budget, owned, mdb_path,
                        discounted=False, whites_only=True)
        except Exception:  # noqa: BLE001
            safe = []
        log.warning("gain_skills refused (%s); retrying with %s plain white skill(s) "
                    "at base price.", exc, len(safe))
        if not safe:
            return 0
        try:
            session.client.gain_skills(safe, current_turn=turn)
        except Exception as exc2:  # noqa: BLE001
            log.warning("gain_skills failed again (%s); finishing without "
                        "spending the points.", exc2)
            return 0
        picked = safe
    log.info("Bought %s skill(s) before the factor roll.", len(picked))
    return len(picked)

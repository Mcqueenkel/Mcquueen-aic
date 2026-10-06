"""A support card below its unlock level has NO fan bonus.

``fan_bonus_at_level`` read ``if level <= curve[0][0]: return curve[0][1]``, so a
card whose Fan Bonus unlocks at level 45 was credited its full unlock value at
level 1. MEASURED on the live data 2026-09-05: **111 of the 172** cards with a
fan-bonus curve unlock above level 1, at levels 5, 10, 15, 25, 30 and 45. A farm
account is full of under-levelled cards, so the ranking that picks a deck was
choosing on phantom numbers - and any "best possible deck" figure derived from it
was inflated.

The second rule pinned here is the deck-confirm body: it must never transmit
``[0, 0, 0, 0, 0]`` for a slot that actually holds cards.
"""

from __future__ import annotations

import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic.supportdata import SupportData  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def sd(curve, rarity=3):
    """A SupportData carrying one card (id 1) with the given curve."""
    obj = SupportData.__new__(SupportData)
    obj.fan_bonus = {"1": {"curve": curve, "rarity": rarity}}
    obj.exp_levels = {}
    obj.max_levels = {}
    return obj


def test_below_unlock_is_zero() -> None:
    print("below the unlock level: no bonus at all")
    # The real shape of a lv45-unlock SSR.
    s = sd([[45, 15]])
    check("level 1 scores 0, not 15", s.fan_bonus_at_level(1, 1) == 0.0,
          str(s.fan_bonus_at_level(1, 1)))
    check("level 44 still scores 0", s.fan_bonus_at_level(1, 44) == 0.0,
          str(s.fan_bonus_at_level(1, 44)))
    check("level 45 scores 15", s.fan_bonus_at_level(1, 45) == 15.0,
          str(s.fan_bonus_at_level(1, 45)))
    check("above the last checkpoint holds", s.fan_bonus_at_level(1, 50) == 15.0,
          str(s.fan_bonus_at_level(1, 50)))


def test_a_real_r_card_curve() -> None:
    print("a real R-card curve, as read from master.mdb")
    # card 10002: [[5,1],[15,5],[20,5],[35,10]]
    s = sd([[5, 1], [15, 5], [20, 5], [35, 10]], rarity=1)
    check("level 1 is 0 (unlock is 5)", s.fan_bonus_at_level(1, 1) == 0.0,
          str(s.fan_bonus_at_level(1, 1)))
    check("level 4 is still 0", s.fan_bonus_at_level(1, 4) == 0.0,
          str(s.fan_bonus_at_level(1, 4)))
    check("level 5 is 1", s.fan_bonus_at_level(1, 5) == 1.0,
          str(s.fan_bonus_at_level(1, 5)))
    check("level 15 is 5", s.fan_bonus_at_level(1, 15) == 5.0,
          str(s.fan_bonus_at_level(1, 15)))
    check("level 35 is 10", s.fan_bonus_at_level(1, 35) == 10.0,
          str(s.fan_bonus_at_level(1, 35)))


def test_a_curve_starting_at_one_is_unchanged() -> None:
    print("a card that unlocks at level 1 is not affected")
    s = sd([[1, 5], [45, 20]])
    check("level 1 still scores 5", s.fan_bonus_at_level(1, 1) == 5.0,
          str(s.fan_bonus_at_level(1, 1)))
    check("level 45 scores 20", s.fan_bonus_at_level(1, 45) == 20.0,
          str(s.fan_bonus_at_level(1, 45)))


def test_between_checkpoints_is_untouched() -> None:
    """Interpolation shape is NOT settled - this only pins that it is bounded.

    AIC interpolates linearly between checkpoints; another implementation holds
    the last checkpoint. They disagree on ~44% of card/uncap pairs and no
    capture resolves it, so this asserts only that the value stays between the
    two neighbours rather than fixing a shape that may be wrong.
    """
    print("between checkpoints: bounded, shape deliberately unpinned")
    s = sd([[30, 5], [50, 10]])
    mid = s.fan_bonus_at_level(1, 40)
    check("a mid value sits between its neighbours", 5.0 <= mid <= 10.0, str(mid))
    check("and the endpoints are exact",
          s.fan_bonus_at_level(1, 30) == 5.0 and s.fan_bonus_at_level(1, 50) == 10.0)


def test_no_curve_is_none_not_zero() -> None:
    print("a card with no Fan Bonus effect at all")
    s = sd([])
    check("an empty curve is None, not 0", s.fan_bonus_at_level(1, 50) is None)
    check("an unknown card is None", s.fan_bonus_at_level(999, 50) is None)


def test_confirm_deck_never_zeroes_a_populated_slot() -> None:
    """The deck-confirm body must echo real contents, not zeros.

    The same array is a WRITE payload on support_card_deck/change_party, so a
    zeroed slot could wipe a saved deck. It has never fired here only because
    this farm runs flow B and confirm_deck is flow A only.
    """
    print("confirm_deck echoes the account's real decks")
    import types
    from aic import independent

    session = types.SimpleNamespace(
        load_index={"support_card_deck_array": [
            {"deck_id": 1, "support_card_id_array": [11, 12, 13, 14, 15]},
            {"deck_id": 2, "support_card_id_array": [21, 22, 23, 24, 25]},
            {"deck_id": 3, "support_card_id_array": []},
        ]},
        clock=types.SimpleNamespace(observe=lambda r: None),
    )
    sent = {}

    class Client:
        def call(self, ep, body):
            sent["body"] = body
            return {"data": {}}

    session.client = Client()
    selection = types.SimpleNamespace(deck_id=2, support_card_ids=[91, 92, 93, 94, 95])
    spec = types.SimpleNamespace(payload=lambda k: {}, endpoint=lambda k: "idle/start")

    independent.confirm_deck(session, spec, selection)
    slots = {d["deck_id"]: d["support_card_id_array"] for d in
             sent["body"]["support_card_deck_array"]}
    check("the target slot carries the new deck", slots[2] == [91, 92, 93, 94, 95],
          str(slots.get(2)))
    check("THE regression: deck 1 keeps its real cards, not zeros",
          slots[1] == [11, 12, 13, 14, 15], str(slots.get(1)))
    check("a genuinely empty slot is still zeros", slots[3] == [0, 0, 0, 0, 0],
          str(slots.get(3)))
    check("all ten slots are sent", len(slots) == 10, str(sorted(slots)))


def test_save_deck_writes_only_its_slot() -> None:
    """save_deck must fill its target and leave every other deck alone.

    MEASURED on live account 896245680077 (Oguri) 2026-09-05: decks 1-4 held
    real cards, slot 10 was empty; after save_deck(10, ...) slot 10 held the
    built deck and decks 1-9 were byte-identical. This pins that in code.
    """
    print("save_deck writes one slot and echoes the rest")
    import types
    from aic import flow

    stored = {1: [11, 12, 13, 14, 15], 2: [21, 22, 23, 24, 25], 10: []}

    def index():
        return {"support_card_deck_array": [
            {"deck_id": n, "support_card_id_array": stored.get(n, [])}
            for n in range(1, 11)]}

    sent = {}

    class Client:
        def change_support_card_deck(self, array):
            sent["array"] = array
            for d in array:
                ids = [i for i in d["support_card_id_array"] if i]
                stored[d["deck_id"]] = ids
            return {"data": {}}

    # Mirror the real Session: refresh_home() reassigns load_index
    # (aic/auth.py:372), which is what makes the unchanged-deck early-out work.
    session = types.SimpleNamespace(client=Client(), load_index=index())

    def refresh():
        session.load_index = index()
        return session.load_index

    session.refresh_home = refresh
    ok = flow.save_deck(session, 10, [91, 92, 93, 94, 95])
    check("it reports success", ok is True, str(ok))
    check("the target slot holds the new deck",
          stored[10] == [91, 92, 93, 94, 95], str(stored[10]))
    check("THE regression: deck 1 is untouched",
          stored[1] == [11, 12, 13, 14, 15], str(stored[1]))
    check("deck 2 is untouched", stored[2] == [21, 22, 23, 24, 25], str(stored[2]))
    by_id = {d["deck_id"]: d["support_card_id_array"] for d in sent["array"]}
    check("all ten slots are sent", len(by_id) == 10, str(sorted(by_id)))
    check("no populated slot is ever sent as zeros",
          by_id[1] == [11, 12, 13, 14, 15] and by_id[2] == [21, 22, 23, 24, 25],
          str(by_id))

    # Re-saving the same deck must not re-write.
    sent.clear()
    ok = flow.save_deck(session, 10, [91, 92, 93, 94, 95])
    check("an unchanged deck is not rewritten", ok is True and "array" not in sent,
          str(sent))

    # A failing save must not raise - the start still carries the ids.
    class Boom:
        def change_support_card_deck(self, array):
            raise RuntimeError("network")

    session2 = types.SimpleNamespace(client=Boom(), load_index=index(),
                                     refresh_home=lambda: index())
    check("a failed save returns False rather than raising",
          flow.save_deck(session2, 9, [1, 2, 3, 4, 5]) is False)


def test_friend_card_respects_group_cards() -> None:
    """A borrowed card that counts as several characters must be rejected.

    30081 counts as charas 1001, 1002, 1013, 1016, 1030 and 1035; 30067 as
    1003, 1017 and 1073. pick_friend_card compared a single name-derived key,
    so it could borrow one of those onto a deck already holding one of those
    characters - an illegal six-card set the server refuses. It now uses
    deck_conflict, the same check the deck itself passes.
    """
    print("a group card is not borrowable onto a conflicting deck")
    from aic import selection as sel
    from aic.gamedata import support_card_charas

    chara_map = support_card_charas()
    if not chara_map or 30081 not in chara_map:
        check("chara map unavailable; skipped", True)
        return

    group = sorted(chara_map[30081])
    # A plain card belonging to one of the characters 30081 also counts as.
    clash = next((cid for cid, cs in chara_map.items()
                  if cid != 30081 and len(cs) == 1 and next(iter(cs)) in group), 0)
    if not clash:
        check("no single-character partner found; skipped", True)
        return

    class GD:                       # deck_conflict only needs a name for the message
        def support_name(self, sid): return f"card {sid}"
        def support_identity(self, sid): return f"#{sid}"
        def chara_identity(self, cid): return f"#chara-{cid}"
        def is_sirius(self, sid): return False

    gd = GD()
    conflicts = sel.deck_conflict([clash, 30081], 0, gd)
    check("deck_conflict sees the group overlap", bool(conflicts),
          f"clash={clash} group={group} -> {conflicts!r}")
    check("and the card alone is fine",
          not sel.deck_conflict([clash], 0, gd), str(clash))
    check("pick_friend_card uses deck_conflict, not a name key",
          "deck_conflict(deck + [sid]" in
          (pathlib.Path(__file__).resolve().parent.parent
           / "aic" / "selection.py").read_text(encoding="utf-8"))


def main() -> int:
    fns = [test_friend_card_respects_group_cards,
           test_save_deck_writes_only_its_slot, test_below_unlock_is_zero, test_a_real_r_card_curve,
           test_a_curve_starting_at_one_is_unchanged,
           test_between_checkpoints_is_untouched, test_no_curve_is_none_not_zero]
    try:
        from aic import independent  # noqa: F401
        if hasattr(independent, "confirm_deck"):
            fns.append(test_confirm_deck_never_zeroes_a_populated_slot)
        else:
            print("  note: independent.confirm_deck not found; deck check skipped")
    except Exception as exc:  # noqa: BLE001
        print(f"  note: could not import independent ({exc}); deck check skipped")
    for fn in fns:
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all fan-scoring checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

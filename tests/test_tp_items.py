"""TP recovery spends TP items, and stops spending anything that is not one.

Three ways this has been wrong:

1. Shipped as ``(32, 12, 11)``. Ids 11 and 12 are not in item_data at all, so
   only 32 ever matched and an account with potions logged "no TP potions".
2. "Fixed" by adding Carrot Jelly (35) and Carrot Jelly Mini (34) because they
   carry ``effect_type_1 = 2``. MEASURED on live account 909146445823 on
   2026-09-02: using one item 34 returned ``result_code 1``, decremented the
   held count 180 -> 179, and left TP at 9 with ``tp_info`` null. Using one
   item 32 took TP 9 -> 39. The jellies are RP; they were burned for nothing.
3. ``item_num`` above 1 came back ``result_code 208``.

The discriminator is ``item_data.item_category``: 20 is TP, 21 is RP.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import racedata  # noqa: E402
from aic.config import TP_RESTORE  # noqa: E402
from vendor.uma_api.client import UmaClient  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


class FakeClient:
    """Just enough of the client to drive :meth:`recovery_tp`."""

    #: item id -> TP each one really restores when the SERVER is asked. An item
    #: absent here is one the server consumes without moving TP - an RP item.
    SERVER_TRUTH = {32: 30, 174: 30, 10049: 30}

    def __init__(self, held):
        self.item_map = dict(held)
        self.tp = 0
        self.tp_info = {"current_tp": 0, "max_tp": 100}
        self.coin_info = {"fcoin": 0, "coin": 0}
        self.calls: list[dict] = []

    def call(self, path, payload):
        assert path == "item/use_recovery_item", path
        self.calls.append(dict(payload))
        if payload["item_num"] != 1:
            return {"data": {}, "data_headers": {"result_code": 208}}
        iid = payload["item_id"]
        self.item_map[iid] = self.item_map.get(iid, 0) - 1
        gain = self.SERVER_TRUTH.get(iid)
        if gain is None:                       # RP item: taken, TP unchanged
            return {"data": {"tp_info": None, "rp_info": {}}}
        self.tp += gain
        self.tp_info = {"current_tp": self.tp, "max_tp": 100}
        return {"data": {"tp_info": dict(self.tp_info)}}

    recovery_tp = UmaClient.recovery_tp
    TP_RESTORE_FALLBACK = UmaClient.TP_RESTORE_FALLBACK


def test_rp_items_are_not_tp_items() -> None:
    print("Carrot Jelly is not a TP item")
    check("34 is not in the fallback table", 34 not in TP_RESTORE)
    check("35 is not in the fallback table", 35 not in TP_RESTORE)
    check("the phantom ids 11 and 12 are still gone",
          11 not in TP_RESTORE and 12 not in TP_RESTORE)
    check("Toughness 30 is there", TP_RESTORE.get(32) == 30)

    # THE regression: hand it a jelly-only holding, as if 34 were still listed.
    c = FakeClient({34: 180})
    try:                          # a shortfall raises; that is not what we test
        c.recovery_tp(count=1, items={34: 1})
    except Exception:
        pass
    check("one wasted jelly, then it stops (not 30)",
          len(c.calls) == 1 and c.item_map[34] == 179,
          f"{len(c.calls)} calls, {c.item_map[34]} left")
    check("TP really did not move", c.tp == 0, str(c.tp))


def test_it_spends_one_toughness() -> None:
    print("a 30-TP top-up costs one item")
    c = FakeClient({32: 43, 34: 180})
    c.recovery_tp(count=1, items={32: 30, 34: 1})
    check("exactly one call", len(c.calls) == 1, str(c.calls))
    check("it was the Toughness", c.calls[0]["item_id"] == 32, str(c.calls[0]))
    check("no jelly touched", c.item_map[34] == 180, str(c.item_map[34]))
    check("TP reached 30", c.tp == 30, str(c.tp))


def test_never_batches() -> None:
    print("item_num is always 1 (208 otherwise)")
    c = FakeClient({32: 5})
    c.recovery_tp(count=3, items={32: 30})
    check("three separate calls", len(c.calls) == 3, str(len(c.calls)))
    check("every call sends item_num 1",
          all(p["item_num"] == 1 for p in c.calls), str(c.calls))
    check("client_own_num counts down",
          [p["client_own_num"] for p in c.calls] == [5, 4, 3],
          str([p["client_own_num"] for p in c.calls]))
    check("TP reached 90", c.tp == 90, str(c.tp))


def test_event_items_count() -> None:
    print("event chocolates are TP items too")
    # A bad explicit path is not "no mdb": find_master_mdb still searches the
    # standard install locations, so this must not assert the fallback exactly -
    # it did, and passed only on a machine with no game installed.
    table = racedata.tp_restore_items("/nonexistent/master.mdb")
    check("always returns a usable table, never raises",
          isinstance(table, dict) and table.get(32) == 30, str(table)[:120])
    check("and an RP item is never in it, whichever source was used",
          34 not in table and 35 not in table, str(table)[:120])
    c = FakeClient({10049: 2})
    c.recovery_tp(count=1, items={32: 30, 10049: 30})
    check("a chocolate is spent when it is all there is",
          len(c.calls) == 1 and c.calls[0]["item_id"] == 10049, str(c.calls))
    check("TP reached 30", c.tp == 30, str(c.tp))


def test_shortfall_is_not_an_exception() -> None:
    print("running out is reported, not raised")
    c = FakeClient({32: 1})
    try:
        c.recovery_tp(count=3, items={32: 30}, use_jewels=False)
    except Exception as exc:
        check("no potions + no jewels raises", "jewel" in str(exc).lower(),
              str(exc))
    else:
        check("no potions + no jewels raises", False, "it returned quietly")
    check("but it spent what it had", c.tp == 30, str(c.tp))


def main() -> int:
    for fn in (test_rp_items_are_not_tp_items, test_it_spends_one_toughness,
               test_never_batches, test_event_items_count,
               test_shortfall_is_not_an_exception):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all TP-item checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Real money must never be spent, and a maybe-applied spend must never repeat.

Found by adversarial audit on 2026-09-02, right after AIC_TP_JEWEL_FLOOR was set
to 0 so the farm would "always spend carats if possible".

Carats come in two buckets: ``coin_info["fcoin"]`` is earned in game, and
``coin_info["coin"]`` is bought with real money. The gate only ever measured
fcoin, and only asked whether it was above the floor - not whether it covered
the purchase. At floor 0 that reads ``fcoin > 0``, so ONE free carat authorised a
full-price refill and the server funded the rest from the paid bucket.

Three more, same audit:
  * the post-spend log read fcoin only, so 3 free + 7 paid printed "spent 3" -
    the one record an unattended farm produces of a real-money spend, understating
    it by 70%;
  * ``recovery_tp`` treated a missing tp_info as "this item is not a TP item",
    but ``call()`` RETURNS a raw 208 for item/use_recovery_item rather than
    raising, and a 208 body has no tp_info - so a transient server-busy condemned
    a working Toughness 30 and fell through to buying carats;
  * a network error replayed the identical body up to 8 times, including on the
    two endpoints that move currency.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import flow  # noqa: E402
from aic.config import Settings  # noqa: E402
from vendor.uma_api.client import UmaClient  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

def run_recover(free, paid, tp=9, needed=30, floor="0", price=None, items=None):
    """Drive flow._recover_tp against a fake session; report what it did."""
    import os

    seen = {"called": False}
    old = {k: os.environ.get(k) for k in ("AIC_TP_JEWEL_FLOOR", "AIC_TP_CARAT_PRICE")}
    os.environ["AIC_TP_JEWEL_FLOOR"] = floor
    if price is not None:
        os.environ["AIC_TP_CARAT_PRICE"] = str(price)
    else:
        os.environ.pop("AIC_TP_CARAT_PRICE", None)
    try:
        load_index = {
            "tp_info": {"current_tp": tp},
            "item_list": [{"item_id": i, "number": n} for i, n in (items or {}).items()],
            "user_info": {"viewer_id": 1},
        }
        client = types.SimpleNamespace(coin_info={"fcoin": free, "coin": paid})

        def recovery_tp(count=1, use_jewels=False, items=None):
            seen["called"] = True
            seen["use_jewels"] = use_jewels
            return {}

        client.recovery_tp = recovery_tp
        session = types.SimpleNamespace(client=client, load_index=load_index,
                                        refresh_home=lambda: load_index)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(icarus_root=root, profile="t",
                                runtime_dir=root / "r", state_dir=root / "s")
            flow._recover_tp(session, needed, use_jewels=True)
        return seen
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_paid_carats_are_never_reachable() -> None:
    print("free carats must cover the whole refill")
    # THE BUG: floor 0, one free carat, a pile of paid ones. The old gate
    # (`carats > floor`) said yes and the shortfall came out of real money.
    seen = run_recover(free=1, paid=12_000, price=50)
    check("1 free + 12,000 paid does NOT authorise a spend",
          seen["called"] is False, str(seen))

    seen = run_recover(free=9, paid=12_000, price=50)
    check("9 free (under the 50 price) still refuses", seen["called"] is False,
          str(seen))

    seen = run_recover(free=50, paid=12_000, price=50)
    check("exactly the price is enough", seen["called"] is True, str(seen))

    seen = run_recover(free=60_000, paid=0, price=50)
    check("a healthy free balance spends normally", seen["called"] is True,
          str(seen))

    seen = run_recover(free=0, paid=99_999, price=50)
    check("paid carats alone are never spendable", seen["called"] is False,
          str(seen))


def test_the_floor_still_works() -> None:
    print("the floor is still honoured on top of the price reserve")
    seen = run_recover(free=5_000, paid=0, floor="5000", price=50)
    check("a balance at the floor refuses", seen["called"] is False, str(seen))
    seen = run_recover(free=5_049, paid=0, floor="5000", price=50)
    check("the reserve is measured ABOVE the floor, not through it",
          seen["called"] is False, str(seen))
    seen = run_recover(free=5_051, paid=0, floor="5000", price=50)
    check("clear of both -> spends", seen["called"] is True, str(seen))


def test_price_scales_with_the_shortfall() -> None:
    print("a bigger TP gap reserves more")
    # needed 90 from TP 0 = 3 chunks = 3 x price.
    seen = run_recover(free=100, paid=9_999, tp=0, needed=90, price=50)
    check("100 free cannot cover a 3-chunk (150) purchase",
          seen["called"] is False, str(seen))
    seen = run_recover(free=150, paid=9_999, tp=0, needed=90, price=50)
    check("150 free can", seen["called"] is True, str(seen))


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #

class FakeClient:
    """Drives the real recovery_tp against scripted server replies."""

    def __init__(self, held, replies):
        self.item_map = dict(held)
        self.replies = list(replies)
        self.tp_info = {"current_tp": 0}
        self.coin_info = {"fcoin": 0, "coin": 0}
        self.calls: list[dict] = []

    def call(self, path, payload):
        self.calls.append(dict(payload))
        r = self.replies.pop(0) if self.replies else {"data": {}, "data_headers": {"result_code": 1}}
        if r.get("consume", True):
            self.item_map[payload["item_id"]] = self.item_map.get(payload["item_id"], 0) - 1
        return {"data": r.get("data", {}), "data_headers": r.get("headers", {})}

    recovery_tp = UmaClient.recovery_tp
    TP_RESTORE_FALLBACK = UmaClient.TP_RESTORE_FALLBACK
    CURRENCY_ENDPOINTS = UmaClient.CURRENCY_ENDPOINTS


def test_a_208_is_not_an_rp_item() -> None:
    print("a server-busy 208 must not condemn a working item")
    c = FakeClient({32: 40}, [
        # call() returns the raw 208 for this endpoint: no tp_info in the body.
        {"data": {"tp_info": None}, "headers": {"result_code": 208}},
    ])
    try:
        c.recovery_tp(count=1, items={32: 30}, use_jewels=False)
    except Exception as exc:
        check("it stops with an explicit 'indeterminate' error",
              "indeterminate" in str(exc), str(exc))
    else:
        check("it stops with an explicit 'indeterminate' error", False,
              "returned quietly")
    check("and does NOT keep hammering the item", len(c.calls) == 1, str(c.calls))


def test_a_real_rp_item_stops_everything() -> None:
    print("result_code 1 with no tp_info wastes exactly one item, total")
    # Two held ids, both wrongly listed as TP. The old bail-out broke only the
    # inner loop, so it burned one of EACH.
    c = FakeClient({32: 5, 174: 5}, [
        {"data": {"tp_info": None}, "headers": {"result_code": 1}},
        {"data": {"tp_info": None}, "headers": {"result_code": 1}},
    ])
    try:
        c.recovery_tp(count=1, items={32: 30, 174: 30}, use_jewels=False)
    except Exception:
        pass
    check("exactly ONE item consumed across the whole table",
          len(c.calls) == 1, f"{len(c.calls)} calls: {c.calls}")
    check("the second id was never touched", c.item_map[174] == 5,
          str(c.item_map))


def test_the_happy_path_still_works() -> None:
    print("a normal restore is unaffected")
    c = FakeClient({32: 40}, [
        {"data": {"tp_info": {"current_tp": 39}}, "headers": {"result_code": 1}},
    ])
    c.recovery_tp(count=1, items={32: 30}, use_jewels=False)
    check("one call, one item", len(c.calls) == 1 and c.item_map[32] == 39,
          f"{c.calls} {c.item_map}")
    check("tp_info carried through", c.tp_info["current_tp"] == 39, str(c.tp_info))


def test_currency_endpoints_are_not_replayed() -> None:
    print("a network error never replays a currency spend")
    check("both spends are marked as currency",
          UmaClient.CURRENCY_ENDPOINTS == frozenset(
              {"user/recovery_trainer_point", "item/use_recovery_item"}),
          str(UmaClient.CURRENCY_ENDPOINTS))
    src = Path(__file__).resolve().parent.parent / "vendor" / "uma_api" / "client.py"
    body = src.read_text(encoding="utf-8")
    check("call() caps retries for them before the POST loop",
          "if ep in self.CURRENCY_ENDPOINTS:\n            max_retries = 1" in body,
          "the guard is missing from call()")


def main() -> int:
    for fn in (test_paid_carats_are_never_reachable, test_the_floor_still_works,
               test_price_scales_with_the_shortfall, test_a_208_is_not_an_rp_item,
               test_a_real_rp_item_stops_everything, test_the_happy_path_still_works,
               test_currency_endpoints_are_not_replayed):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all paid-carat checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

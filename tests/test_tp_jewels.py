"""Carats may be spent on TP - but never below the floor. No network.

    python tests/test_tp_jewels.py

Carats are the one resource in this loop that waiting does not replace, and the
loop runs unattended forever. The floor is the difference between "top up TP
when it is short" and "drain the account while nobody is looking".
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import flow  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


class _Env:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = os.environ.get("AIC_TP_JEWEL_FLOOR")
        if self.value is None:
            os.environ.pop("AIC_TP_JEWEL_FLOOR", None)
        else:
            os.environ["AIC_TP_JEWEL_FLOOR"] = str(self.value)

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("AIC_TP_JEWEL_FLOOR", None)
        else:
            os.environ["AIC_TP_JEWEL_FLOOR"] = self.old


def run_recover(carats, potions, use_jewels, tp=10, needed=30):
    """Call _recover_tp against a fake session; return how it called the client."""
    seen = {}
    load_index = {"tp_info": {"current_tp": tp},
                  "item_list": [{"item_id": i, "number": n}
                                # real TP item ids; 12 and 11 do not exist
                                for i, n in zip((32, 174, 10049), potions)],
                  "user_info": {"viewer_id": 1}}
    client = types.SimpleNamespace(coin_info={"fcoin": carats})

    def recovery_tp(count=1, use_jewels=False, items=None):
        seen["called"] = True
        seen["use_jewels"] = use_jewels
        seen["items"] = items
        return {}
    client.recovery_tp = recovery_tp
    session = types.SimpleNamespace(client=client, load_index=load_index,
                                    refresh_home=lambda: load_index)
    flow._recover_tp(session, needed, use_jewels=use_jewels)
    return seen


def test_floor() -> None:
    print("the carat floor")
    with _Env(5000):
        s = run_recover(carats=26_523, potions=(0, 0, 0), use_jewels=True)
        check("well above the floor -> carats allowed",
              s.get("use_jewels") is True, str(s))

        s = run_recover(carats=5000, potions=(0, 0, 0), use_jewels=True)
        check("exactly AT the floor -> refused", s.get("called") is not True, str(s))

        s = run_recover(carats=4999, potions=(0, 0, 0), use_jewels=True)
        check("below the floor -> refused", s.get("called") is not True, str(s))

        s = run_recover(carats=26_523, potions=(0, 0, 0), use_jewels=False)
        check("not asked for -> never spends carats", s.get("called") is not True, str(s))

        s = run_recover(carats=100, potions=(3, 0, 0), use_jewels=True)
        check("under the floor but potions exist -> still recovers, potions only",
              s.get("called") is True and s.get("use_jewels") is False, str(s))


def test_floor_config() -> None:
    print("configuring the floor")
    with _Env(None):
        check("default applies when unset", flow.jewel_floor() == flow.DEFAULT_JEWEL_FLOOR)
    with _Env(0):
        check("0 disables the floor", flow.jewel_floor() == 0)
        # NOT "down to the last carat": a refill the free balance cannot cover
        # is funded from the PAID bucket, so the price is reserved even at
        # floor 0. See tests/test_paid_carats.py.
        s = run_recover(carats=10, potions=(0, 0, 0), use_jewels=True)
        check("but a balance under one refill's price still refuses",
              s.get("called") is not True, str(s))
        s = run_recover(carats=5_000, potions=(0, 0, 0), use_jewels=True)
        check("and a balance that covers it is spent",
              s.get("use_jewels") is True, str(s))
    with _Env("12,000"):
        check("separators parse", flow.jewel_floor() == 12000)
    with _Env("nonsense"):
        check("garbage falls back to the default rather than 0 (which would spend)",
              flow.jewel_floor() == flow.DEFAULT_JEWEL_FLOOR)


def test_no_resources_at_all() -> None:
    print("nothing to spend")
    with _Env(5000):
        s = run_recover(carats=0, potions=(0, 0, 0), use_jewels=True)
        check("no potions and no carats -> no call, no crash",
              s.get("called") is not True, str(s))


def main() -> int:
    for fn in (test_floor, test_floor_config, test_no_resources_at_all):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all TP-jewel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

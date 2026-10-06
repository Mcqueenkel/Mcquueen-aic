"""A Steam rate-limit backs off ALL mints, so the retry storm can't wedge it.

THE WEDGE, measured live 2026-09-12: the stored ticket died, every rotation
visit tried to re-mint, and 13 accounts cycling every ~50s fired ~130 Steam
logins in 2h. Steam returned RateLimitExceeded - which blocks the refresh-token
mint that is the only unattended recovery, so the farm could never climb out.

This pins that one RateLimitExceeded sets a global cooldown during which NO
further mint touches Steam (so the limiter cools), and that the cooldown is
honoured even for a forced mint.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import auth, steam  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


def test_rate_limit_sets_a_global_cooldown() -> None:
    print("a RateLimitExceeded mint blocks every later mint until it cools")
    auth._mint_block_until = 0.0
    steam._SESSION = None  # no live session: force the mint path

    calls = {"acquire": 0}

    def boom(*a, **k):
        calls["acquire"] += 1
        raise RuntimeError("Steam error: RateLimitExceeded")

    orig = steam.acquire
    steam.acquire = boom
    try:
        cfg = {"steam_username": "u", "steam_session_ticket": "DEAD",
               "steam_ticket_ts": int(time.time())}
        settings = types.SimpleNamespace(icarus_root="/x", auth_config_path="/x")

        ok1 = auth.refresh_steam_ticket(cfg, settings, force=True)
        check("first mint returns False (no ticket)", ok1 is False, str(ok1))
        check("it reached Steam exactly once", calls["acquire"] == 1,
              str(calls["acquire"]))
        check("a cooldown is now armed", auth._mint_block_until > time.time(),
              str(auth._mint_block_until - time.time()))

        # A second attempt WHILE cooling must not touch Steam again - even forced.
        ok2 = auth.refresh_steam_ticket(cfg, settings, force=True)
        check("second mint returns False", ok2 is False, str(ok2))
        check("THE WEDGE FIX: Steam was NOT hit again during cooldown",
              calls["acquire"] == 1, str(calls["acquire"]))

        # Once the cooldown lapses, minting is attempted again.
        auth._mint_block_until = time.time() - 1
        auth.refresh_steam_ticket(cfg, settings, force=True)
        check("after the cooldown lapses, Steam is tried again",
              calls["acquire"] == 2, str(calls["acquire"]))
    finally:
        steam.acquire = orig
        auth._mint_block_until = 0.0


def test_cooldown_env_knob() -> None:
    print("AIC_STEAM_MINT_COOLDOWN tunes the cooldown, with a floor")
    import os

    old = os.environ.pop("AIC_STEAM_MINT_COOLDOWN", None)
    try:
        check("default is 30 min", auth.mint_cooldown() == 1800.0)
        os.environ["AIC_STEAM_MINT_COOLDOWN"] = "300"
        check("300 honoured", auth.mint_cooldown() == 300.0)
        os.environ["AIC_STEAM_MINT_COOLDOWN"] = "5"
        check("floored at 60s", auth.mint_cooldown() == 60.0)
        os.environ["AIC_STEAM_MINT_COOLDOWN"] = "junk"
        check("junk falls back to 30m", auth.mint_cooldown() == 1800.0)
    finally:
        os.environ.pop("AIC_STEAM_MINT_COOLDOWN", None)
        if old is not None:
            os.environ["AIC_STEAM_MINT_COOLDOWN"] = old


def main() -> int:
    for fn in (test_rate_limit_sets_a_global_cooldown, test_cooldown_env_knob):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all mint-cooldown checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

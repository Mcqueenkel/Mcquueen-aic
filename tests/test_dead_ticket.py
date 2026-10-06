"""A cancelled Steam ticket must drop the held session. No network.

    python tests/test_dead_ticket.py

MEASURED 2026-09-02: one Steam session minted a ticket, served 16 signups, then
had it cancelled by Steam. SteamSession.alive() only checks that the node
process is running, so has_live_session() kept reporting "live" and
refresh_steam_ticket kept copying the SAME dead ticket into every request:
216 consecutive 391s with no recovery short of restarting the unit.

A 391 from the server is the only reliable signal the ticket is finished.
"""

from __future__ import annotations

import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import auth, bootstrap, steam  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


class FakeError(Exception):
    def __init__(self, code):
        super().__init__(f"API error {code} on tool/signup")
        self.result_code = code


def with_session(live: bool):
    """Patch the steam module's liveness + close, returning a 'closed' flag."""
    state = {"closed": False, "live": live}
    saved = (steam.has_live_session, steam.close)
    steam.has_live_session = lambda: state["live"]

    def close():
        state["closed"] = True
        state["live"] = False
    steam.close = close
    return state, saved


def restore(saved):
    steam.has_live_session, steam.close = saved


def test_391_drops_the_session() -> None:
    print("a 391 drops the held session")
    state, saved = with_session(True)
    try:
        dropped = auth.note_dead_ticket(FakeError(391))
        check("it reports that it dropped one", dropped is True)
        check("and the session really was closed", state["closed"] is True)
    finally:
        restore(saved)


def test_394_drops_the_session() -> None:
    print("a 394 (game session expired) also drops the held session")
    # 2026-09-09: a persistent session's ticket died with 394 and the farm
    # reused it for 8 hours because recovery was gated to 391 alone.
    state, saved = with_session(True)
    try:
        dropped = auth.note_dead_ticket(FakeError(394))
        check("394 reports a drop", dropped is True)
        check("and the session was closed", state["closed"] is True)
    finally:
        restore(saved)


def test_other_codes_leave_it_alone() -> None:
    print("only the dead-ticket codes (391, 394) count")
    for code in (102, 204, 205, 208, 709, 1055, 2509):
        state, saved = with_session(True)
        try:
            dropped = auth.note_dead_ticket(FakeError(code))
            check(f"{code} does not drop the session",
                  dropped is False and state["closed"] is False)
        finally:
            restore(saved)


def test_no_session_is_a_no_op() -> None:
    print("nothing held, nothing to drop")
    state, saved = with_session(False)
    try:
        check("returns False rather than closing nothing",
              auth.note_dead_ticket(FakeError(391)) is False)
        check("and never calls close()", state["closed"] is False)
    finally:
        restore(saved)


def test_never_masks_the_original_error() -> None:
    print("a failure while closing must not raise")
    state, saved = with_session(True)

    def boom():
        raise RuntimeError("node refused to die")
    steam.close = boom
    try:
        check("a broken close() is swallowed",
              auth.note_dead_ticket(FakeError(391)) is True)
    except Exception as exc:  # noqa: BLE001
        check("a broken close() is swallowed", False, repr(exc))
    finally:
        restore(saved)


def test_non_api_exception() -> None:
    print("a plain exception is not a 391")
    state, saved = with_session(True)
    try:
        check("a bare ValueError is ignored",
              auth.note_dead_ticket(ValueError("nope")) is False)
        check("and the session is kept", state["closed"] is False)
    finally:
        restore(saved)


def test_aged_session_is_recycled_proactively() -> None:
    """PREVENTION (2026-09-09): recycle the persistent session before it can 394.

    The held session's ticket was trusted indefinitely, so a stale one wedged
    the farm for 8h. refresh_steam_ticket must now DROP a session older than the
    cap and fall through to a fresh mint, rather than reusing its ticket.
    """
    print("an over-age Steam session is recycled, not reused")
    import os
    from aic import steam

    class FakeLive:
        ticket = "OLD"; steam_id = "1"; refresh_token = ""; started_at = 0.0

    saved = (steam.current, steam.session_age, steam.close, steam.acquire,
             os.environ.get("AIC_STEAM_SESSION_MAX_AGE"))
    state = {"closed": False, "age": 7 * 3600}   # 7h old, over the 6h default
    steam.current = lambda: (None if state["closed"] else FakeLive())
    steam.session_age = lambda: state["age"]
    def _close():
        state["closed"] = True
    steam.close = _close
    # acquire is where a fresh mint would go; raising proves we REACHED it after
    # dropping the aged session (and keeps the test from doing real Steam I/O).
    def _acquire(*a, **k):
        raise steam.SteamGuardRequired("stub: reached the mint path")
    steam.acquire = _acquire
    os.environ.pop("AIC_STEAM_SESSION_MAX_AGE", None)
    try:
        cfg = {"steam_username": "u", "steam_session_ticket": "OLD",
               "steam_refresh_token": "r"}
        # A settings stub real enough for ticket_age()'s stat() call. The
        # SteamGuardRequired from _acquire is swallowed (returns False); all we
        # assert is whether the aged session was dropped first.
        import types, tempfile, os as _os
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        tf.write(b"{}"); tf.close()
        st = types.SimpleNamespace(icarus_root="/x",
                                   auth_config_path=pathlib.Path(tf.name))
        auth.refresh_steam_ticket(cfg, settings=st)
        check("the aged session was closed", state["closed"] is True)

        # a young session must NOT be recycled
        state["closed"] = False; state["age"] = 600   # 10 min
        steam.current = lambda: (None if state["closed"] else FakeLive())
        auth.refresh_steam_ticket(cfg, settings=st)
        check("a fresh session is left alone", state["closed"] is False)
        _os.unlink(tf.name)
    finally:
        (steam.current, steam.session_age, steam.close, steam.acquire, old_env) = (
            saved[0], saved[1], saved[2], saved[3], saved[4])
        if old_env is not None:
            os.environ["AIC_STEAM_SESSION_MAX_AGE"] = old_env


def main() -> int:
    for fn in (test_391_drops_the_session, test_394_drops_the_session, test_aged_session_is_recycled_proactively,
               test_other_codes_leave_it_alone,
               test_no_session_is_a_no_op, test_never_masks_the_original_error,
               test_non_api_exception):
        fn()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all dead-ticket checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

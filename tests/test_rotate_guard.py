"""The rotation must never unlink an account it cannot get back into.

    python tests/test_rotate_guard.py

``account/steam_chain_disconnect`` is the game's "Delete User Data" - permanent,
and recoverable only with a Link Password. ``account.sh logout`` gates it and
``farm()`` gates it; ``rotate._ensure_bound`` did not, and it is the one that runs
unattended. No network: the two calls that would touch it are stubbed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aic import rotate  # noqa: E402
from aic.config import Settings  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


class Recorder:
    """Stands in for aic.account + aic.flow, recording the destructive calls."""

    def __init__(self, known: set[int]):
        self.known = known
        self.disconnected: list[int] = []
        self.logged_in: list[int] = []

    # --- aic.account ---
    def find_account(self, settings, viewer_id):
        return {"trainer_id": str(viewer_id)} if int(viewer_id) in self.known else None

    def steam_disconnect(self, session):
        self.disconnected.append(session.viewer_id)

    def login_with_link(self, settings, trainer_id, password):
        self.logged_in.append(int(trainer_id))

    def archive_auth_config(self, settings, note=""):
        return None

    @staticmethod
    def format_trainer_id(viewer_id):
        return str(viewer_id)


def run_ensure_bound(occupant: int, target_id: int, known: set[int]):
    """Call _ensure_bound with the slot held by ``occupant``. Returns the recorder."""
    rec = Recorder(known)
    settings = Settings(icarus_root=Path("."), profile="t",
                        runtime_dir=Path("."), state_dir=Path("."))
    target = rotate.RosterAccount(trainer_id=target_id, password="pw", name="target")

    saved_acct, saved_flow = rotate.acct, rotate.flow
    rotate.acct = rec
    rotate.flow = types.SimpleNamespace(
        connect=lambda s: types.SimpleNamespace(viewer_id=occupant))
    quiet = types.SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None)
    try:
        rotate._ensure_bound(settings, target, quiet)
        rec.error = None
    except rotate.RotationError as exc:
        rec.error = str(exc)
    finally:
        rotate.acct, rotate.flow = saved_acct, saved_flow
    return rec


def main() -> int:
    print("rotate never deletes an unrecoverable account")

    rec = run_ensure_bound(occupant=111, target_id=222, known=set())
    check("an occupant with no Link Password on record is refused",
          rec.error is not None and "DELETES it" in rec.error, str(rec.error))
    check("and steam_disconnect was NOT called", rec.disconnected == [], str(rec.disconnected))
    check("and no takeover happened either", rec.logged_in == [], str(rec.logged_in))
    check("the message says how to fix it",
          rec.error is not None and "save-link" in rec.error and "remember" in rec.error)

    rec = run_ensure_bound(occupant=111, target_id=222, known={111})
    check("a recoverable occupant still switches", rec.error is None, str(rec.error))
    check("it is disconnected", rec.disconnected == [111], str(rec.disconnected))
    check("and the target is logged in", rec.logged_in == [222], str(rec.logged_in))

    rec = run_ensure_bound(occupant=0, target_id=222, known=set())
    check("a free slot needs no disconnect and no password",
          rec.error is None and rec.disconnected == [] and rec.logged_in == [222])

    rec = run_ensure_bound(occupant=222, target_id=222, known=set())
    check("already on the target is a no-op",
          rec.error is None and rec.disconnected == [] and rec.logged_in == [])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all rotate-guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

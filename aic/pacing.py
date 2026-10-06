"""The global "remove every delay" switch.

Requests are paced in two places: this package's own gaps (between gacha pulls,
between farmed accounts, around signup) and the Icarus client's per-request
floor - a 0.14s minimum between raw calls plus a ~0.166s inter-request sleep,
and 0.83/2.5s gaps inside its signup sequence.

``AIC_NO_DELAY=1`` (or ``--no-delay``) zeroes all of them. It exists to answer
"can this run flat out at all?", and the honest answer from this codebase's own
history is: probably not. The farm module records that at machine speed the edge
(Akamai) starts answering 403, and the gacha sequence in particular is
timing-sensitive - delays over ~10s inside its window return 1808, and removing
them entirely is an equally distinctive pattern. Treat it as an experiment, not
a speed setting.

Icarus is a separate checkout, so its sleeps are neutralised by swapping the
``time`` module *it* looks up for a proxy whose ``sleep`` does nothing, rather
than by editing that tree.
"""

from __future__ import annotations

import os
import time as _time

from .logging_setup import get

log = get("pacing")

ENV_VAR = "AIC_NO_DELAY"

_TRUTHY = {"1", "true", "yes", "on"}


def no_delay() -> bool:
    """Whether every artificial delay should be skipped."""
    return str(os.environ.get(ENV_VAR, "")).strip().lower() in _TRUTHY


def enable() -> None:
    """Turn the switch on for this process (what ``--no-delay`` calls)."""
    os.environ[ENV_VAR] = "1"


def sleep(seconds: float) -> None:
    """``time.sleep``, unless delays are switched off."""
    if seconds > 0 and not no_delay():
        _time.sleep(seconds)


class _NoSleepTime:
    """The stdlib ``time`` module with ``sleep`` neutered.

    Everything else (``time()``, ``monotonic()``, ...) is forwarded, so code
    that measures elapsed time still behaves - it simply never waits.
    """

    def sleep(self, _seconds):  # noqa: D102 - deliberately does nothing
        return None

    def __getattr__(self, name):
        return getattr(_time, name)


def patch_client(client_module) -> bool:
    """Neutralise the Icarus client's internal request pacing.

    Returns True if the patch was applied. Idempotent.
    """
    if not no_delay():
        return False
    if getattr(client_module, "_aic_no_delay", False):
        return True
    client_module.time = _NoSleepTime()
    client_module._aic_no_delay = True
    log.warning("AIC_NO_DELAY: every inter-request delay is removed, including "
                "the Icarus client's 0.14s floor. Expect 403s from the edge, "
                "and 1808 from the gacha if its sequence is disturbed.")
    return True

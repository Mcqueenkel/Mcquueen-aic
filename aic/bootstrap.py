"""Imports the wire-protocol layer into this process.

``uma_api`` is **vendored** at ``vendor/uma_api`` (see its docstring), so this
package is self-contained.

Ordering is load-bearing either way. ``uma_api.client`` and
``career_bot.logging_utils`` both resolve their output root from the
``UMA_RUNTIME_DIR`` environment variable, and ``uma_api.client`` reads it lazily
but ``career_bot`` does not in every path - so the variable must be set *before*
the first import. Everything else in this package goes through
``bind()``/``api()`` rather than importing ``uma_api`` directly, which keeps
that ordering in one place.
"""

from __future__ import annotations

import os
import platform
import sys
from types import SimpleNamespace

from .config import ConfigError, Settings

_BOUND: SimpleNamespace | None = None
_BOUND_ROOT: str | None = None


def bind(settings: Settings) -> SimpleNamespace:
    """Put ``vendor/`` on ``sys.path`` and import what we need from it.

    Returns a namespace of the imported symbols. Safe to call repeatedly; the
    second call with a different checkout is an error because the modules are
    already in ``sys.modules`` and would silently keep pointing at the first.
    """
    global _BOUND, _BOUND_ROOT

    root = str(settings.data_root)
    if _BOUND is not None:
        if _BOUND_ROOT != root:
            raise ConfigError(
                f"client already bound to {_BOUND_ROOT}; cannot rebind to {root} "
                "in the same process."
            )
        return _BOUND

    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    # Must precede the imports below - see the module docstring.
    os.environ["UMA_RUNTIME_DIR"] = str(settings.runtime_dir)

    # The vendored copy is authoritative: one client, always the one that ships
    # with this tool. The data root is used for its *data* only (data/*.json,
    # node_modules) - see aic.config.
    from pathlib import Path

    vendor = str(Path(__file__).resolve().parent.parent / "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    source = vendor

    try:
        from uma_api.client import (  # noqa: E402
            SALT,
            SCENARIO_API_PREFIXES,
            ApiError,
            StateRecoveryError,
            UmaClient,
            error_code,
            get_ticket,
        )
    except ImportError as exc:
        raise ConfigError(
            f"failed to import uma_api.client from {root}: {exc}\n"
            "  Install the requirements into this interpreter (Python 3.12):\n"
            "    pip install -r requirements.txt"
        ) from exc

    if platform.system() != "Windows":
        _patch_for_posix()

    # Opt-in experiment: strip the client's per-request pacing too, not just
    # this package's own gaps. See aic/pacing.py.
    from . import pacing
    if pacing.no_delay():
        import uma_api.client as _client_mod
        pacing.patch_client(_client_mod)

    _BOUND = SimpleNamespace(
        ApiError=ApiError,
        StateRecoveryError=StateRecoveryError,
        UmaClient=UmaClient,
        error_code=error_code,
        get_ticket=get_ticket,
        SCENARIO_API_PREFIXES=SCENARIO_API_PREFIXES,
        SALT=SALT,
        root=root,
    )
    _BOUND_ROOT = root
    return _BOUND


def _patch_for_posix() -> None:
    """Neutralize the one Windows-only call on the paths aic uses.

    ``UmaClient.__init__`` calls ``get_hwid()`` unconditionally to build the
    device-fingerprint *fallbacks*, and on non-Windows that raises before the
    captured values in auth_config ever get a look. Every field it would
    provide is present in a Windows capture, so replace it with a mapping that
    only complains if a field is genuinely absent.
    """
    import uma_api.client as _client

    class _CapturedOnly(dict):
        def __missing__(self, key):
            raise ConfigError(
                f"auth_config.json has no '{key}', and it cannot be generated "
                f"on {platform.system()} (the client reads it from the Windows "
                "registry). Capture on Windows once, then copy the profile."
            )

    _client.get_hwid = lambda seed_string="default": _CapturedOnly()


def api() -> SimpleNamespace:
    """The already-bound namespace. Raises if :func:`bind` has not run."""
    if _BOUND is None:
        raise ConfigError("aic.bootstrap.bind() has not been called yet")
    return _BOUND


def result_code(exc: BaseException):
    """Structured API result code for an exception, or ``None``."""
    if _BOUND is not None:
        return _BOUND.error_code(exc)
    return getattr(exc, "result_code", None)

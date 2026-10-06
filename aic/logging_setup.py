"""Console + file logging, with credential redaction on the console path."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Substrings that mark a value as a secret. Matched case-insensitively against
#: whole log records, so a stray ``print`` of a payload dict still gets masked.
_SECRET_PATTERN = re.compile(
    r"('|\")?(auth_key|steam_session_ticket|steam_id|udid|device_id|sid|"
    r"steam_password_seed|steam_username|credential)\1?\s*[:=]\s*('|\")?([^,}'\"\s]+)",
    re.IGNORECASE,
)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return _SECRET_PATTERN.sub(
            lambda m: f"{m.group(1) or ''}{m.group(2)}{m.group(1) or ''}"
                      f"{'=' if '=' in m.group(0) else ':'} <redacted>",
            text,
        )


def setup(state_dir: Path, verbose: bool = False) -> logging.Logger:
    root = logging.getLogger("aic")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_RedactingFormatter("%(message)s"))
    root.addHandler(console)

    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # ponytail: hard cap, never grows. 3 x 20 MB = 60 MB forever, ~25 days
    # of history at the measured 2.33 MB/day. health_alert.py and the
    # status check read this file, so it must keep existing.
    fh = RotatingFileHandler(log_dir / "aic.log", maxBytes=20 * 1024 * 1024,
                             backupCount=2, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_RedactingFormatter("[%(asctime)s] %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)

    # Icarus logs through its own tree; surface warnings so version/session
    # problems are not swallowed, but keep its DEBUG chatter off the console.
    for name in ("client", "icarus.client", "career_bot"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG if verbose else logging.WARNING)
        lg.handlers.clear()
        lg.addHandler(console)
        lg.addHandler(fh)
        lg.propagate = False

    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"aic.{name}")

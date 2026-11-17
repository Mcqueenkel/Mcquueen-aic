"""Central logging for Icarus.

Two sinks, one call:

* **Console** — keeps the familiar operator-facing output the cmd window has
  always shown (plain messages, WARNING/ERROR prefixed).
* **Debug file** — ``<runtime>/logs/icarus.log`` (rotating, 5 MB x 5) records
  everything at DEBUG with timestamps and module names, so crashes and odd
  server behaviour can be diagnosed after the fact without re-running.

Usage::

    from career_bot.logging_utils import get_logger
    log = get_logger(__name__)          # or get_logger("runner")
    log.info("visible on console + file")
    log.debug("file only (console too with ICARUS_LOG_LEVEL=DEBUG)")

Environment:
    ICARUS_LOG_LEVEL  console verbosity (DEBUG/INFO/WARNING/ERROR, default INFO)
    UMA_RUNTIME_DIR   overrides where the log directory lives

NEVER log credentials or session material (sid, steam_session_ticket,
device_token, viewer_id). Log endpoints, result codes, turns and decisions —
the encrypted-payload capture channel (client.api_log) already exists for
deep protocol work.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT_NAME = "uma"

_configured = False
_log_file = None  # the actual icarus.log path resolved at setup time


def runtime_output_root(base_dir):
    """Resolve the runtime output directory (shared with the runner)."""
    override = os.environ.get("UMA_RUNTIME_DIR")
    if override:
        return Path(override).expanduser().resolve()

    base = Path(base_dir).resolve()
    for candidate in (base, *base.parents):
        if (candidate / ".git").exists():
            return candidate / "uma_runtime"
    # No .git found: keep the runtime INSIDE the bot folder. (Previously fell
    # back to base.parent, which spawned stray uma_runtime folders one level up
    # — in Downloads/, etc. — on installs without a git repo.)
    return base / "uma_runtime"


class _ConsoleFormatter(logging.Formatter):
    """Plain messages at INFO and below; level-prefixed above."""

    def format(self, record):
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"[{record.levelname}] {msg}"
        return msg


class _ConsoleHandler(logging.Handler):
    """Console handler that resolves sys.stdout at EMIT time.

    main.py replaces sys.stdout with a tee (console + timestamped
    logs/*-start.log) AFTER career_bot modules are imported. Binding the
    stream lazily keeps logger output flowing through that tee exactly like
    the print() calls it replaces.
    """

    def emit(self, record):
        try:
            stream = sys.stdout
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 - logging must never kill the bot
            self.handleError(record)


def setup_logging(base_dir=None, console_level=None):
    """Configure the icarus logger tree. Idempotent - safe to call anywhere.

    Returns the root "icarus" logger. Handlers attach only once; later calls
    (e.g. a second CareerRunner) are no-ops.
    """
    global _configured
    root = logging.getLogger(ROOT_NAME)
    if _configured:
        return root

    root.setLevel(logging.DEBUG)
    root.propagate = False  # keep uvicorn/root logging out of the mix

    level_name = (
        console_level or os.environ.get("ICARUS_LOG_LEVEL") or "INFO"
    ).upper()
    console = _ConsoleHandler()
    console.setLevel(getattr(logging, level_name, logging.INFO))
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    try:
        log_dir = runtime_output_root(base_dir or Path(__file__).parent.parent)
        log_dir = log_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        global _log_file
        _log_file = log_dir / "client.log"
        file_handler = RotatingFileHandler(
            _log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)
        root.debug("--- logging initialised (dir: %s) ---", log_dir)
    except Exception as exc:  # noqa: BLE001 - logging must never kill the bot
        root.warning("debug log file unavailable: %s", exc)

    _configured = True
    return root


def get_log_file():
    """Absolute path of the active icarus.log (resolved at logging setup).

    Use this instead of recomputing runtime_output_root() - the env override
    (UMA_RUNTIME_DIR) can be set AFTER logging initialises, so recomputing
    would point at the wrong directory."""
    setup_logging()
    return _log_file


def get_logger(name=None):
    """Child logger under the icarus tree, e.g. get_logger("runner")."""
    setup_logging()
    if not name:
        return logging.getLogger(ROOT_NAME)
    short = name.rsplit(".", 1)[-1] if "." in str(name) else name
    return logging.getLogger(f"{ROOT_NAME}.{short}")

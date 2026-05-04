"""Centralised file logging for cc-monitor.

Logs land in ~/.cache/cc-monitor/usagemonitor.log with a 10MB
rotating cap (one .1 backup, so disk usage tops out at ~20MB total).
Modules grab named child loggers via `get_logger(__name__)` and the
configuration in setup_logging() applies to all descendants.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".cache" / "cc-monitor"
LOG_FILE = LOG_DIR / "usagemonitor.log"
ROOT_LOGGER_NAME = "cc_usagemonitor"

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB hard cap
_BACKUP_COUNT = 1               # one .1 backup, then overwrite
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(debug: bool = False) -> logging.Logger:
    """Wire up the file handler and return the root cc_usagemonitor logger.

    Idempotent — re-running clears any previously installed handlers
    (useful for tests / repeated CLI invocations within one process)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    # Keep records from leaking to the stderr handler that Textual / pytest
    # might attach — file is the sole sink.
    root.propagate = False
    return root


def get_logger(module_name: str) -> logging.Logger:
    """Convenience: returns cc_usagemonitor.<short>. The 'cc_usagemonitor'
    prefix in module_name (when called as get_logger(__name__)) gets
    stripped to keep log lines compact."""
    short = module_name
    prefix = "cc_usagemonitor."
    if short.startswith(prefix):
        short = short[len(prefix):]
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{short}")

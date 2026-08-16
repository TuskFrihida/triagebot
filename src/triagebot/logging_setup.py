"""Logging configuration, applied once at the entrypoint.

Library modules only ever call ``logging.getLogger(__name__)``; they never
configure handlers. That is what lets this project be imported into a larger
application without hijacking its logging.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# These libraries log every HTTP request at INFO. Beyond being noisy, a
# Telegram request URL contains the bot token, so leaving them at INFO would
# write a live credential into the log file in plain text. This is a security
# control, not a cosmetic one.
#
# Note openai 3.x vendors its transport as httpx2/httpcore2, not httpx.
_NOISY_LOGGERS = (
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "urllib3",
    "requests",
    "openai",
    "google",
    "google_auth_httplib2",
    "googleapiclient",
)

_FORMAT = "%(asctime)s  %(levelname)-8s %(name)-22s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure root logging for a command-line run.

    Args:
        level: one of DEBUG, INFO, WARNING, ERROR.
        log_file: optional path to also write logs to, for unattended runs.
    """
    resolved = getattr(logging, level.upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    root = logging.getLogger()
    # Replace rather than append, so repeated calls in tests or a REPL do not
    # duplicate every line.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(resolved)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

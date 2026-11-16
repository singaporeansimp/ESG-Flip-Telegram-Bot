"""The one place logging is configured.

The bot logs through the standard library at INFO level: each flip's lifecycle,
flood-control waits, voided flips, watchdog actions and database errors. It never
logs the bot token or any hand before showdown.

Call :func:`setup_logging` once at start-up, then use :func:`get_logger` (or
``logging.getLogger(__name__)``, which is the same thing) everywhere else.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final, TextIO

__all__ = ["DEFAULT_FORMAT", "LEVEL_ENV_VAR", "get_logger", "setup_logging"]

DEFAULT_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DEFAULT_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
LEVEL_ENV_VAR: Final = "ESGBOT_LOG_LEVEL"
"""Environment variable that overrides the default level, for example ``DEBUG``."""

_LOGGER_NAME: Final = "esgbot"
_configured = False


def setup_logging(
    level: int | str | None = None,
    *,
    stream: TextIO | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure logging for the process and return the ``esgbot`` logger.

    Args:
        level: the level to log at. Defaults to the ``ESGBOT_LOG_LEVEL``
            environment variable, and to INFO when that is unset or unreadable.
        stream: where records go. Defaults to standard error.
        force: replace the handler if logging was already configured here.

    Calling this twice is harmless: the second call only updates the level,
    unless ``force`` is set.
    """
    global _configured
    resolved = _resolve_level(level)
    root = logging.getLogger()
    if _configured and not force:
        root.setLevel(resolved)
        return logging.getLogger(_LOGGER_NAME)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(resolved)
    _configured = True
    return logging.getLogger(_LOGGER_NAME)


def get_logger(name: str | None = None) -> logging.Logger:
    """The logger for a module: ``get_logger(__name__)``, or the ``esgbot`` logger."""
    return logging.getLogger(name if name else _LOGGER_NAME)


def _resolve_level(level: int | str | None) -> int:
    if level is None:
        level = os.environ.get(LEVEL_ENV_VAR, "") or logging.INFO
    if isinstance(level, int):
        return level
    named = logging.getLevelNamesMapping().get(level.strip().upper())
    return named if named is not None else logging.INFO

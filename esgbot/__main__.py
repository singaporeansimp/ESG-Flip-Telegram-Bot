"""``python -m esgbot``: configure logging, read the environment, run the bot.

Everything that can go wrong before the bot is up goes wrong here, with a
message naming what to fix rather than a traceback: a missing token, or a
database file that cannot be opened.
"""

from __future__ import annotations

import asyncio

from esgbot.logging_setup import get_logger, setup_logging
from esgbot.telegram.app import run_bot
from esgbot.telegram.settings import ConfigError, load_settings

__all__ = ["main"]

_EXIT_CONFIG = 2
"""Exit status for a bot that was never configured enough to start."""


def main() -> int:
    """Run the bot. Returns the process's exit status."""
    setup_logging()
    logger = get_logger(__name__)
    try:
        settings = load_settings()
    except ConfigError as error:
        logger.error("%s", error)
        return _EXIT_CONFIG
    logger.info("starting, database at %s", settings.db_path)
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:  # pragma: no cover - only ever from a terminal
        logger.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The bot's configuration: two environment variables and nothing else.

The token is the one secret in the process, so it is read once here and never
logged, printed or put in a :func:`repr`. :class:`BotSettings` hides it even from
its own representation, because a settings object is exactly the kind of thing
that ends up in a log line or a traceback.

The database path has a sensible default; the token has none, and a missing one
is a :class:`ConfigError` naming the variable to set.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DB_PATH_ENV_VAR",
    "DEFAULT_DB_PATH",
    "TOKEN_ENV_VAR",
    "BotSettings",
    "ConfigError",
    "load_settings",
]

TOKEN_ENV_VAR: Final = "ESGBOT_TELEGRAM_TOKEN"
"""The bot token from @BotFather. Required, and never logged."""

DB_PATH_ENV_VAR: Final = "ESGBOT_DB_PATH"
"""Where the SQLite file lives. Optional; see :data:`DEFAULT_DB_PATH`."""

DEFAULT_DB_PATH: Final = "esgbot.sqlite3"
"""The database file the bot uses when ``ESGBOT_DB_PATH`` is unset."""


class ConfigError(RuntimeError):
    """The environment does not say enough to start the bot.

    ``str(error)`` is a complete sentence naming the variable that is missing or
    empty, so ``__main__`` can print it and stop.
    """


@dataclass(frozen=True, slots=True)
class BotSettings:
    """What the bot needs to know before it can talk to anything.

    Raises:
        ValueError: if the token or the database path is blank.
    """

    token: str
    db_path: str

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("the bot token cannot be empty")
        if not self.db_path:
            raise ValueError("the database path cannot be empty")

    def __repr__(self) -> str:
        """Everything but the token, which is never shown."""
        return f"BotSettings(token=<hidden>, db_path={self.db_path!r})"


def load_settings(env: Mapping[str, str] | None = None) -> BotSettings:
    """Read the settings from the environment, or from ``env`` for a test.

    Raises:
        ConfigError: if :data:`TOKEN_ENV_VAR` is unset or empty.
    """
    source = os.environ if env is None else env
    token = source.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise ConfigError(
            f"{TOKEN_ENV_VAR} is not set. Put the token @BotFather gave you in that "
            f"environment variable and start the bot again."
        )
    db_path = source.get(DB_PATH_ENV_VAR, "").strip() or DEFAULT_DB_PATH
    return BotSettings(token=token, db_path=db_path)

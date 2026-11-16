"""Where the bot's configuration comes from, and the one thing it must never show.

The token is the process's only secret. A settings object is exactly what ends
up in a log line or a traceback, so the second half of this file checks that
neither its :func:`repr` nor its :func:`str` gives the token away.
"""

from __future__ import annotations

import dataclasses

import pytest

from esgbot.telegram.settings import (
    DB_PATH_ENV_VAR,
    DEFAULT_DB_PATH,
    TOKEN_ENV_VAR,
    BotSettings,
    ConfigError,
    load_settings,
)

_TOKEN = "123456:secret-token-value"


def test_the_token_and_the_database_path_are_read_from_the_environment() -> None:
    settings = load_settings({TOKEN_ENV_VAR: _TOKEN, DB_PATH_ENV_VAR: "/data/esg.sqlite3"})

    assert settings.token == _TOKEN
    assert settings.db_path == "/data/esg.sqlite3"


def test_the_process_environment_is_used_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, _TOKEN)
    monkeypatch.delenv(DB_PATH_ENV_VAR, raising=False)

    settings = load_settings()

    assert settings.token == _TOKEN
    assert settings.db_path == DEFAULT_DB_PATH


def test_a_missing_database_path_falls_back_to_the_default() -> None:
    assert load_settings({TOKEN_ENV_VAR: _TOKEN}).db_path == DEFAULT_DB_PATH


def test_a_blank_database_path_falls_back_to_the_default() -> None:
    assert load_settings({TOKEN_ENV_VAR: _TOKEN, DB_PATH_ENV_VAR: "  "}).db_path == DEFAULT_DB_PATH


def test_values_are_stripped_of_surrounding_whitespace() -> None:
    settings = load_settings({TOKEN_ENV_VAR: f"  {_TOKEN}\n", DB_PATH_ENV_VAR: "\t/data/esg.db "})

    assert settings.token == _TOKEN
    assert settings.db_path == "/data/esg.db"


@pytest.mark.parametrize("env", [{}, {TOKEN_ENV_VAR: ""}, {TOKEN_ENV_VAR: "  \t\n"}])
def test_a_missing_or_blank_token_is_a_config_error_naming_the_variable(
    env: dict[str, str],
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(env)

    assert TOKEN_ENV_VAR in str(caught.value)


def test_a_config_error_does_not_echo_the_environment() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({TOKEN_ENV_VAR: "   ", DB_PATH_ENV_VAR: "/data/private.db"})

    assert "/data/private.db" not in str(caught.value)


def test_the_token_is_not_in_the_repr() -> None:
    settings = load_settings({TOKEN_ENV_VAR: _TOKEN, DB_PATH_ENV_VAR: "esg.db"})

    assert _TOKEN not in repr(settings)
    assert "esg.db" in repr(settings)


def test_the_token_is_not_in_the_str() -> None:
    settings = load_settings({TOKEN_ENV_VAR: _TOKEN})

    assert _TOKEN not in str(settings)
    assert _TOKEN not in f"{settings}"
    assert _TOKEN not in f"{settings!r}"


def test_the_token_is_not_in_a_traceback_that_carries_the_settings() -> None:
    settings = BotSettings(token=_TOKEN, db_path="esg.db")

    with pytest.raises(dataclasses.FrozenInstanceError) as caught:
        settings.db_path = "elsewhere"  # type: ignore[misc]

    assert _TOKEN not in str(caught.value)


def test_settings_cannot_be_built_with_a_blank_token_or_path() -> None:
    with pytest.raises(ValueError, match="token"):
        BotSettings(token="", db_path="esg.db")
    with pytest.raises(ValueError, match="path"):
        BotSettings(token=_TOKEN, db_path="")

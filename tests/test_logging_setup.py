from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from esgbot import logging_setup as ls


@pytest.fixture(autouse=True)
def _isolated_root_logger(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for handler in original_handlers:
        root.removeHandler(handler)
    monkeypatch.setattr(ls, "_configured", False)

    yield

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


def test_setup_logging_writes_to_given_stream() -> None:
    stream = io.StringIO()
    logger = ls.setup_logging(stream=stream)
    logger.info("hello from esgbot")
    assert "hello from esgbot" in stream.getvalue()


def test_setup_logging_returns_esgbot_logger() -> None:
    logger = ls.setup_logging(stream=io.StringIO())
    assert logger.name == "esgbot"


def test_setup_logging_is_idempotent_no_duplicate_handlers() -> None:
    stream = io.StringIO()
    ls.setup_logging(stream=stream)
    ls.setup_logging(stream=stream)
    assert len(logging.getLogger().handlers) == 1


def test_setup_logging_second_call_updates_level_without_new_handler() -> None:
    stream = io.StringIO()
    ls.setup_logging(logging.INFO, stream=stream)
    ls.setup_logging(logging.ERROR, stream=stream)
    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger().level == logging.ERROR


def test_setup_logging_force_replaces_handler() -> None:
    stream = io.StringIO()
    ls.setup_logging(stream=stream)
    ls.setup_logging(stream=stream, force=True)
    assert len(logging.getLogger().handlers) == 1


def test_setup_logging_honours_explicit_level() -> None:
    ls.setup_logging(logging.DEBUG, stream=io.StringIO())
    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_honours_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ls.LEVEL_ENV_VAR, "WARNING")
    ls.setup_logging(stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING


def test_setup_logging_env_var_unset_falls_back_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ls.LEVEL_ENV_VAR, raising=False)
    ls.setup_logging(stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_unknown_level_name_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ls.LEVEL_ENV_VAR, "NOT_A_LEVEL")
    ls.setup_logging(stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO


def test_get_logger_default_name() -> None:
    assert ls.get_logger().name == "esgbot"


def test_get_logger_named() -> None:
    assert ls.get_logger(__name__).name == __name__

    assert ls.get_logger(None).name == "esgbot"

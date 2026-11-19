"""``/autopeek``: asking to be counted as having peeked, and what the command says."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from esgbot.storage import queries, schema
from tests.telegram_helpers import BotHarness, bot_harness, group_message, user

_PAT, _LEE = 71, 72


async def _say(world: BotHarness, text: str, user_id: int, name: str) -> str:
    await world.feed(message=group_message(text, sender=user(user_id, name=name)))
    return world.client.sends[-1].text


def test_on_off_and_asking_what_is_set_each_say_where_the_player_stands() -> None:
    async def scenario() -> None:
        world = bot_harness()
        assert "off" in (await _say(world, "/autopeek", _PAT, "Pat")).lower()
        assert "Auto-peek is on" in await _say(world, "/autopeek on", _PAT, "Pat")
        assert "Auto-peek is on" in await _say(world, "/autopeek ON", _PAT, "Pat")
        assert "Auto-peek is on" in await _say(world, "/autopeek", _PAT, "Pat")
        assert "Auto-peek is off" in await _say(world, "/autopeek", _LEE, "Lee")
        assert "Auto-peek is off" in await _say(world, "/autopeek off", _PAT, "Pat")
        assert "Auto-peek is off" in await _say(world, "/autopeek", _PAT, "Pat")
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/autopeek maybe", "/autopeek on off", "/autopeek 5"])
def test_what_cannot_be_read_sets_nothing_and_says_how(text: str) -> None:
    async def scenario() -> None:
        world = bot_harness()
        reply = await _say(world, text, _PAT, "Pat")

        assert "/autopeek on" in reply
        count = world.store.connection.execute("SELECT COUNT(*) FROM autopeek").fetchone()[0]
        assert count == 0
        await world.close()

    asyncio.run(scenario())


def test_auto_peek_in_a_private_chat_sets_nothing_and_a_failing_database_is_answered() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await world.feed(message=group_message("/autopeek on", chat_type="private"))
        count = world.store.connection.execute("SELECT COUNT(*) FROM autopeek").fetchone()[0]
        assert count == 0

        world.store.autojoin_error = sqlite3.OperationalError("disk I/O error")
        assert "database" in await _say(world, "/autopeek on", _PAT, "Pat")
        await world.close()

    asyncio.run(scenario())


def test_auto_peek_belongs_to_one_group_and_follows_it_to_a_new_id() -> None:
    connection = schema.connect(":memory:")
    schema.apply_migrations(connection)
    queries.set_autopeek(connection, group_id=1, user_id=_PAT, on=True)
    queries.set_autopeek(connection, group_id=1, user_id=_PAT, on=True)  # twice is once
    queries.set_autopeek(connection, group_id=1, user_id=_LEE, on=True)
    queries.set_autopeek(connection, group_id=2, user_id=_PAT, on=True)
    assert queries.autopeekers(connection, group_id=1, user_ids=[_PAT, _LEE, 5]) == {_PAT, _LEE}
    assert queries.autopeekers(connection, group_id=2, user_ids=[_PAT, _LEE]) == {_PAT}

    queries.migrate_group_id(connection, old_group_id=1, new_group_id=2)

    assert queries.autopeekers(connection, group_id=2, user_ids=[_PAT, _LEE]) == {_PAT, _LEE}
    assert queries.autopeekers(connection, group_id=1, user_ids=[_PAT, _LEE]) == frozenset()
    queries.set_autopeek(connection, group_id=2, user_id=_LEE, on=False)
    assert not queries.autopeek_on(connection, group_id=2, user_id=_LEE)
    connection.close()

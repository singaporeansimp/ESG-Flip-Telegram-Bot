"""``/autojoin``: asking to be put into a group's flips up to a wager, and being put in."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from esgbot.storage import AutoJoiner, queries, schema
from tests.engine_helpers import GROUP
from tests.telegram_helpers import (
    STARTER,
    BotHarness,
    bot_harness,
    group_message,
    migration_message,
    user,
)

_PAT, _LEE = 71, 72


async def _say(world: BotHarness, text: str, user_id: int, name: str) -> str:
    """Send ``text`` as this user and return what the bot said back."""
    await world.feed(message=group_message(text, sender=user(user_id, name=name)))
    return world.client.sends[-1].text


def _seated(world: BotHarness, group_id: int = GROUP) -> list[int]:
    flip = world.registry.flip_for_group(group_id)
    assert flip is not None
    return [player.user_id for player in flip.players]


def test_a_player_is_seated_in_flips_at_or_under_their_wager_and_not_above_it() -> None:
    async def scenario() -> None:
        world = bot_harness()
        assert "$50 or less" in await _say(world, "/autojoin 50", _PAT, "Pat")
        assert "$50 or less" in await _say(world, "/autojoin", _PAT, "Pat")

        await world.flip_command("/flip 50 5")
        assert _seated(world) == [STARTER, _PAT]
        lobby = world.client.sends[-1]
        assert "Pat" in lobby.text
        flip = world.registry.flip_for_group(GROUP)
        assert flip is not None
        await flip.cancel(STARTER)

        await world.flip_command("/flip 51 5")
        assert _seated(world) == [STARTER]
        await world.close()

    asyncio.run(scenario())


def test_off_stops_it_and_a_new_wager_replaces_the_old_one() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _say(world, "/autojoin 50", _PAT, "Pat")
        await _say(world, "/autojoin 10", _PAT, "Pat")
        await _say(world, "/autojoin 100", _LEE, "Lee")
        assert "off" in (await _say(world, "/autojoin OFF", _LEE, "Lee")).lower()
        assert "off" in (await _say(world, "/autojoin", _LEE, "Lee")).lower()

        await world.flip_command("/flip 20 5")

        assert _seated(world) == [STARTER]
        await world.close()

    asyncio.run(scenario())


def test_the_starter_is_seated_once_and_the_player_cap_holds_in_asking_order() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _say(world, "/autojoin 100", STARTER, "Sam")
        for joiner in range(30):
            await _say(world, "/autojoin 100", 100 + joiner, f"P{joiner}")

        await world.flip_command("/flip 50 21")  # 42 // 21: two players fit

        assert _seated(world) == [STARTER, 100]
        await world.close()

    asyncio.run(scenario())


def test_auto_join_belongs_to_one_group_and_follows_it_to_a_new_id() -> None:
    async def scenario() -> None:
        world = bot_harness()
        other, moved = GROUP - 1, GROUP - 2
        await _say(world, "/autojoin 50", _PAT, "Pat")

        await world.feed(message=group_message("/flip 50 5", chat_id=other))
        assert _seated(world, other) == [STARTER]

        await world.feed(message=migration_message(new_group_id=moved))
        await world.feed(message=group_message("/flip 50 5", chat_id=moved))
        assert _seated(world, moved) == [STARTER, _PAT]
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text", ["/autojoin 0", "/autojoin fifty", "/autojoin 5 5", "/autojoin -3"]
)
def test_a_wager_that_cannot_be_read_sets_nothing_and_says_how(text: str) -> None:
    async def scenario() -> None:
        world = bot_harness()
        reply = await _say(world, text, _PAT, "Pat")

        assert "/autojoin &lt;wager&gt;" in reply
        assert queries.autojoiners(world.store.connection, group_id=GROUP, wager=1) == ()
        await world.close()

    asyncio.run(scenario())


def test_auto_join_in_a_private_chat_sets_nothing() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await world.feed(message=group_message("/autojoin 50", chat_type="private"))

        count = world.store.connection.execute("SELECT COUNT(*) FROM autojoin").fetchone()[0]
        assert count == 0
        await world.close()

    asyncio.run(scenario())


def test_a_failing_database_still_opens_the_flip_and_answers_the_command() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _say(world, "/autojoin 50", _PAT, "Pat")
        world.store.autojoin_error = sqlite3.OperationalError("disk I/O error")

        assert "database" in await _say(world, "/autojoin 60", _LEE, "Lee")
        await world.flip_command("/flip 50 5")

        assert _seated(world) == [STARTER]
        await world.close()

    asyncio.run(scenario())


def test_a_damaged_row_costs_one_player_their_seat_and_migration_keeps_the_new_ids_row() -> None:
    connection = schema.connect(":memory:")
    schema.apply_migrations(connection)
    for group_id, user_id, wager in ((1, _PAT, 50), (1, _LEE, 50), (2, _PAT, 99)):
        queries.set_autojoin(
            connection,
            group_id=group_id,
            joiner=AutoJoiner(user_id=user_id, display_name="N", username=None, max_wager=wager),
        )
    queries.migrate_group_id(connection, old_group_id=1, new_group_id=2)
    assert queries.autojoin_limit(connection, group_id=2, user_id=_PAT) == 99
    assert queries.autojoin_limit(connection, group_id=2, user_id=_LEE) == 50
    assert connection.execute("SELECT COUNT(*) FROM autojoin WHERE group_id = 1").fetchone()[0] == 0

    connection.execute("UPDATE autojoin SET display_name = '' WHERE user_id = ?", (_LEE,))
    assert [j.user_id for j in queries.autojoiners(connection, group_id=2, wager=50)] == [_PAT]
    assert queries.clear_autojoin(connection, group_id=2, user_id=_PAT)
    assert not queries.clear_autojoin(connection, group_id=2, user_id=_PAT)
    connection.close()

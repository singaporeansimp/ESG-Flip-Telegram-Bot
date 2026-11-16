"""What the bot does about the flips a restart left behind.

Live flip state is in memory only, so every row the last run left in the
database is a flip that can never finish. The sweep takes the buttons off its
message, tells the group and forgets the row, and must never be the reason the
bot fails to start.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest

from esgbot.engine import Flip, FlipPhase
from esgbot.game import fairness
from esgbot.storage import ActiveFlip, StorageError
from esgbot.telegram.keyboards import Press, pack
from esgbot.telegram.sweep import void_leftover_flips
from tests.telegram_helpers import (
    TOPIC,
    BotHarness,
    active_row,
    bot_harness,
    button_press,
    kicked_out,
)

_SEED = bytes(range(32))
_OTHER_GROUP = -2002


async def _sweep(world: BotHarness) -> int:
    return await void_leftover_flips(store=world.store, gateway=world.gateway)


def _leave(world: BotHarness, *rows: ActiveFlip) -> None:
    """Put rows in the store, as the last run would have left them."""
    for row in rows:
        world.store.active[row.token] = row


def test_a_leftover_lobby_and_a_leftover_running_flip_are_both_cleaned_up() -> None:
    async def scenario() -> None:
        world = bot_harness()
        lobby = active_row(token="lobby", message_id=500, topic_id=TOPIC)
        running = active_row(
            token="running", group_id=_OTHER_GROUP, message_id=610, running=True, seed=_SEED
        )
        _leave(world, lobby, running)

        swept = await _sweep(world)

        assert swept == 2
        markups = {call.message_id: call for call in world.client.of("markup")}
        assert set(markups) == {500, 610}
        assert all(call.markup is None for call in markups.values())
        assert markups[500].chat_id == lobby.group_id
        assert markups[610].chat_id == _OTHER_GROUP

        notices = {call.chat_id: call for call in world.client.sends}
        assert set(notices) == {lobby.group_id, _OTHER_GROUP}
        assert notices[lobby.group_id].thread_id == TOPIC
        assert notices[_OTHER_GROUP].thread_id is None
        assert all("voided" in call.text for call in notices.values())
        assert all(call.markup is None for call in notices.values())
        assert world.store.active == {}
        await world.close()

    asyncio.run(scenario())


def test_only_the_notice_for_a_running_flip_reveals_its_seed() -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(
            world,
            active_row(token="lobby", message_id=500),
            active_row(
                token="running", group_id=_OTHER_GROUP, message_id=610, running=True, seed=_SEED
            ),
        )

        await _sweep(world)

        by_chat = {call.chat_id: call.text for call in world.client.sends}
        assert fairness.seed_to_hex(_SEED) in by_chat[_OTHER_GROUP]
        assert "No cards had been dealt" in by_chat[world.group_id]
        assert "seed" not in by_chat[world.group_id].lower()
        await world.close()

    asyncio.run(scenario())


def test_the_buttons_come_off_before_the_group_is_told() -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(world, active_row(token="lobby", message_id=500))

        await _sweep(world)

        assert [call.kind for call in world.client.calls] == ["markup", "send"]
        await world.close()

    asyncio.run(scenario())


def test_a_swept_flip_is_not_recorded_in_stats() -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(
            world,
            active_row(token="lobby", message_id=500),
            active_row(token="running", group_id=_OTHER_GROUP, message_id=610, running=True),
        )

        await _sweep(world)

        assert world.store.recorded == []
        await world.close()

    asyncio.run(scenario())


async def _run_to_result(world: BotHarness) -> Flip:
    """Open a two-player flip, start it and let it play out."""
    await world.flip_command()
    flip = world.registry.flip_for_group(world.group_id)
    assert flip is not None
    for press, user_id in ((Press.JOIN, 12),):
        await world.feed(
            callback_query=button_press(pack(press, flip.token), user_id=user_id, press_id="j")
        )
    await world.feed(callback_query=button_press(pack(Press.START, flip.token), press_id="s"))
    for _ in range(40):
        if flip.closed:
            break
        await world.clock.advance_to_next()
    return flip


def test_a_flip_that_was_recorded_is_never_announced_as_voided() -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.remove_error = sqlite3.OperationalError("disk I/O error")

        flip = await _run_to_result(world)

        assert flip.phase is FlipPhase.FINISHED
        assert len(world.store.recorded) == 1
        assert world.store.active == {}
        sends_before = len(world.client.sends)

        assert await _sweep(world) == 0

        assert len(world.client.sends) == sends_before
        assert not any("voided" in text for text in world.client.texts)
        await world.close()

    asyncio.run(scenario())


def test_a_flip_whose_record_failed_and_whose_row_stayed_is_announced_as_voided() -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.record_error = sqlite3.OperationalError("disk I/O error")
        world.store.remove_error = sqlite3.OperationalError("disk I/O error")

        flip = await _run_to_result(world)

        assert world.store.recorded == []
        assert flip.token in world.store.active
        sends_before = len(world.client.sends)

        assert await _sweep(world) == 1

        notice = world.client.sends[sends_before:]
        assert len(notice) == 1
        assert "voided" in notice[0].text
        await world.close()

    asyncio.run(scenario())


def test_an_empty_store_sweeps_nothing_and_posts_nothing() -> None:
    async def scenario() -> None:
        world = bot_harness()

        assert await _sweep(world) == 0

        assert world.client.calls == []
        assert world.client.attempts == 0
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [StorageError("the database is locked"), sqlite3.OperationalError("disk I/O error")],
    ids=["storage-error", "sqlite-error"],
)
def test_a_database_that_cannot_be_listed_is_logged_and_the_bot_still_starts(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(world, active_row(token="lobby", message_id=500))
        world.store.list_error = error

        with caplog.at_level(logging.ERROR):
            assert await _sweep(world) == 0

        assert str(error) in caplog.text
        assert world.client.calls == []
        assert "lobby" in world.store.active
        await world.close()

    asyncio.run(scenario())


def test_a_group_the_bot_was_thrown_out_of_does_not_stop_the_sweep() -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(
            world,
            active_row(token="gone", group_id=_OTHER_GROUP, message_id=610),
            active_row(token="here", message_id=500),
        )
        # Three attempts at the first group's call, all refused; then everything works.
        world.client.failures = [kicked_out()] * 3

        swept = await _sweep(world)

        assert swept == 2
        assert [call.chat_id for call in world.client.sends] == [world.group_id]
        assert [call.message_id for call in world.client.of("markup")] == [500]
        assert world.store.active == {}
        await world.close()

    asyncio.run(scenario())


def test_every_row_is_dropped_even_when_no_group_can_be_reached() -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(
            world,
            active_row(token="one", message_id=500),
            active_row(token="two", message_id=501, running=True),
        )
        world.client.failures = [kicked_out()] * 12

        assert await _sweep(world) == 2

        assert world.client.calls == []
        assert world.store.active == {}
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [StorageError("the database is locked"), sqlite3.OperationalError("disk I/O error")],
    ids=["storage-error", "sqlite-error"],
)
def test_a_row_that_cannot_be_dropped_is_logged_and_does_not_raise(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(
            world,
            active_row(token="one", message_id=500),
            active_row(token="two", group_id=_OTHER_GROUP, message_id=610),
        )
        world.store.remove_error = error

        with caplog.at_level(logging.ERROR):
            swept = await _sweep(world)

        assert swept == 2
        assert str(error) in caplog.text
        assert "one" in caplog.text
        assert "two" in caplog.text
        assert len(world.client.sends) == 2
        await world.close()

    asyncio.run(scenario())


def test_a_press_on_a_swept_flip_is_answered_as_expired() -> None:
    async def scenario() -> None:
        world = bot_harness()
        _leave(
            world,
            active_row(token="lobby", message_id=500),
            active_row(token="running", group_id=_OTHER_GROUP, message_id=610, running=True),
        )
        await _sweep(world)
        before = world.client.calls[:]

        presses = []
        for token in ("lobby", "running"):
            for press in (Press.JOIN, Press.START, Press.PEEK):
                press_id = world.press_id()
                presses.append(press_id)
                await world.feed(callback_query=button_press(pack(press, token), press_id=press_id))

        answers = world.client.answers
        assert [call.press_id for call in answers] == presses
        assert all("expired" in call.text for call in answers)
        assert all(not call.alert for call in answers)
        assert world.client.calls[: len(before)] == before
        assert len(world.client.calls) == len(before) + len(presses)
        assert world.registry.flips == ()
        await world.close()

    asyncio.run(scenario())

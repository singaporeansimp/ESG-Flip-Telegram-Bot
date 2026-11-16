"""What the bot does when Telegram hands it a command, a button press or a migration.

Every test feeds real aiogram updates through a real dispatcher wired the way
``run_bot`` wires it, with a fake Telegram on the far side, so what is asserted
is what a group would have seen: the messages sent, the edits made, the pop-ups
answered. The engine's own rules are in ``test_engine_*.py``; what is pinned
here is that the handlers read the command and the callback data correctly and
route everything to the right flip, the right topic and the right answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3

import pytest
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.methods import EditMessageText
from aiogram.types import Message

from esgbot.engine import Flip, FlipPhase, Notice, timing
from esgbot.game import rules
from esgbot.game.deal import deal_cards
from esgbot.storage import FlipState, StorageError
from esgbot.telegram import keyboards, render
from esgbot.telegram.keyboards import Press, pack
from tests.engine_helpers import GROUP, SETTINGS, use_seed
from tests.telegram_helpers import (
    STARTER,
    TOPIC,
    BotHarness,
    Call,
    FakeTelegram,
    bot_harness,
    button_press,
    group_message,
    migration_message,
    topic_message,
)

_JOINER = 12
_STRANGER = 99
_SEED = bytes(range(32))
_NEW_GROUP = -1009999


async def _open(world: BotHarness, *, topic_id: int | None = None) -> Flip:
    """Send ``/flip 50 5 2 3`` and return the flip it opened."""
    await world.flip_command(topic_id=topic_id)
    flip = world.registry.flip_for_group(world.group_id)
    assert flip is not None
    return flip


async def _press(
    world: BotHarness, flip: Flip, press: Press, *, user_id: int = STARTER, name: str = "Sam"
) -> Call:
    """Press a button on this flip's message and return the one answer it got.

    Raises:
        AssertionError: if the press was not answered exactly once.
    """
    press_id = world.press_id()
    await world.feed(
        callback_query=button_press(
            pack(press, flip.token), user_id=user_id, press_id=press_id, name=name
        )
    )
    matching = [call for call in world.client.answers if call.press_id == press_id]
    assert len(matching) == 1, f"press {press_id} was answered {len(matching)} times"
    return matching[0]


async def _play_out(world: BotHarness, flip: Flip, *, limit: int = 40) -> None:
    """Advance the engine's clock until the flip is over."""
    for _ in range(limit):
        if flip.closed:
            return
        assert await world.clock.advance_to_next() != 0.0 or flip.closed
    raise AssertionError(f"flip {flip.token} did not finish in {limit} steps")


async def _two_players(world: BotHarness) -> Flip:
    """A lobby with the starter and one joiner in it."""
    flip = await _open(world)
    await _press(world, flip, Press.JOIN, user_id=_JOINER, name="Pat")
    return flip


def _tokens(text: str) -> set[str]:
    """The words of a pop-up, with the new-card marker taken off."""
    return {word.rstrip("*") for word in text.split()}


# --------------------------------------------------------------------- /flip


def test_flip_opens_one_lobby_with_four_buttons_and_the_starter_in_it() -> None:
    async def scenario() -> None:
        world = bot_harness()

        flip = await _open(world)

        assert len(world.client.sends) == 1
        lobby = world.client.sends[0]
        assert lobby.chat_id == GROUP
        assert lobby.thread_id is None
        assert len(lobby.buttons) == 4
        assert flip.settings == SETTINGS
        assert flip.starter.user_id == STARTER
        assert [seated.user_id for seated in flip.players] == [STARTER]
        assert flip.message_id == lobby.message_id
        assert world.registry.flips == (flip,)
        row = world.store.active[flip.token]
        assert (row.group_id, row.topic_id, row.state) == (GROUP, None, FlipState.LOBBY)
        assert row.message_id == lobby.message_id
        await world.close()

    asyncio.run(scenario())


def test_flip_carries_the_senders_name_and_username_onto_the_player() -> None:
    async def scenario() -> None:
        world = bot_harness()

        flip = await _open(world)

        assert flip.starter.display_name == "Sam"
        assert flip.starter.username == "sam"
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("command", "complaint"),
    [
        pytest.param("/flip", "Give four whole numbers", id="no-arguments"),
        pytest.param("/flip 50 5 2", "Give four whole numbers", id="too-few"),
        pytest.param("/flip 50 5 2 3 1", "Give four whole numbers", id="too-many"),
        pytest.param("/flip abc 5 2 3", "wager must be", id="wager-not-digits"),
        pytest.param("/flip 50 five 2 3", "X must be", id="x-not-digits"),
        pytest.param("/flip $50 5 2 3", "wager must be", id="dollar-sign"),
        pytest.param("/flip 50 5.0 2 3", "X must be", id="decimal-point"),
        pytest.param("/flip 1,000 5 2 3", "wager must be", id="thousands-separator"),
        pytest.param("/flip -50 5 2 3", "wager must be", id="negative-wager"),
        pytest.param("/flip 50 5 -2 3", "Y must be", id="negative-y"),
        pytest.param(f"/flip {rules.WAGER_MIN - 1} 5 2 3", "wager must be", id="wager-too-small"),
        pytest.param(f"/flip {rules.WAGER_MAX + 1} 5 2 3", "wager must be", id="wager-too-big"),
        pytest.param(f"/flip 50 {rules.X_MIN - 1} 2 3", "X must be", id="x-too-small"),
        pytest.param(f"/flip 50 {rules.X_MAX + 1} 2 3", "X must be", id="x-too-big"),
        pytest.param(f"/flip 50 5 {rules.Y_MAX + 1} 3", "Y must be", id="y-too-big"),
        pytest.param(f"/flip 50 5 2 {rules.Z_MIN - 1}", "Z must be", id="z-too-small"),
        pytest.param(f"/flip 50 5 2 {rules.Z_MAX + 1}", "Z must be", id="z-too-big"),
    ],
)
def test_flip_with_bad_settings_names_the_problem_and_opens_nothing(
    command: str, complaint: str
) -> None:
    async def scenario() -> None:
        world = bot_harness()

        await world.flip_command(command)

        assert len(world.client.sends) == 1
        reply = world.client.sends[0]
        assert complaint in reply.text
        assert "/flip 50 5 2 3" in reply.text
        assert reply.markup is None
        assert world.registry.flips == ()
        assert world.store.active == {}
        await world.close()

    asyncio.run(scenario())


def test_the_settings_complaint_echoes_nothing_that_could_inject_markup() -> None:
    async def scenario() -> None:
        world = bot_harness()

        await world.flip_command("/flip <b>50 5 2 3")

        assert "<b>50" not in world.client.sends[0].text
        assert "&lt;b&gt;50" in world.client.sends[0].text
        await world.close()

    asyncio.run(scenario())


def test_a_second_flip_in_the_same_group_is_refused_and_the_first_is_untouched() -> None:
    async def scenario() -> None:
        world = bot_harness()
        first = await _open(world)
        sends_before = len(world.client.sends)

        await world.feed(message=group_message("/flip 10 6 1 2"))

        assert world.registry.flips == (first,)
        assert world.registry.flip_for_group(GROUP) is first
        assert first.settings == SETTINGS
        assert len(world.client.sends) == sends_before + 1
        refusal = world.client.sends[-1]
        assert "already open" in refusal.text
        assert "$50 a head" in refusal.text
        assert "$10" not in refusal.text
        assert refusal.markup is None
        assert world.client.edits == ()
        assert len(world.store.active) == 1
        await world.close()

    asyncio.run(scenario())


def test_each_group_can_have_its_own_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _open(world)

        await world.feed(message=group_message("/flip 50 5 2 3", chat_id=-2002))

        assert len(world.registry.flips) == 2
        assert [call.chat_id for call in world.client.sends] == [GROUP, -2002]
        await world.close()

    asyncio.run(scenario())


def test_flip_in_a_private_chat_says_flips_need_a_group_and_opens_nothing() -> None:
    async def scenario() -> None:
        world = bot_harness()

        await world.feed(message=group_message("/flip 50 5 2 3", chat_type="private"))

        assert len(world.client.sends) == 1
        reply = world.client.sends[0]
        assert "group" in reply.text
        assert reply.markup is None
        assert world.registry.flips == ()
        assert world.store.active == {}
        await world.close()

    asyncio.run(scenario())


def test_flip_with_a_failing_database_says_so_and_leaves_no_flip_behind() -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.add_error = StorageError("the database is locked")

        await world.flip_command()

        assert world.client.sends[-1].text == render.STORAGE_TROUBLE
        assert "database" in world.client.sends[-1].text
        assert world.registry.flips == ()
        assert world.store.active == {}
        # The lobby goes up before its row can be written, so when the write
        # fails it is closed again rather than left in the group with four
        # buttons and no flip behind them.
        assert [call.kind for call in world.client.calls] == ["send", "edit", "send"]
        closed = world.client.edits[0]
        assert closed.buttons == ()
        assert "voided" in closed.text

        world.store.add_error = None
        await world.flip_command()
        assert world.registry.flip_for_group(GROUP) is not None
        await world.close()

    asyncio.run(scenario())


def test_flip_with_a_failing_sqlite_database_is_told_the_same_way() -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.add_error = sqlite3.OperationalError("disk I/O error")

        await world.flip_command()

        assert world.client.sends[-1].text == render.STORAGE_TROUBLE
        assert world.registry.flips == ()
        await world.close()

    asyncio.run(scenario())


# -------------------------------------------------------------------- topics


def test_a_flip_started_in_a_topic_posts_everything_into_that_topic() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world, topic_id=TOPIC)
        assert flip.topic_id == TOPIC
        assert world.store.active[flip.token].topic_id == TOPIC

        await _press(world, flip, Press.JOIN, user_id=_JOINER, name="Pat")
        await _press(world, flip, Press.START)
        await _play_out(world, flip)

        sends = world.client.sends
        texts = [call.text for call in sends]
        assert any("New ESG flip" in text for text in texts)
        assert any("Deck commitment" in text for text in texts)
        assert any("result</b>" in text for text in texts)
        assert len(sends) >= 3
        assert {call.thread_id for call in sends} == {TOPIC}
        assert {call.chat_id for call in sends} == {GROUP}
        await world.close()

    asyncio.run(scenario())


def test_an_error_reply_to_a_command_in_a_topic_goes_to_that_topic() -> None:
    async def scenario() -> None:
        world = bot_harness()

        await world.feed(message=topic_message("/flip 0 5 2 3"))
        await world.feed(message=topic_message("/flip 50 5 2 3", topic_id=TOPIC + 1))
        await world.feed(message=topic_message("/flip 50 5 2 3", topic_id=TOPIC + 2))

        assert [call.thread_id for call in world.client.sends] == [TOPIC, TOPIC + 1, TOPIC + 2]
        assert "already open" in world.client.sends[-1].text
        await world.close()

    asyncio.run(scenario())


def test_a_command_in_a_plain_group_has_no_topic() -> None:
    async def scenario() -> None:
        world = bot_harness()

        await world.feed(message=group_message("/flip 0 5 2 3"))
        await world.flip_command()

        assert [call.thread_id for call in world.client.sends] == [None, None]
        flip = world.registry.flip_for_group(GROUP)
        assert flip is not None
        assert flip.topic_id is None
        await world.close()

    asyncio.run(scenario())


def test_a_reply_thread_in_a_group_without_topics_is_not_taken_for_a_topic() -> None:
    async def scenario() -> None:
        world = bot_harness()
        reply_thread = group_message("/flip 50 5 2 3").model_copy(update={"message_thread_id": 5})

        await world.feed(message=reply_thread)

        flip = world.registry.flip_for_group(GROUP)
        assert flip is not None
        assert flip.topic_id is None
        assert world.client.sends[0].thread_id is None
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------- presses


def test_join_adds_the_presser_and_edits_the_lobby() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)

        answer = await _press(world, flip, Press.JOIN, user_id=_JOINER, name="Pat")

        assert "You are in" in answer.text
        assert not answer.alert
        assert [seated.user_id for seated in flip.players] == [STARTER, _JOINER]
        assert [seated.display_name for seated in flip.players] == ["Sam", "Pat"]
        assert len(world.client.edits) == 1
        edit = world.client.edits[0]
        assert edit.message_id == flip.message_id
        assert "Pat" in edit.text
        assert len(edit.buttons) == 4
        await world.close()

    asyncio.run(scenario())


def test_leave_removes_the_presser_and_edits_the_lobby() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)

        answer = await _press(world, flip, Press.LEAVE, user_id=_JOINER, name="Pat")

        assert "left" in answer.text
        assert [seated.user_id for seated in flip.players] == [STARTER]
        assert len(world.client.edits) == 2
        assert "Pat" not in world.client.edits[-1].text
        await world.close()

    asyncio.run(scenario())


def test_leave_by_somebody_who_never_joined_changes_nothing() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)

        answer = await _press(world, flip, Press.LEAVE, user_id=_STRANGER)

        assert "not in this lobby" in answer.text
        assert world.client.edits == ()
        assert [seated.user_id for seated in flip.players] == [STARTER]
        await world.close()

    asyncio.run(scenario())


def test_joining_twice_gets_a_private_notice_and_no_second_edit() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        edits_before = len(world.client.edits)

        answer = await _press(world, flip, Press.JOIN, user_id=_JOINER, name="Pat")

        assert "already in" in answer.text
        assert not answer.alert
        assert len(world.client.edits) == edits_before
        assert [seated.user_id for seated in flip.players] == [STARTER, _JOINER]
        await world.close()

    asyncio.run(scenario())


def test_start_by_the_starter_deals() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)

        answer = await _press(world, flip, Press.START)

        assert "Dealing now" in answer.text
        assert flip.phase is not FlipPhase.LOBBY
        assert any("Deck commitment" in call.text for call in world.client.sends)
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("press", [Press.START, Press.CANCEL])
def test_start_and_cancel_from_anyone_but_the_starter_change_nothing(press: Press) -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        calls_before = list(world.client.calls)

        answer = await _press(world, flip, press, user_id=_JOINER, name="Pat")

        assert "Only whoever opened the flip" in answer.text
        assert not answer.alert
        assert flip.phase is FlipPhase.LOBBY
        assert world.registry.flip_for_group(GROUP) is flip
        assert world.client.calls == [*calls_before, answer]
        assert flip.token in world.store.active
        await world.close()

    asyncio.run(scenario())


def test_start_with_one_player_says_more_are_needed() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)

        answer = await _press(world, flip, Press.START)

        assert "at least 2 players" in answer.text
        assert flip.phase is FlipPhase.LOBBY
        assert world.client.edits == ()
        assert len(world.client.sends) == 1
        await world.close()

    asyncio.run(scenario())


def test_cancel_by_the_starter_closes_the_lobby_and_forgets_the_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)

        answer = await _press(world, flip, Press.CANCEL)

        assert "cancelled" in answer.text
        assert world.registry.flip_for_group(GROUP) is None
        assert flip.token not in world.store.active
        assert "cancelled" in world.client.edits[-1].text
        assert world.client.edits[-1].buttons == ()
        assert world.store.recorded == []
        await world.close()

    asyncio.run(scenario())


def test_peek_from_somebody_who_is_not_playing_shows_no_cards() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        await _press(world, flip, Press.START)

        answer = await _press(world, flip, Press.PEEK, user_id=_STRANGER, name="Lou")

        assert "not in this flip" in answer.text
        assert "Seat" not in answer.text
        assert not answer.alert
        await world.close()

    asyncio.run(scenario())


def test_every_press_is_answered_once_with_its_own_id() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)
        expected: list[str] = []
        steps = [
            (Press.JOIN, _JOINER),
            (Press.JOIN, _JOINER),
            (Press.START, _JOINER),
            (Press.LEAVE, _STRANGER),
            (Press.CANCEL, STARTER),
        ]
        for press, user_id in steps:
            press_id = world.press_id()
            expected.append(press_id)
            await world.feed(
                callback_query=button_press(
                    pack(press, flip.token), user_id=user_id, press_id=press_id
                )
            )

        assert [call.press_id for call in world.client.answers] == expected
        await world.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------- peek


def test_peek_is_a_private_alert_holding_only_that_players_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        use_seed(monkeypatch, _SEED)
        flip = await _two_players(world)
        await _press(world, flip, Press.START)
        deal = deal_cards(seed=_SEED, player_ids=[STARTER, _JOINER], x=SETTINGS.x, y=SETTINGS.y)
        mine, theirs = deal.hands

        early = await _press(world, flip, Press.PEEK)
        await world.clock.advance(timing.BOARD_GAP_SECONDS)
        late = await _press(world, flip, Press.PEEK)

        for answer in (early, late):
            assert answer.alert
            assert "Seat 1" in answer.text
            assert {str(card) for card in mine.hole} <= _tokens(answer.text)
            assert not {str(card) for card in theirs.cards} & _tokens(answer.text)
        assert {str(card) for card in mine.extra} <= _tokens(late.text)
        assert not {str(card) for card in mine.extra} & _tokens(early.text)
        await world.close()

    asyncio.run(scenario())


class _HoldsTheCommitment(FakeTelegram):
    """A Telegram whose second send -- the commitment -- never comes back.

    The lobby goes out as usual, so the round can be started through the real
    router; what hangs is the message that tells the group which deck this is.
    """

    def __init__(self) -> None:
        super().__init__()
        self.commitment_requested = asyncio.Event()

    async def send_message(self, **kwargs: object) -> Message:
        if self.sends:
            self.commitment_requested.set()
            await asyncio.Event().wait()
        return await super().send_message(**kwargs)  # type: ignore[arg-type]


def test_a_peek_pressed_before_the_commitment_is_out_shows_no_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hand goes out before the group has been told which deck it came from.

    The cards exist from Start, and the round's token is already live, so a
    seated player who presses Peek in the gap -- or sends the callback data by
    hand -- used to be shown their hole cards before anybody had seen the
    commitment they could be checked against. The press is answered, as every
    press is, and the answer holds nothing.
    """

    async def scenario() -> None:
        client = _HoldsTheCommitment()
        world = bot_harness(client=client)
        use_seed(monkeypatch, _SEED)
        flip = await _two_players(world)
        deal = deal_cards(seed=_SEED, player_ids=[STARTER, _JOINER], x=SETTINGS.x, y=SETTINGS.y)

        start = asyncio.create_task(
            world.feed(
                callback_query=button_press(
                    pack(Press.START, flip.token), user_id=STARTER, press_id=world.press_id()
                )
            )
        )
        await asyncio.wait_for(client.commitment_requested.wait(), timeout=1)

        answer = await _press(world, flip, Press.PEEK)

        assert flip.phase is FlipPhase.DEALING
        assert len(client.sends) == 1
        assert not answer.alert
        assert "Seat" not in answer.text
        for hand in deal.hands:
            assert not {str(card) for card in hand.cards} & _tokens(answer.text)
        assert answer.text == render.render_notice(Notice.EXPIRED)

        start.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start
        flip.stop()
        await world.close()

    asyncio.run(scenario())


def test_the_other_player_peeks_at_their_own_cards_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        use_seed(monkeypatch, _SEED)
        flip = await _two_players(world)
        await _press(world, flip, Press.START)
        deal = deal_cards(seed=_SEED, player_ids=[STARTER, _JOINER], x=SETTINGS.x, y=SETTINGS.y)
        mine, theirs = deal.hands

        answer = await _press(world, flip, Press.PEEK, user_id=_JOINER, name="Pat")

        assert "Seat 2" in answer.text
        assert {str(card) for card in theirs.hole} <= _tokens(answer.text)
        assert not {str(card) for card in mine.cards} & _tokens(answer.text)
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------- old and odd presses


def test_a_press_with_a_token_the_registry_does_not_know_is_answered_as_expired() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        calls_before = list(world.client.calls)

        press_id = world.press_id()
        await world.feed(
            callback_query=button_press(pack(Press.JOIN, "nosuchtoken"), press_id=press_id)
        )

        (answer,) = world.client.answers[-1:]
        assert answer.press_id == press_id
        assert "expired" in answer.text
        assert not answer.alert
        assert world.client.calls == [*calls_before, answer]
        assert [seated.user_id for seated in flip.players] == [STARTER, _JOINER]
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data",
    ["", "garbage", "j", "j:", ":token", "z:token", "J:token", "  "],
)
def test_callback_data_this_bot_never_wrote_is_answered_as_expired(data: str) -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)
        calls_before = list(world.client.calls)

        press_id = world.press_id()
        await world.feed(callback_query=button_press(data, press_id=press_id))

        assert [call.press_id for call in world.client.answers] == [press_id]
        assert "expired" in world.client.answers[0].text
        assert world.client.calls == [*calls_before, world.client.answers[0]]
        assert world.registry.flip_for_group(GROUP) is flip
        await world.close()

    asyncio.run(scenario())


def test_a_press_with_no_callback_data_at_all_is_answered_as_expired() -> None:
    async def scenario() -> None:
        world = bot_harness()
        press = button_press("x", press_id="nothing").model_copy(update={"data": None})

        await world.feed(callback_query=press)

        assert [call.press_id for call in world.client.answers] == ["nothing"]
        assert "expired" in world.client.answers[0].text
        await world.close()

    asyncio.run(scenario())


def test_a_press_on_the_lobby_of_a_cancelled_flip_is_answered_as_expired() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        await _press(world, flip, Press.CANCEL)
        calls_before = list(world.client.calls)

        for press in (Press.JOIN, Press.START, Press.CANCEL, Press.PEEK):
            answer = await _press(world, flip, press, user_id=_STRANGER, name="Lou")
            assert "expired" in answer.text
            calls_before.append(answer)

        assert world.client.calls == calls_before
        assert world.registry.flips == ()
        await world.close()

    asyncio.run(scenario())


def test_a_press_on_the_table_of_a_finished_flip_is_answered_as_expired() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        await _press(world, flip, Press.START)
        await _play_out(world, flip)
        assert world.registry.flip_for_token(flip.token) is None
        calls_before = list(world.client.calls)

        answer = await _press(world, flip, Press.PEEK, user_id=_JOINER, name="Pat")

        assert "expired" in answer.text
        assert not answer.alert
        assert world.client.calls == [*calls_before, answer]
        await world.close()

    asyncio.run(scenario())


def test_a_press_on_a_lobby_that_timed_out_is_answered_as_expired() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)
        await world.clock.advance(timing.LOBBY_IDLE_SECONDS)
        assert flip.closed

        answer = await _press(world, flip, Press.JOIN, user_id=_JOINER, name="Pat")

        assert "expired" in answer.text
        assert [seated.user_id for seated in flip.players] == [STARTER]
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------- callback data


def test_callback_data_on_a_real_lobby_fits_the_limit_and_carries_the_token() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        await _press(world, flip, Press.START)

        markups = [call for call in world.client.calls if call.markup is not None]
        assert markups
        for call in markups:
            for data in call.callback_data:
                assert len(data.encode()) <= keyboards.CALLBACK_DATA_LIMIT
                assert data.endswith(f"{keyboards.SEPARATOR}{flip.token}")
        lobby = world.client.sends[0]
        assert [keyboards.parse(data) for data in lobby.callback_data] == [
            keyboards.PressData(press=press, token=flip.token)
            for press in (Press.JOIN, Press.LEAVE, Press.START, Press.CANCEL)
        ]
        await world.close()

    asyncio.run(scenario())


def test_pack_refuses_a_token_that_would_push_the_data_over_the_limit() -> None:
    longest = "t" * (keyboards.CALLBACK_DATA_LIMIT - len("j:"))

    assert len(pack(Press.JOIN, longest).encode()) == keyboards.CALLBACK_DATA_LIMIT
    with pytest.raises(ValueError, match="bytes"):
        pack(Press.JOIN, longest + "t")


def test_pack_counts_bytes_not_characters() -> None:
    with pytest.raises(ValueError, match="bytes"):
        pack(Press.JOIN, "é" * keyboards.CALLBACK_DATA_LIMIT)


# ----------------------------------------------------------------- migration


def test_a_group_becoming_a_supergroup_moves_its_rows_to_the_new_id() -> None:
    async def scenario() -> None:
        world = bot_harness()

        await world.feed(message=migration_message(new_group_id=_NEW_GROUP))

        assert world.store.migrations == [(GROUP, _NEW_GROUP)]
        assert world.client.calls == []
        await world.close()

    asyncio.run(scenario())


def test_migration_does_not_open_or_disturb_a_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world)

        await world.feed(message=migration_message(new_group_id=_NEW_GROUP))

        assert world.store.migrations == [(GROUP, _NEW_GROUP)]
        assert world.store.active[flip.token].group_id == _NEW_GROUP
        assert flip.group_id == _NEW_GROUP
        assert len(world.client.sends) == 1
        await world.close()

    asyncio.run(scenario())


def test_a_flip_open_when_its_group_migrates_plays_out_in_the_new_chat() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        await _press(world, flip, Press.START)
        sent_before = len(world.client.sends)
        edited_before = len(world.client.edits)

        await world.feed(message=migration_message(new_group_id=_NEW_GROUP))

        assert flip.group_id == _NEW_GROUP
        assert world.registry.flip_for_group(_NEW_GROUP) is flip
        assert world.registry.flip_for_group(GROUP) is None
        assert world.store.active[flip.token].group_id == _NEW_GROUP

        await world.feed(message=group_message("/flip 50 5 2 3", chat_id=_NEW_GROUP))
        assert world.client.sends[-1].chat_id == _NEW_GROUP
        assert world.client.sends[-1].text == render.render_already_open(SETTINGS)
        assert world.registry.flips == (flip,)

        await _play_out(world, flip)

        later = world.client.sends[sent_before:] + world.client.edits[edited_before:]
        assert later
        assert {call.chat_id for call in later} == {_NEW_GROUP}
        assert flip.phase is FlipPhase.FINISHED
        assert world.store.active == {}
        assert world.store.recorded[-1].group_id == _NEW_GROUP
        assert len(await world.store.leaderboard(_NEW_GROUP)) > 0
        assert await world.store.leaderboard(GROUP) == ()
        await world.close()

    asyncio.run(scenario())


def test_a_table_edit_refused_with_the_new_group_id_keeps_the_flip_playing() -> None:
    async def scenario() -> None:
        world = bot_harness()
        flip = await _two_players(world)
        edited_before = len(world.client.edits)
        world.client.failures = [
            TelegramMigrateToChat(
                method=EditMessageText(chat_id=GROUP, message_id=flip.message_id, text="x"),
                message="upgraded",
                migrate_to_chat_id=_NEW_GROUP,
            )
        ]

        await _press(world, flip, Press.START)

        assert flip.phase is not FlipPhase.VOIDED
        assert flip.group_id == _NEW_GROUP
        assert world.store.active[flip.token].group_id == _NEW_GROUP
        assert world.registry.flip_for_group(_NEW_GROUP) is flip
        assert world.registry.flip_for_group(GROUP) is None
        assert (GROUP, _NEW_GROUP) in world.store.migrations
        assert world.gateway.chat_id_for(GROUP) == _NEW_GROUP
        assert world.client.edits[edited_before].chat_id == _NEW_GROUP

        # The service message that follows finds nothing left to move.
        calls_before = len(world.client.calls)
        await world.feed(message=migration_message(new_group_id=_NEW_GROUP))

        assert set(world.store.migrations) == {(GROUP, _NEW_GROUP)}
        assert flip.group_id == _NEW_GROUP
        assert world.store.active[flip.token].group_id == _NEW_GROUP
        assert world.registry.flips == (flip,)
        assert len(world.client.calls) == calls_before

        await _play_out(world, flip)
        assert flip.phase is FlipPhase.FINISHED
        assert world.store.recorded[-1].group_id == _NEW_GROUP
        await world.close()

    asyncio.run(scenario())


def test_an_open_flips_row_follows_it_even_when_the_rest_of_the_rows_cannot_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The flip's row moves with the flip, not with the group's history.

    So a rewrite that fails costs the group its history, and the void notice the
    next start posts for this flip still goes to the chat it was playing in.
    """

    async def scenario() -> None:
        world = bot_harness()
        flip = await _open(world, topic_id=TOPIC)
        world.store.migrate_error = sqlite3.OperationalError("disk I/O error")

        with caplog.at_level(logging.ERROR):
            await world.feed(message=migration_message(new_group_id=_NEW_GROUP))

        assert world.store.migrations == []
        assert "disk I/O error" in caplog.text
        assert flip.group_id == _NEW_GROUP
        row = world.store.active[flip.token]
        assert row.group_id == _NEW_GROUP
        assert row.topic_id == TOPIC
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [StorageError("the database is locked"), sqlite3.OperationalError("disk I/O error")],
    ids=["storage-error", "sqlite-error"],
)
def test_a_storage_failure_during_migration_is_logged_and_not_raised(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.migrate_error = error

        with caplog.at_level(logging.ERROR):
            await world.feed(message=migration_message(new_group_id=_NEW_GROUP))

        assert world.store.migrations == []
        assert str(error) in caplog.text
        assert str(GROUP) in caplog.text
        assert str(_NEW_GROUP) in caplog.text
        assert world.client.calls == []
        await world.close()

    asyncio.run(scenario())

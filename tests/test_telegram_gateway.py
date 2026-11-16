"""The gateway against a Telegram that waits, refuses and forgets, never a real one.

Everything here runs on :class:`~tests.telegram_helpers.FakeTelegram` and a clock
that records its sleeps rather than taking them. The engine's own rules for the
gateway -- wait and retry the same call, give up after three, never let a lost
pop-up cost a flip -- are each pinned to a number: the seconds slept, the attempts
made, the message id that comes back.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable, Coroutine

import pytest
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramMigrateToChat, TelegramNotFound
from aiogram.methods import EditMessageText

from esgbot.engine import (
    FlipView,
    LobbyStatus,
    LobbyView,
    MessageTarget,
    Notice,
    PeekAnswer,
    PermanentFailureError,
    SeatView,
    TableStage,
    TableView,
)
from esgbot.game.cards import Card
from esgbot.game.rules import FlipRequest
from esgbot.telegram.gateway import (
    FLOOD_RETRIES,
    MIGRATIONS_FOLLOWED,
    SEND_ATTEMPTS,
    TelegramGateway,
)
from esgbot.telegram.render import ALERT_LIMIT
from tests.engine_helpers import GROUP, SETTINGS, player, players
from tests.telegram_helpers import (
    TOPIC,
    FakeTelegram,
    RecordingClock,
    bad_request,
    deleted_message,
    flood_wait,
    kicked_out,
    not_modified,
    telegram_gateway,
)

_TARGET = MessageTarget(group_id=GROUP)
_IN_TOPIC = MessageTarget(group_id=GROUP, topic_id=TOPIC)
_FIRST_ID = 500
_NEW_CHAT = -1009999


def _lobby(status: LobbyStatus = LobbyStatus.OPEN) -> LobbyView:
    return LobbyView(
        token="tok",
        request=FlipRequest.from_settings(SETTINGS),
        status=status,
        starter=player(11),
        players=players(2),
    )


def _table(*, voided: bool = False) -> TableView:
    return TableView(
        token="tok",
        settings=SETTINGS,
        commitment="ab" * 32,
        stage=TableStage.HOLE_CARDS,
        button_seat=0,
        seats=tuple(SeatView(seat=n, player=p, card_count=2) for n, p in enumerate(players(2))),
        voided=voided,
    )


def _peek(count: int = 4) -> PeekAnswer:
    cards = tuple(Card.from_index(index) for index in range(count))
    return PeekAnswer(seat=0, hole=cards[:2], extra=cards[2:])


def _not_found() -> TelegramNotFound:
    return TelegramNotFound(method=EditMessageText(text="x"), message="Not Found")


def _moved(old: int, new: int) -> TelegramMigrateToChat:
    """Telegram's answer to a call made to a group that has become a supergroup."""
    return TelegramMigrateToChat(
        method=EditMessageText(chat_id=old, message_id=1, text="x"),
        message="Bad Request: group chat was upgraded to a supergroup chat",
        migrate_to_chat_id=new,
    )


# ------------------------------------------------------------------ happy path


def test_post_sends_the_rendered_view_as_html_with_the_lobby_buttons() -> None:
    client, _, gateway = telegram_gateway()

    message_id = asyncio.run(gateway.post(_TARGET, _lobby()))

    sent = client.last("send")
    assert message_id == sent.message_id == _FIRST_ID
    assert sent.chat_id == GROUP
    assert sent.parse_mode == ParseMode.HTML
    assert sent.text
    assert len(sent.buttons) == 4
    assert all(data.endswith(":tok") for data in sent.callback_data)


@pytest.mark.parametrize(
    ("view", "buttons"),
    [(_table(), 1), (_table(voided=True), 0), (_lobby(LobbyStatus.CANCELLED), 0)],
    ids=["live table", "voided table", "cancelled lobby"],
)
def test_the_buttons_follow_the_view(view: FlipView, buttons: int) -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.post(_TARGET, view))

    sent = client.last("send")
    assert len(sent.buttons) == buttons
    if buttons:
        assert "Peek" in sent.buttons[0]


def test_edit_edits_the_given_message_and_returns_its_id() -> None:
    client, _, gateway = telegram_gateway()
    message_id = asyncio.run(gateway.post(_TARGET, _lobby()))

    edited = asyncio.run(gateway.edit(_TARGET, message_id, _table()))

    assert edited == message_id
    call = client.last("edit")
    assert call.message_id == message_id
    assert call.chat_id == GROUP
    assert call.parse_mode == ParseMode.HTML
    assert len(call.buttons) == 1
    assert len(client.sends) == 1


def test_a_final_edit_takes_the_buttons_off() -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _lobby(LobbyStatus.TIMED_OUT)))

    assert client.last("edit").buttons == ()


# --------------------------------------------------------------- topic routing


def test_post_goes_to_the_topic_the_target_names() -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.post(_IN_TOPIC, _lobby()))
    asyncio.run(gateway.post(_TARGET, _lobby()))

    topical, plain = client.sends
    assert topical.thread_id == TOPIC
    assert plain.thread_id is None


def test_send_and_say_go_to_the_topic_too() -> None:
    client, _, gateway = telegram_gateway()

    async def scenario() -> None:
        await gateway.send(_IN_TOPIC, "one")
        await gateway.say(_IN_TOPIC, "two")
        await gateway.send(_TARGET, "three")
        await gateway.say(_TARGET, "four")

    asyncio.run(scenario())

    assert [(call.text, call.thread_id) for call in client.sends] == [
        ("one", TOPIC),
        ("two", TOPIC),
        ("three", None),
        ("four", None),
    ]
    assert all(call.parse_mode == ParseMode.HTML for call in client.sends)


def test_a_fresh_post_after_a_deleted_table_stays_in_the_topic() -> None:
    client, _, gateway = telegram_gateway()
    message_id = asyncio.run(gateway.post(_IN_TOPIC, _table()))
    client.deleted.add(message_id)

    asyncio.run(gateway.edit(_IN_TOPIC, message_id, _table()))

    assert [call.thread_id for call in client.sends] == [TOPIC, TOPIC]


# --------------------------------------------------------------- flood control


def test_a_flood_wait_on_a_send_sleeps_for_exactly_that_long_and_retries_the_call() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [flood_wait(7)]

    message_id = asyncio.run(gateway.post(_TARGET, _lobby()))

    assert clock.sleeps == [7]
    assert client.attempts == 2
    assert len(client.sends) == 1
    assert message_id == client.sends[0].message_id


def test_a_flood_wait_on_an_edit_sleeps_for_exactly_that_long_and_retries_the_call() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [flood_wait(4)]

    message_id = asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _table()))

    assert message_id == _FIRST_ID
    assert clock.sleeps == [4]
    assert client.attempts == 2
    assert len(client.edits) == 1
    assert client.sends == ()


def test_each_flood_wait_is_honoured_at_its_own_length() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [flood_wait(2), flood_wait(30), flood_wait(1)]

    asyncio.run(gateway.post(_TARGET, _lobby()))

    assert clock.sleeps == [2, 30, 1]
    assert client.attempts == 4
    assert len(client.sends) == 1


def test_a_flood_wait_does_not_use_up_an_attempt() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [flood_wait(2), kicked_out(), kicked_out(), kicked_out()]

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == 1 + SEND_ATTEMPTS
    assert clock.sleeps == [2]
    assert client.sends == ()


def test_a_flood_wait_between_failures_does_not_reset_or_spend_the_count() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out(), flood_wait(1), kicked_out(), None]

    message_id = asyncio.run(gateway.post(_TARGET, _lobby()))

    assert message_id == _FIRST_ID
    assert client.attempts == 4


def test_waits_are_given_up_on_after_flood_retries_in_a_row() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [flood_wait(1)] * (FLOOD_RETRIES + 1)

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == FLOOD_RETRIES + 1
    assert clock.sleeps == [1] * FLOOD_RETRIES
    assert client.sends == ()


def test_flood_retries_waits_are_still_survivable() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [flood_wait(1)] * FLOOD_RETRIES

    asyncio.run(gateway.post(_TARGET, _lobby()))

    assert clock.sleeps == [1] * FLOOD_RETRIES
    assert len(client.sends) == 1


def test_a_shorter_flood_budget_is_respected() -> None:
    client, clock, gateway = telegram_gateway(flood_retries=2)
    client.failures = [flood_wait(1)] * 3

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == 3
    assert clock.sleeps == [1, 1]


# ------------------------------------------------------------- not modified


def test_an_edit_telegram_calls_unchanged_is_a_success() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [not_modified()]

    message_id = asyncio.run(gateway.edit(_TARGET, _FIRST_ID + 3, _table()))

    assert message_id == _FIRST_ID + 3
    assert client.attempts == 1
    assert clock.sleeps == []
    assert client.sends == ()


def test_not_modified_is_recognised_whatever_its_case() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [bad_request("Bad Request: Message Is Not Modified: same text")]

    assert asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _table())) == _FIRST_ID
    assert client.attempts == 1


# ---------------------------------------------------------- deleted messages


def _gone_wordings() -> list[Callable[[], Exception]]:
    return [
        deleted_message,
        lambda: bad_request("Bad Request: message to be edited not found"),
        lambda: bad_request("Bad Request: MESSAGE_ID_INVALID"),
        lambda: bad_request("Bad Request: message can't be edited"),
        _not_found,
    ]


def test_editing_a_deleted_table_posts_a_fresh_one_and_returns_the_new_id() -> None:
    client, _, gateway = telegram_gateway()
    old_id = asyncio.run(gateway.post(_TARGET, _lobby()))
    client.deleted.add(old_id)

    new_id = asyncio.run(gateway.edit(_TARGET, old_id, _table()))

    assert new_id != old_id
    assert new_id == old_id + 1
    assert [call.message_id for call in client.sends] == [old_id, new_id]
    assert client.edits == ()
    fresh = client.sends[-1]
    assert fresh.parse_mode == ParseMode.HTML
    assert len(fresh.buttons) == 1


@pytest.mark.parametrize("make", _gone_wordings(), ids=range(5))
def test_every_wording_of_message_gone_posts_a_fresh_message(
    make: Callable[[], Exception],
) -> None:
    client, _, gateway = telegram_gateway()
    old_id = asyncio.run(gateway.post(_TARGET, _lobby()))
    client.failures = [make()]

    new_id = asyncio.run(gateway.edit(_TARGET, old_id, _table()))

    assert new_id == old_id + 1
    assert len(client.sends) == 2


def test_the_engine_can_keep_following_the_new_message() -> None:
    client, _, gateway = telegram_gateway()
    old_id = asyncio.run(gateway.post(_TARGET, _lobby()))
    client.deleted.add(old_id)

    async def scenario() -> int:
        followed = await gateway.edit(_TARGET, old_id, _table())
        return await gateway.edit(_TARGET, followed, _table(voided=True))

    final_id = asyncio.run(scenario())

    assert final_id == old_id + 1
    assert client.last("edit").message_id == old_id + 1


def test_a_group_that_refuses_the_replacement_post_is_a_permanent_failure() -> None:
    client, _, gateway = telegram_gateway()
    client.deleted.add(_FIRST_ID)
    client.failures = [None, kicked_out(), kicked_out(), kicked_out()]

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _table()))

    assert client.attempts == 1 + SEND_ATTEMPTS
    assert client.sends == ()


# ----------------------------------------------------- three attempts, then out


def test_post_gives_up_after_exactly_three_attempts() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 5

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == SEND_ATTEMPTS == 3
    assert client.sends == ()


def test_edit_gives_up_after_exactly_three_attempts() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 5

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _table()))

    assert client.attempts == 3
    assert client.edits == ()


def test_remove_buttons_gives_up_after_exactly_three_attempts() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 5

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.remove_buttons(_TARGET, _FIRST_ID))

    assert client.attempts == 3


def test_send_gives_up_after_exactly_three_attempts() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 5

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.send(_TARGET, "hello"))

    assert client.attempts == 3


@pytest.mark.parametrize("operation", ["post", "edit", "remove_buttons"])
def test_two_failures_then_a_success_is_a_success(operation: str) -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out(), kicked_out()]

    async def scenario() -> int | None:
        if operation == "post":
            return await gateway.post(_TARGET, _lobby())
        if operation == "edit":
            return await gateway.edit(_TARGET, _FIRST_ID, _table())
        await gateway.remove_buttons(_TARGET, _FIRST_ID)
        return None

    result = asyncio.run(scenario())

    assert client.attempts == 3
    if operation == "remove_buttons":
        assert result is None
        assert client.last("markup").buttons == ()
    else:
        assert result is not None


def test_the_failure_is_chained_to_what_telegram_said() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 3

    with pytest.raises(PermanentFailureError) as caught:
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert isinstance(caught.value.__cause__, TelegramForbiddenError)


def test_a_pause_separates_failed_attempts_but_does_not_follow_the_last() -> None:
    client, clock, gateway = telegram_gateway(retry_delay=0.5)
    client.failures = [kicked_out()] * 3

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert clock.sleeps == [0.5, 0.5]


def test_a_longer_attempt_budget_is_honoured() -> None:
    client, _, gateway = telegram_gateway(attempts=5)
    client.failures = [kicked_out()] * 5

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == 5


def test_remove_buttons_treats_an_unchanged_or_missing_message_as_done() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [not_modified(), deleted_message(), _not_found()]

    async def scenario() -> None:
        for _ in range(3):
            await gateway.remove_buttons(_TARGET, _FIRST_ID)

    asyncio.run(scenario())

    assert client.attempts == 3


# --------------------------------------------------------------- answer_press


def test_a_notice_is_answered_as_a_toast() -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.answer_press("press-1", Notice.JOINED))

    answer = client.last("answer")
    assert answer.press_id == "press-1"
    assert answer.alert is False
    assert answer.text


def test_a_peek_is_answered_as_an_alert_so_the_cards_stay_up() -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.answer_press("press-2", _peek()))

    answer = client.last("answer")
    assert answer.press_id == "press-2"
    assert answer.alert is True
    assert str(Card.from_index(0)) in answer.text


def test_the_largest_peek_fits_the_alert_limit() -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.answer_press("press-3", _peek(count=21)))

    assert 0 < len(client.last("answer").text) <= ALERT_LIMIT


@pytest.mark.parametrize("answer", [Notice.JOINED, _peek()], ids=["notice", "peek"])
def test_a_failed_answer_is_dropped_after_a_single_attempt(answer: Notice | PeekAnswer) -> None:
    client, clock, gateway = telegram_gateway()
    client.answer_failure = bad_request("Bad Request: query is too old")

    asyncio.run(gateway.answer_press("press-4", answer))

    assert client.answers == ()
    assert client.attempts == 0
    assert clock.sleeps == []


def test_a_failed_answer_is_not_retried() -> None:
    attempts = 0

    class Refusing(FakeTelegram):
        async def answer_callback_query(
            self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False
        ) -> bool:
            nonlocal attempts
            attempts += 1
            raise kicked_out()

    clock = RecordingClock()
    gateway = TelegramGateway(Refusing(), clock=clock, retry_delay=0.0)

    asyncio.run(gateway.answer_press("press-5", Notice.JOINED))

    assert attempts == 1
    assert clock.sleeps == []


def test_a_flood_wait_on_an_answer_is_not_waited_out() -> None:
    client, clock, gateway = telegram_gateway()
    client.answer_failure = flood_wait(5)

    asyncio.run(gateway.answer_press("press-6", Notice.JOINED))

    assert clock.sleeps == []
    assert client.answers == ()


# ------------------------------------------------------------------ the logs


@pytest.mark.parametrize("fail", [False, True], ids=["answered", "dropped"])
def test_no_card_from_a_hand_reaches_the_log(caplog: pytest.LogCaptureFixture, fail: bool) -> None:
    client, _, gateway = telegram_gateway()
    if fail:
        client.answer_failure = bad_request("Bad Request: query is too old")
    answer = _peek()
    caplog.set_level(logging.DEBUG)

    asyncio.run(gateway.answer_press("press-7", answer))

    for card in answer.cards:
        assert str(card) not in caplog.text
    if fail:
        assert "press-7" in caplog.text


# ------------------------------------------------------------------------ say


def test_say_swallows_a_group_the_bot_cannot_post_in() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 3

    asyncio.run(gateway.say(_TARGET, "usage: /flip ..."))

    assert client.attempts == 3
    assert client.sends == ()


def test_send_on_the_same_group_still_raises() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [kicked_out()] * 3

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.send(_TARGET, "usage: /flip ..."))

    assert client.sends == ()


def test_say_still_delivers_when_the_group_is_fine() -> None:
    client, _, gateway = telegram_gateway()

    asyncio.run(gateway.say(_TARGET, "hello"))

    assert client.last("send").text == "hello"


# ---------------------------------------------------------------- construction


@pytest.mark.parametrize("attempts", [0, -1])
def test_a_call_needs_at_least_one_attempt(attempts: int) -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        TelegramGateway(FakeTelegram(), attempts=attempts)


@pytest.mark.parametrize("flood_retries", [0, -1])
def test_flood_retries_must_be_at_least_one(flood_retries: int) -> None:
    with pytest.raises(ValueError, match="flood_retries"):
        TelegramGateway(FakeTelegram(), flood_retries=flood_retries)


def test_an_unknown_bad_request_counts_as_a_failure_not_a_deletion() -> None:
    client, _, gateway = telegram_gateway()
    client.failures = [bad_request("Bad Request: can't parse entities")] * 3

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _table()))

    assert client.attempts == 3
    assert client.sends == ()


# ----------------------------------------------------- a group that has moved


_Operation = Callable[[TelegramGateway], Coroutine[object, object, None]]


async def _do_post(gateway: TelegramGateway) -> None:
    await gateway.post(_TARGET, _lobby())


async def _do_edit(gateway: TelegramGateway) -> None:
    await gateway.edit(_TARGET, _FIRST_ID, _table())


async def _do_remove_buttons(gateway: TelegramGateway) -> None:
    await gateway.remove_buttons(_TARGET, _FIRST_ID)


_OPERATIONS = pytest.mark.parametrize(
    ("operation", "kind"),
    [(_do_post, "send"), (_do_edit, "edit"), (_do_remove_buttons, "markup")],
    ids=["post", "edit", "remove-buttons"],
)


@_OPERATIONS
def test_a_call_refused_with_the_new_group_id_succeeds_at_the_new_id(
    operation: _Operation, kind: str
) -> None:
    client, clock, gateway = telegram_gateway(retry_delay=1.0)
    client.failures = [_moved(GROUP, _NEW_CHAT)]

    asyncio.run(operation(gateway))

    assert [call.chat_id for call in client.of(kind)] == [_NEW_CHAT]
    assert client.attempts == 2
    assert clock.sleeps == []


@_OPERATIONS
def test_following_a_move_spends_neither_an_attempt_nor_a_flood_retry(
    operation: _Operation, kind: str
) -> None:
    client, clock, gateway = telegram_gateway(retry_delay=1.0, attempts=3, flood_retries=1)
    client.failures = [_moved(GROUP, _NEW_CHAT), flood_wait(3), kicked_out(), kicked_out(), None]

    asyncio.run(operation(gateway))

    assert [call.chat_id for call in client.of(kind)] == [_NEW_CHAT]
    assert client.attempts == 5
    assert clock.sleeps == [3, 1, 1]


def test_the_attempt_budget_is_whole_on_a_later_call_after_a_move() -> None:
    client, _, gateway = telegram_gateway(attempts=3)
    client.failures = [_moved(GROUP, _NEW_CHAT)]
    asyncio.run(gateway.post(_TARGET, _lobby()))
    client.failures = [kicked_out(), kicked_out(), None]

    asyncio.run(gateway.post(_TARGET, _lobby()))

    assert [call.chat_id for call in client.sends] == [_NEW_CHAT, _NEW_CHAT]


def test_every_later_call_goes_straight_to_the_new_id() -> None:
    client, _, gateway = telegram_gateway()
    assert gateway.chat_id_for(GROUP) == GROUP
    client.failures = [_moved(GROUP, _NEW_CHAT)]
    asyncio.run(gateway.post(_TARGET, _lobby()))
    attempts_so_far = client.attempts

    assert gateway.chat_id_for(GROUP) == _NEW_CHAT
    asyncio.run(gateway.post(_IN_TOPIC, _lobby()))
    asyncio.run(gateway.edit(_TARGET, _FIRST_ID, _table()))
    asyncio.run(gateway.remove_buttons(_TARGET, _FIRST_ID))

    assert client.attempts == attempts_so_far + 3
    assert {call.chat_id for call in client.calls} == {_NEW_CHAT}
    assert client.sends[-1].thread_id == TOPIC


def test_the_migration_handler_is_told_exactly_once_with_the_old_and_new_ids() -> None:
    told: list[tuple[int, int]] = []

    async def on_migrated(old: int, new: int) -> None:
        told.append((old, new))

    client, _, gateway = telegram_gateway(on_migrated=on_migrated)
    client.failures = [_moved(GROUP, _NEW_CHAT)]

    asyncio.run(gateway.post(_TARGET, _lobby()))
    asyncio.run(gateway.post(_TARGET, _lobby()))

    assert told == [(GROUP, _NEW_CHAT)]


def test_a_migration_handler_that_raises_does_not_cost_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def on_migrated(old: int, new: int) -> None:
        raise RuntimeError("could not move the rows")

    client, _, gateway = telegram_gateway(on_migrated=on_migrated)
    client.failures = [_moved(GROUP, _NEW_CHAT)]

    with caplog.at_level(logging.ERROR):
        message_id = asyncio.run(gateway.post(_TARGET, _lobby()))

    assert message_id == _FIRST_ID
    assert [call.chat_id for call in client.sends] == [_NEW_CHAT]
    assert "could not move the rows" in caplog.text
    assert gateway.chat_id_for(GROUP) == _NEW_CHAT


def test_a_group_moved_the_most_times_one_call_follows_is_still_reached() -> None:
    client, _, gateway = telegram_gateway()
    ids = [GROUP, -2001, -2002, -2003][: MIGRATIONS_FOLLOWED + 1]
    client.failures = [_moved(old, new) for old, new in itertools.pairwise(ids)]

    asyncio.run(gateway.post(_TARGET, _lobby()))

    assert [call.chat_id for call in client.sends] == [ids[-1]]
    assert gateway.chat_id_for(GROUP) == ids[-1]


def test_following_more_new_ids_than_the_limit_is_a_permanent_failure() -> None:
    client, clock, gateway = telegram_gateway()
    client.failures = [_moved(-2000 - n, -2001 - n) for n in range(MIGRATIONS_FOLLOWED + 10)]

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == MIGRATIONS_FOLLOWED + 1
    assert client.sends == ()
    assert clock.sleeps == []


# ----------------------------------------------------- whether a pop-up went out


@pytest.mark.parametrize("answer", [Notice.JOINED, _peek()], ids=["notice", "peek"])
def test_answer_press_says_true_for_a_pop_up_that_was_delivered(
    answer: Notice | PeekAnswer,
) -> None:
    client, _, gateway = telegram_gateway()

    assert asyncio.run(gateway.answer_press("press-7", answer)) is True

    assert len(client.answers) == 1


@pytest.mark.parametrize(
    "refusal",
    [bad_request("Bad Request: query is too old"), kicked_out(), flood_wait(5)],
    ids=["bad-request", "kicked-out", "flood-wait"],
)
@pytest.mark.parametrize("answer", [Notice.JOINED, _peek()], ids=["notice", "peek"])
def test_answer_press_says_false_for_a_pop_up_telegram_refused_and_never_raises(
    refusal: Exception, answer: Notice | PeekAnswer
) -> None:
    client, clock, gateway = telegram_gateway()
    client.answer_failure = refusal

    assert asyncio.run(gateway.answer_press("press-8", answer)) is False

    assert client.answers == ()
    assert clock.sleeps == []


# --------------------------------------------- a run of waits broken by a failure


def test_an_ordinary_failure_breaks_the_run_of_flood_waits() -> None:
    client, clock, gateway = telegram_gateway(retry_delay=0.5, attempts=3, flood_retries=2)
    client.failures = [
        flood_wait(1),
        flood_wait(2),
        kicked_out(),
        flood_wait(3),
        flood_wait(4),
        None,
    ]

    message_id = asyncio.run(gateway.post(_TARGET, _lobby()))

    assert message_id == _FIRST_ID
    assert client.attempts == 6
    assert clock.sleeps == [1, 2, 0.5, 3, 4]


def test_flood_retries_plus_one_consecutive_waits_still_give_up_after_a_failure() -> None:
    client, clock, gateway = telegram_gateway(retry_delay=0.5, attempts=3, flood_retries=2)
    client.failures = [kicked_out(), flood_wait(1), flood_wait(2), flood_wait(3), None]

    with pytest.raises(PermanentFailureError):
        asyncio.run(gateway.post(_TARGET, _lobby()))

    assert client.attempts == 4
    assert clock.sleeps == [0.5, 1, 2]
    assert client.sends == ()

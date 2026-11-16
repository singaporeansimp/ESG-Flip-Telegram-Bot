"""Presses from the wrong people, presses that race, and answering before anything else.

Covers who may Start and Cancel, a double Start, Join racing with Start and with
other Joins, a Peek answered while the gateway is stuck, the rule that every press
is answered before the post or edit it leads to, and presses on a flip that is
over. The races are forced by firing presses together with ``asyncio.gather``
(both orders of the pair where the order matters), and the slow gateway is
``FakeGateway.hold``; nothing waits on real time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine

import pytest

from esgbot.engine import (
    CommitmentView,
    Flip,
    FlipPhase,
    LobbyStatus,
    LobbyView,
    Notice,
    PeekAnswer,
    TableStage,
    TableView,
    timing,
)
from tests.engine_helpers import (
    Answered,
    Edited,
    FakeClock,
    Harness,
    HoldsAnAnswer,
    HoldsAView,
    Posted,
    harness,
    harness_over,
    player,
)


def _lobby_statuses(world: Harness) -> list[LobbyStatus]:
    """Every lobby the group was shown, as the status each one was showing."""
    return [view.status for view in world.gateway.views if isinstance(view, LobbyView)]


def _only_answers_since(world: Harness, count: int) -> list[Answered]:
    """The events after the first ``count``, which must all be button answers."""
    since = world.gateway.events[count:]
    answers = [event for event in since if isinstance(event, Answered)]
    assert answers == since
    return answers


def test_cancel_from_someone_who_is_not_the_starter_changes_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            before = len(world.gateway.events)
            press = world.press()

            assert await flip.cancel(12, press_id=press) is Notice.NOT_THE_STARTER

            (answer,) = _only_answers_since(world, before)
            assert (answer.press_id, answer.answer) == (press, Notice.NOT_THE_STARTER)
            assert flip.phase is FlipPhase.LOBBY
            assert [seated.user_id for seated in flip.players] == [11, 12, 13]
            assert world.registry.flip_for_group(world.group_id) is flip
            assert flip.token in world.store.active

    asyncio.run(scenario())


def test_start_from_someone_who_is_not_the_starter_starts_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            before = len(world.gateway.events)
            press = world.press()

            assert await flip.start(12, press_id=press) is Notice.NOT_THE_STARTER
            await world.clock.advance(timing.BOARD_GAP_SECONDS)

            (answer,) = _only_answers_since(world, before)
            assert (answer.press_id, answer.answer) == (press, Notice.NOT_THE_STARTER)
            assert flip.phase is FlipPhase.LOBBY
            assert world.gateway.views_of(CommitmentView) == ()
            assert world.store.active[flip.token].seed is None

    asyncio.run(scenario())


def test_start_with_one_player_needs_more_players() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            before = len(world.gateway.events)
            press = world.press()

            assert await flip.start(11, press_id=press) is Notice.NEED_MORE_PLAYERS

            (answer,) = _only_answers_since(world, before)
            assert (answer.press_id, answer.answer) == (press, Notice.NEED_MORE_PLAYERS)
            assert flip.phase is FlipPhase.LOBBY
            assert flip.player_count == 1

    asyncio.run(scenario())


def test_a_second_start_is_refused_and_the_round_is_dealt_once() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            views_after_first = len(world.gateway.views)
            before = len(world.gateway.events)
            press = world.press()

            assert await flip.start(11, press_id=press) is Notice.ALREADY_STARTED

            (answer,) = _only_answers_since(world, before)
            assert (answer.press_id, answer.answer) == (press, Notice.ALREADY_STARTED)
            assert len(world.gateway.views) == views_after_first

            await world.finish(flip)
            assert len(world.gateway.views_of(CommitmentView)) == 1
            assert len(world.store.recorded) == 1

    asyncio.run(scenario())


def test_two_starts_at_once_start_one_round() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)

            first, second = await asyncio.gather(
                flip.start(11, press_id=world.press()), flip.start(11, press_id=world.press())
            )
            assert {first, second} == {Notice.STARTED, Notice.ALREADY_STARTED}
            assert {answer.answer for answer in world.gateway.answers[-2:]} == {
                Notice.ALREADY_STARTED,
                Notice.STARTED,
            }

            await world.finish(flip)
            assert len(world.gateway.views_of(CommitmentView)) == 1
            assert len(world.store.recorded) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("start_first", [False, True])
def test_a_join_racing_with_start_is_either_in_the_deal_or_too_late(start_first: bool) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            join = flip.join(player(14), press_id=world.press())
            start = flip.start(11, press_id=world.press())
            if start_first:
                started, joined = await asyncio.gather(start, join)
            else:
                joined, started = await asyncio.gather(join, start)

            assert started is Notice.STARTED
            assert joined in (Notice.JOINED, Notice.ALREADY_STARTED)
            locked = [seated.user_id for seated in flip.players]
            assert (14 in locked) is (joined is Notice.JOINED)
            assert locked == ([11, 12, 14] if joined is Notice.JOINED else [11, 12])

            await world.finish(flip)
            (recorded,) = world.store.recorded
            assert list(recorded.deal.player_ids) == locked
            assert [line.user_id for line in recorded.players] == locked

    asyncio.run(scenario())


def test_joins_at_once_seat_everyone_exactly_once() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            joiners = [player(user_id) for user_id in (21, 22, 23, 24, 25)]

            notices = await asyncio.gather(
                *(flip.join(joiner, press_id=world.press()) for joiner in joiners)
            )

            assert notices == [Notice.JOINED] * 5
            seated = [seated.user_id for seated in flip.players]
            assert sorted(seated) == [11, 21, 22, 23, 24, 25]
            assert len(set(seated)) == len(seated)
            last = world.gateway.edits[-1].view
            assert isinstance(last, LobbyView)
            assert last.status is LobbyStatus.OPEN
            assert [seated.user_id for seated in last.players] == seated

    asyncio.run(scenario())


def test_the_same_player_joining_twice_at_once_is_seated_once() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)

            first, second = await asyncio.gather(
                flip.join(player(21), press_id=world.press()),
                flip.join(player(21), press_id=world.press()),
            )

            assert {first, second} == {Notice.JOINED, Notice.ALREADY_JOINED}
            assert [seated.user_id for seated in flip.players] == [11, 21]

    asyncio.run(scenario())


def test_a_repeat_join_and_a_leave_by_a_stranger_change_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            before = len(world.gateway.events)
            repeat, stranger = world.press(), world.press()

            assert await flip.join(player(12), press_id=repeat) is Notice.ALREADY_JOINED
            assert await flip.leave(99, press_id=stranger) is Notice.NOT_IN_LOBBY

            answers = _only_answers_since(world, before)
            assert [(answer.press_id, answer.answer) for answer in answers] == [
                (repeat, Notice.ALREADY_JOINED),
                (stranger, Notice.NOT_IN_LOBBY),
            ]
            assert [seated.user_id for seated in flip.players] == [11, 12, 13]
            assert flip.phase is FlipPhase.LOBBY

    asyncio.run(scenario())


def test_the_starter_keeps_control_after_leaving_the_player_list() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)

            assert await flip.leave(11, press_id=world.press()) is Notice.LEFT
            assert [seated.user_id for seated in flip.players] == [12, 13]
            assert await flip.start(11, press_id=world.press()) is Notice.STARTED

            await world.finish(flip)
            (recorded,) = world.store.recorded
            assert list(recorded.deal.player_ids) == [12, 13]

    asyncio.run(scenario())


def test_a_starter_who_left_the_list_can_still_cancel() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await flip.leave(11, press_id=world.press())

            assert await flip.cancel(11, press_id=world.press()) is Notice.CANCELLED

            assert flip.phase is FlipPhase.CANCELLED
            last = world.gateway.edits[-1].view
            assert isinstance(last, LobbyView)
            assert last.status is LobbyStatus.CANCELLED
            assert world.registry.flip_for_group(world.group_id) is None

    asyncio.run(scenario())


def test_a_peek_is_answered_while_a_table_edit_is_stuck() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            world.gateway.hold()
            await world.clock.advance(timing.BOARD_GAP_SECONDS)  # Board 2 goes to edit
            assert world.gateway.waiting == 1
            edits_before = len(world.gateway.edits)

            first_press = world.press()
            answer = await flip.peek(11, press_id=first_press)

            assert isinstance(answer, PeekAnswer)
            assert len(answer.hole) == world.settings.x
            assert world.gateway.events[-1] == Answered(
                at=world.clock.monotonic(), press_id=first_press, answer=answer
            )
            assert world.gateway.waiting == 1
            assert len(world.gateway.edits) == edits_before

            for user_id in (12, 13, 99):
                await flip.peek(user_id, press_id=world.press())
            assert [answer.answer for answer in world.gateway.answers[-3:]][-1] is (
                Notice.NOT_A_PLAYER
            )
            assert world.gateway.waiting == 1
            assert len(world.gateway.edits) == edits_before

            world.gateway.release()
            await world.finish(flip)
            assert flip.phase is FlipPhase.FINISHED

    asyncio.run(scenario())


def test_a_join_is_answered_before_the_lobby_edit_it_causes() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            world.gateway.hold()
            press = world.press()

            task = asyncio.create_task(flip.join(player(14), press_id=press))
            await world.clock.quiesce()

            assert world.gateway.waiting == 1
            assert not task.done()
            assert world.gateway.events[-1] == Answered(
                at=world.clock.monotonic(), press_id=press, answer=Notice.JOINED
            )

            world.gateway.release()
            assert await task is Notice.JOINED
            *_, answered, edited = world.gateway.events
            assert isinstance(answered, Answered)
            assert isinstance(edited, Edited)
            assert isinstance(edited.view, LobbyView)
            assert [seated.user_id for seated in edited.view.players] == [11, 12, 14]

    asyncio.run(scenario())


def test_a_start_is_answered_before_the_commitment_is_posted() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            world.gateway.hold()
            press = world.press()

            assert await flip.start(11, press_id=press) is Notice.STARTED
            await world.clock.quiesce()

            assert world.gateway.waiting == 1
            assert world.gateway.views_of(CommitmentView) == ()
            assert world.gateway.events[-1] == Answered(
                at=world.clock.monotonic(), press_id=press, answer=Notice.STARTED
            )

            world.gateway.release()
            await world.clock.quiesce()
            events = world.gateway.events
            answered_at = events.index(
                Answered(at=events[-1].at, press_id=press, answer=Notice.STARTED)
            )
            commitment = next(
                index
                for index, event in enumerate(events)
                if isinstance(event, Posted) and isinstance(event.view, CommitmentView)
            )
            assert answered_at < commitment

    asyncio.run(scenario())


def test_presses_on_a_finished_flip_answer_expired_and_change_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)
            await world.finish(flip)
            before = len(world.gateway.events)

            presses = [
                await flip.join(player(14), press_id=world.press()),
                await flip.leave(12, press_id=world.press()),
                await flip.start(11, press_id=world.press()),
                await flip.cancel(11, press_id=world.press()),
                await flip.peek(11, press_id=world.press()),
            ]

            assert presses == [Notice.EXPIRED] * 5
            answers = _only_answers_since(world, before)
            assert [answer.answer for answer in answers] == [Notice.EXPIRED] * 5
            assert flip.phase is FlipPhase.FINISHED
            assert [seated.user_id for seated in flip.players] == [11, 12]
            assert len(world.store.recorded) == 1

    asyncio.run(scenario())


def test_presses_on_a_cancelled_flip_answer_expired_and_change_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await flip.cancel(11, press_id=world.press())
            before = len(world.gateway.events)

            presses = [
                await flip.join(player(14), press_id=world.press()),
                await flip.leave(12, press_id=world.press()),
                await flip.start(11, press_id=world.press()),
                await flip.cancel(11, press_id=world.press()),
                await flip.peek(11, press_id=world.press()),
            ]

            assert presses == [Notice.EXPIRED] * 5
            assert [answer.answer for answer in _only_answers_since(world, before)] == [
                Notice.EXPIRED
            ] * 5
            assert flip.phase is FlipPhase.CANCELLED
            assert world.store.recorded == []
            assert world.gateway.views_of(TableView) == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("turns", range(7))
def test_closing_the_registry_stops_a_timer_whatever_turn_the_cancel_lands_on(
    turns: int,
) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            await flip.join(player(14), press_id=world.press())
            for _ in range(turns):
                await asyncio.sleep(0)
            events = len(world.gateway.events)

            world.registry.close()
            await world.clock.quiesce()

            assert world.clock.sleeping == 0
            await world.clock.advance(3600)
            assert len(world.gateway.events) == events
            assert flip.phase is FlipPhase.LOBBY

    asyncio.run(scenario())


def test_a_peek_before_the_commitment_is_posted_is_expired_and_shows_no_cards() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.gateway.hold()
            await world.start(flip)
            assert world.gateway.waiting == 1
            assert world.gateway.views_of(CommitmentView) == ()

            press = world.press()
            answer = await flip.peek(11, press_id=press)

            assert answer is Notice.EXPIRED
            assert world.gateway.events[-1] == Answered(
                at=world.clock.monotonic(), press_id=press, answer=Notice.EXPIRED
            )
            assert await flip.peek(99, press_id=world.press()) is Notice.EXPIRED

            world.gateway.release()
            await world.clock.quiesce()
            assert len(world.gateway.views_of(CommitmentView)) == 1

            shown = await flip.peek(11, press_id=world.press())
            assert isinstance(shown, PeekAnswer)
            assert len(shown.hole) == world.settings.x
            assert await flip.peek(99, press_id=world.press()) is Notice.NOT_A_PLAYER

    asyncio.run(scenario())


def test_a_peek_after_the_commitment_shows_cards_while_the_table_is_still_undrawn() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAView(
            clock,
            chosen=lambda view: isinstance(view, TableView) and view.stage is TableStage.HOLE_CARDS,
        )
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            assert gateway.reached.is_set()
            assert len(gateway.views_of(CommitmentView)) == 1
            assert gateway.views_of(TableView) == ()

            answer = await flip.peek(11, press_id=world.press())

            assert isinstance(answer, PeekAnswer)
            assert len(answer.hole) == world.settings.x
            assert await flip.peek(99, press_id=world.press()) is Notice.NOT_A_PLAYER

            gateway.go.set()
            await world.finish(flip)
            assert flip.phase is FlipPhase.FINISHED

    asyncio.run(scenario())


SLOW = "slow-press"


def _lobby_press(flip: Flip, kind: str) -> Coroutine[None, None, Notice]:
    """A Join by player 14 or a Leave by player 13, on the press id the gateway holds up."""
    if kind == "join":
        return flip.join(player(14), press_id=SLOW)
    return flip.leave(13, press_id=SLOW)


def _own_notice(kind: str) -> Notice:
    return Notice.JOINED if kind == "join" else Notice.LEFT


@pytest.mark.parametrize("kind", ["join", "leave"])
def test_a_slow_join_or_leave_still_shows_the_lobby_while_the_flip_is_open(kind: str) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock, slow_press=SLOW)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            task = asyncio.create_task(_lobby_press(flip, kind))
            await gateway.reached.wait()
            shown_before = len(gateway.views)

            gateway.go.set()

            assert await task is _own_notice(kind)
            assert len(gateway.views) == shown_before + 1
            last = gateway.views[-1]
            assert isinstance(last, LobbyView)
            assert last.status is LobbyStatus.OPEN
            expected = [11, 12, 13, 14] if kind == "join" else [11, 12]
            assert [seated.user_id for seated in last.players] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["join", "leave"])
def test_a_slow_join_or_leave_does_not_reopen_a_lobby_that_was_cancelled(kind: str) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock, slow_press=SLOW)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            task = asyncio.create_task(_lobby_press(flip, kind))
            await gateway.reached.wait()

            assert await flip.cancel(11, press_id=world.press()) is Notice.CANCELLED
            gateway.go.set()

            assert await task is _own_notice(kind)
            await world.clock.quiesce()
            statuses = _lobby_statuses(world)
            assert statuses[-1] is LobbyStatus.CANCELLED
            assert statuses.count(LobbyStatus.CANCELLED) == 1
            last = gateway.views[-1]
            assert isinstance(last, LobbyView)
            assert last.status is LobbyStatus.CANCELLED
            assert flip.phase is FlipPhase.CANCELLED

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["join", "leave"])
def test_a_slow_join_or_leave_does_not_overwrite_the_table_of_a_round_that_started(
    kind: str,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock, slow_press=SLOW)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            task = asyncio.create_task(_lobby_press(flip, kind))
            await gateway.reached.wait()

            await world.start(flip)
            assert gateway.views_of(TableView) != ()
            lobbies_before = len(gateway.views_of(LobbyView))
            gateway.go.set()

            assert await task is _own_notice(kind)
            await world.clock.quiesce()
            first_table = next(
                index for index, view in enumerate(gateway.views) if isinstance(view, TableView)
            )
            assert not any(isinstance(view, LobbyView) for view in gateway.views[first_table:])
            assert len(gateway.views_of(LobbyView)) == lobbies_before

    asyncio.run(scenario())

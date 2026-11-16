"""The flip's clock: when the peek window opens and closes, the pace of the deal and
showdown, and how many messages a flip spends.

Every test reads the gateway's timestamps, which are the fake clock's readings, so a
changed constant or a dropped sleep in the engine moves a number asserted here.
Offsets are seconds after Start. The full flip, the unhappy paths and the races are
in the other ``test_engine_*`` files.
"""

from __future__ import annotations

import asyncio
import itertools

from esgbot.engine import (
    CommitmentView,
    Flip,
    PeekAnswer,
    ResultView,
    ShowdownStep,
    ShowdownView,
    TableStage,
    TableView,
    timing,
)
from esgbot.game.rules import Settings
from tests.engine_helpers import (
    Edited,
    FakeClock,
    Harness,
    HoldsAView,
    harness,
    harness_over,
)

WINDOW_OPENS = timing.BOARD_GAP_SECONDS
"""Offset of the last card dealt: the peek window opens here."""

LAST_STEP = (len(ShowdownStep) - 1) * timing.SHOWDOWN_STEP_SECONDS


async def _begin(world: Harness, *, joiners: int) -> tuple[Flip, float]:
    """Open a lobby, press Start, and return the flip and the clock reading at Start."""
    flip = await world.open_lobby(joiners=joiners)
    started = world.clock.monotonic()
    await world.start(flip)
    return flip, started


async def _advance_to(world: Harness, started: float, offset: float) -> None:
    await world.clock.advance(started + offset - world.clock.monotonic())


async def _peek(world: Harness, flip: Flip, user_id: int) -> PeekAnswer:
    answer = await flip.peek(user_id, press_id=world.press())
    assert isinstance(answer, PeekAnswer)
    await world.clock.quiesce()
    return answer


def _table_edits(world: Harness) -> list[Edited]:
    return [edit for edit in world.gateway.edits if isinstance(edit.view, TableView)]


def _stages(edits: list[Edited]) -> list[TableStage]:
    stages = []
    for edit in edits:
        assert isinstance(edit.view, TableView)
        stages.append(edit.view.stage)
    return stages


def _peeking_edits(world: Harness) -> list[Edited]:
    return [
        edit
        for edit in _table_edits(world)
        if isinstance(edit.view, TableView) and edit.view.stage is TableStage.PEEKING
    ]


def _showdown_edits(world: Harness) -> list[Edited]:
    return [edit for edit in world.gateway.edits if isinstance(edit.view, ShowdownView)]


def _showdown_offsets(world: Harness, started: float) -> list[float]:
    return [edit.at - started for edit in _showdown_edits(world)]


def _peeked_counts(world: Harness) -> list[int]:
    counts = []
    for edit in _peeking_edits(world):
        assert isinstance(edit.view, TableView)
        counts.append(edit.view.peeked)
    return counts


def _result_offset(world: Harness, started: float) -> float:
    (result,) = (post for post in world.gateway.posts if isinstance(post.view, ResultView))
    return result.at - started


def test_nobody_peeking_ends_the_window_thirty_seconds_after_it_opens() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await world.finish(flip)

            (opening,) = _peeking_edits(world)
            assert opening.at - started == WINDOW_OPENS

            first = WINDOW_OPENS + timing.PEEK_WINDOW_SECONDS
            assert first == 32.0
            assert _showdown_offsets(world, started) == [first + 3.0 * n for n in range(5)]
            assert _result_offset(world, started) == first + LAST_STEP

    asyncio.run(scenario())


def test_nothing_is_shown_before_the_thirty_seconds_are_up() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, WINDOW_OPENS + timing.PEEK_WINDOW_SECONDS - 0.5)
            assert _showdown_edits(world) == []
            assert not flip.closed

            await world.clock.advance(0.5)
            assert len(_showdown_edits(world)) == 1

    asyncio.run(scenario())


def test_everyone_peeking_after_the_window_opens_brings_the_showdown_ten_seconds_on() -> None:
    peeked_at = 3.0

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, peeked_at)
            for user_id in (11, 12, 13):
                answer = await _peek(world, flip, user_id)
                assert len(answer.extra) == world.settings.y
            await world.finish(flip)

            first = peeked_at + timing.PEEK_ALL_SECONDS
            assert first == 13.0
            assert _showdown_offsets(world, started)[0] == first
            assert _result_offset(world, started) == first + LAST_STEP

    asyncio.run(scenario())


def test_the_ten_seconds_run_from_the_window_opening_when_everyone_peeked_before_it() -> None:
    async def scenario() -> None:
        world = harness(settings=Settings(wager=50, x=5, y=0, z=3))
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, 1.0)
            for user_id in (11, 12, 13):
                await _peek(world, flip, user_id)
            await world.finish(flip)

            (opening,) = _peeking_edits(world)
            assert opening.at - started == WINDOW_OPENS
            assert _peeked_counts(world) == [3]
            first = WINDOW_OPENS + timing.PEEK_ALL_SECONDS
            assert first == 12.0
            assert _showdown_offsets(world, started)[0] == first

    asyncio.run(scenario())


def test_hands_that_are_final_from_the_start_count_a_peek_during_the_board_reveal() -> None:
    async def scenario() -> None:
        world = harness(settings=Settings(wager=50, x=5, y=0, z=3))
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, 1.0)
            answer = await _peek(world, flip, 11)
            assert len(answer.hole) == world.settings.x
            assert answer.extra == ()

            await _advance_to(world, started, WINDOW_OPENS)
            (opening,) = _peeking_edits(world)
            assert opening.at - started == WINDOW_OPENS
            assert isinstance(opening.view, TableView)
            assert opening.view.extra_count == 0
            assert opening.view.peeked == 1

            await world.finish(flip)
            assert _showdown_offsets(world, started)[0] == WINDOW_OPENS + 30.0

    asyncio.run(scenario())


def test_a_deck_too_short_for_extra_cards_counts_peeks_from_the_start() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=7)
            assert flip.player_count == 8
            await _advance_to(world, started, 1.0)
            answer = await _peek(world, flip, 11)
            assert answer.extra == ()

            await _advance_to(world, started, WINDOW_OPENS)
            (opening,) = _peeking_edits(world)
            assert opening.at - started == WINDOW_OPENS
            assert isinstance(opening.view, TableView)
            assert opening.view.extra_count == 0
            assert opening.view.peeked == 1
            assert {seat.card_count for seat in opening.view.seats} == {world.settings.x}

            # The one peek during the board reveal was the only one, so all eight
            # peeking later is what ends the window ten seconds after the last.
            await _advance_to(world, started, 4.0)
            for user_id in range(12, 19):
                await _peek(world, flip, user_id)
            await world.finish(flip)
            assert _showdown_offsets(world, started)[0] == 4.0 + timing.PEEK_ALL_SECONDS

    asyncio.run(scenario())


def test_the_ten_second_wait_is_cut_off_at_thirty_seconds_from_the_window_opening() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            for offset, user_id in ((5.0, 11), (12.0, 12), (25.0, 13)):
                await _advance_to(world, started, offset)
                await _peek(world, flip, user_id)
            await world.finish(flip)

            assert 25.0 + timing.PEEK_ALL_SECONDS > WINDOW_OPENS + timing.PEEK_WINDOW_SECONDS
            assert _showdown_offsets(world, started)[0] == 32.0

    asyncio.run(scenario())


def test_a_peek_before_the_extra_cards_does_not_count_towards_everyone_peeking() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, 1.0)
            for user_id in (11, 12, 13):
                answer = await _peek(world, flip, user_id)
                assert len(answer.hole) == world.settings.x
                assert answer.extra == ()
            await world.finish(flip)

            assert _peeked_counts(world) == [0]
            assert _showdown_offsets(world, started)[0] == 32.0

    asyncio.run(scenario())


def test_a_repeat_peek_does_not_push_the_deadline_out() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, 3.0)
            await _peek(world, flip, 11)
            await _peek(world, flip, 12)
            await _advance_to(world, started, 5.0)
            await _peek(world, flip, 13)
            # Everyone has now peeked, so the showdown is due at 15.
            for offset, user_id in ((9.0, 13), (12.0, 11), (14.5, 12)):
                await _advance_to(world, started, offset)
                await _peek(world, flip, user_id)
            await world.finish(flip)

            assert _showdown_offsets(world, started)[0] == 5.0 + timing.PEEK_ALL_SECONDS

    asyncio.run(scenario())


def test_board_two_comes_two_seconds_after_board_one() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await world.clock.advance(timing.BOARD_GAP_SECONDS)

            hole, board1, board2, peeking = _table_edits(world)
            assert _stages([hole, board1, board2, peeking]) == [
                TableStage.HOLE_CARDS,
                TableStage.BOARD1,
                TableStage.BOARD2,
                TableStage.PEEKING,
            ]
            assert hole.at == started
            assert board1.at == started
            assert board2.at - board1.at == timing.BOARD_GAP_SECONDS
            assert peeking.at == board2.at
            assert flip.message_id == hole.message_id

    asyncio.run(scenario())


def test_the_showdown_reveals_a_step_every_three_seconds_and_posts_the_result_with_the_last() -> (
    None
):
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, _ = await _begin(world, joiners=2)
            await world.finish(flip)

            edits = _showdown_edits(world)
            steps = []
            for edit in edits:
                assert isinstance(edit.view, ShowdownView)
                steps.append(edit.view.step)
            assert steps == list(ShowdownStep)
            gaps = [later.at - earlier.at for earlier, later in itertools.pairwise(edits)]
            assert gaps == [timing.SHOWDOWN_STEP_SECONDS] * 4

            (result,) = (post for post in world.gateway.posts if isinstance(post.view, ResultView))
            assert result.at == edits[-1].at
            assert world.gateway.events[-1] is result

    asyncio.run(scenario())


def test_peeks_inside_one_interval_make_a_single_count_edit_at_the_end_of_it() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, 2.5)
            await _peek(world, flip, 11)
            await _advance_to(world, started, 3.5)
            await _peek(world, flip, 12)
            await _advance_to(world, started, 20.0)

            opening, counted = _peeking_edits(world)
            assert opening.at - started == WINDOW_OPENS
            assert counted.at - opening.at == timing.PEEK_EDIT_INTERVAL_SECONDS
            assert counted.at - started == 7.0
            assert _peeked_counts(world) == [0, 2]

    asyncio.run(scenario())


def test_the_count_edit_shows_the_count_at_the_moment_it_goes_out() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=3)
            await _advance_to(world, started, 2.5)
            await _peek(world, flip, 11)
            await _advance_to(world, started, 3.5)
            await _peek(world, flip, 12)
            await _advance_to(world, started, 6.5)
            await _peek(world, flip, 13)
            await _advance_to(world, started, 6.9)
            assert _peeked_counts(world) == [0]

            await _advance_to(world, started, 7.0)
            assert _peeked_counts(world) == [0, 3]

    asyncio.run(scenario())


def test_a_peek_after_the_interval_has_passed_edits_the_count_at_once() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=3)
            await _advance_to(world, started, 2.5)
            await _peek(world, flip, 11)
            await _peek(world, flip, 12)
            await _advance_to(world, started, 12.0)
            assert [edit.at - started for edit in _peeking_edits(world)] == [2.0, 7.0]

            await _peek(world, flip, 13)
            edits = _peeking_edits(world)
            assert [edit.at - started for edit in edits] == [2.0, 7.0, 12.0]
            assert _peeked_counts(world) == [0, 2, 3]

    asyncio.run(scenario())


def test_the_edit_budget_of_a_full_flip_with_peeks_spread_across_the_window() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=5)
            assert flip.player_count == 6
            before = len(world.gateway.events)
            started = world.clock.monotonic()
            await world.start(flip)

            # Five of six players peek, one every five seconds, so the window never
            # shortens and every peek lands in a fresh interval.
            for offset, user_id in ((3.0, 11), (8.0, 12), (13.0, 13), (18.0, 14), (23.0, 15)):
                await _advance_to(world, started, offset)
                await _peek(world, flip, user_id)
            await world.finish(flip)

            events = world.gateway.events[before:]
            posts = [event for event in events if event in world.gateway.posts]
            edits = [event for event in events if event in world.gateway.edits]

            assert [type(post.view) for post in posts] == [CommitmentView, ResultView]

            assert _stages([edit for edit in edits if isinstance(edit.view, TableView)]) == [
                TableStage.HOLE_CARDS,  # Start
                TableStage.BOARD1,  # dealing
                TableStage.BOARD2,
                TableStage.PEEKING,
                *[TableStage.PEEKING] * 5,  # the peek counts
            ]
            counts = _peeking_edits(world)[1:]
            assert [edit.at - started for edit in counts] == [7.0, 12.0, 17.0, 22.0, 27.0]
            assert _peeked_counts(world) == [0, 1, 2, 3, 4, 5]
            assert len(counts) <= round(
                timing.PEEK_WINDOW_SECONDS / timing.PEEK_EDIT_INTERVAL_SECONDS
            )

            assert len([edit for edit in edits if isinstance(edit.view, ShowdownView)]) == 5
            # Start's table edit, the three dealing edits, the five peek counts and
            # the five showdown steps.
            assert len(edits) == 1 + 3 + 5 + 5
            assert len(edits) <= 15  # the Tech Plan's budget
            assert len(posts) + 1 == 3  # the lobby post makes the third send

    asyncio.run(scenario())


def test_peeks_whose_popups_failed_do_not_end_the_window_early() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            world.gateway.fail_answers = True
            await _advance_to(world, started, 3.0)
            for user_id in (11, 12, 13):
                await _peek(world, flip, user_id)

            await _advance_to(world, started, 3.0 + timing.PEEK_ALL_SECONDS + 0.5)
            assert _showdown_edits(world) == []
            assert _peeked_counts(world) == [0]

            await _advance_to(world, started, WINDOW_OPENS + timing.PEEK_WINDOW_SECONDS - 0.5)
            assert _showdown_edits(world) == []
            await world.finish(flip)
            assert _peeked_counts(world) == [0]
            assert _showdown_offsets(world, started)[0] == WINDOW_OPENS + timing.PEEK_WINDOW_SECONDS

    asyncio.run(scenario())


def test_a_successful_retry_after_failed_popups_counts_from_the_retry() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            world.gateway.fail_answers = True
            await _advance_to(world, started, 3.0)
            for user_id in (11, 12, 13):
                await _peek(world, flip, user_id)
            world.gateway.fail_answers = False
            await _advance_to(world, started, 5.0)
            for user_id in (11, 12, 13):
                await _peek(world, flip, user_id)

            await _advance_to(world, started, 5.0 + timing.PEEK_ALL_SECONDS - 0.5)
            assert _showdown_edits(world) == []
            await world.finish(flip)

            assert _showdown_offsets(world, started)[0] == 5.0 + timing.PEEK_ALL_SECONDS

    asyncio.run(scenario())


def test_a_peek_with_no_press_id_counts_because_there_is_nothing_to_deliver() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            world.gateway.fail_answers = True  # would sink a press that had to be delivered
            await _advance_to(world, started, 3.0)
            for user_id in (11, 12, 13):
                answer = await flip.peek(user_id)
                assert isinstance(answer, PeekAnswer)
            await world.finish(flip)

            assert _showdown_offsets(world, started)[0] == 3.0 + timing.PEEK_ALL_SECONDS

    asyncio.run(scenario())


def test_a_slow_count_edit_is_not_followed_straight_away_by_another() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAView(
            clock,
            chosen=lambda view: (
                isinstance(view, TableView) and view.stage is TableStage.PEEKING and view.peeked > 0
            ),
        )
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip, started = await _begin(world, joiners=2)
            await _advance_to(world, started, 3.0)
            await _peek(world, flip, 11)

            # The count edit falls due at 7.0 and is stuck in the gateway.
            await _advance_to(world, started, 12.0)
            assert gateway.reached.is_set()
            assert _peeked_counts(world) == [0]
            await _peek(world, flip, 12)

            gateway.go.set()
            await world.clock.quiesce()
            await _advance_to(world, started, 20.0)

            edits = _peeking_edits(world)
            stamps = [edit.at - started for edit in edits]
            assert stamps == [2.0, 12.0, 17.0]
            gaps = [later - earlier for earlier, later in itertools.pairwise(stamps[1:])]
            assert all(gap >= timing.PEEK_EDIT_INTERVAL_SECONDS for gap in gaps)
            assert _peeked_counts(world) == [0, 1, 2]

    asyncio.run(scenario())

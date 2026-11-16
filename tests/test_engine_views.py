"""What the engine's views may and may not carry, and the shapes of a result.

The most important test here is the first: no view handed to the gateway holds a
player's private cards before the showdown. The rest check the dealing views in
order, the commitment and seed through the views, and the result shapes a flip can
come to: a scoop, a chop, a tie all round, a split board, no extra cards and a deck
too short for any.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from fractions import Fraction

import pytest

from esgbot.engine import (
    CommitmentView,
    FlipView,
    LobbyView,
    PeekAnswer,
    ResultView,
    ShowdownStep,
    ShowdownView,
    TableStage,
    TableView,
    VoidReason,
    VoidView,
)
from esgbot.game import fairness
from esgbot.game.cards import Card
from esgbot.game.deal import Deal
from esgbot.game.rules import FlipRequest, Settings
from esgbot.game.scoring import Board
from esgbot.game.settlement import Payment
from esgbot.storage import FinishedFlip
from tests.engine_helpers import (
    Harness,
    harness,
    outcome_for,
    player,
    seed_where,
    use_seed,
)

PLAYER_IDS = [11, 12, 13]


def _cards_in(value: object) -> Iterator[Card]:
    """Every card anywhere inside ``value``, found by walking its fields generically.

    Nothing here names a field, so a hand added to any view, however deep, is found.
    """
    if isinstance(value, Card):
        yield value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _cards_in(getattr(value, field.name))
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _cards_in(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _cards_in(key)
            yield from _cards_in(item)


def _private_cards(deal: Deal) -> set[Card]:
    return {card for hand in deal.hands for card in hand.cards}


def _one_recorded(world: Harness) -> FinishedFlip:
    (recorded,) = world.store.recorded
    return recorded


def _result(world: Harness) -> ResultView:
    result = world.gateway.posts[-1].view
    assert isinstance(result, ResultView)
    return result


def _showdowns(world: Harness) -> list[ShowdownView]:
    return [view for view in world.gateway.views if isinstance(view, ShowdownView)]


def _tables(world: Harness) -> list[TableView]:
    return [view for view in world.gateway.views if isinstance(view, TableView)]


async def _pinned_flip(world: Harness, seed: bytes) -> ResultView:
    flip = await world.open_lobby(joiners=2)
    await world.start(flip)
    await world.finish(flip)
    assert _one_recorded(world).seed == seed
    return _result(world)


def _moved(payments: tuple[Payment, ...], seats: int) -> list[int]:
    moved = [0] * seats
    for payment in payments:
        moved[payment.payer] -= payment.dollars
        moved[payment.receiver] += payment.dollars
    return moved


def test_the_walk_finds_cards_in_a_view_that_carries_them() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)
            deal = _one_recorded(world).deal
            (first, *_) = _showdowns(world)
            assert set(_cards_in(first)) >= _private_cards(deal)
            assert set(_cards_in(_result(world))) >= _private_cards(deal)

    asyncio.run(scenario())


def test_no_view_before_the_showdown_carries_any_players_private_cards() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            assert isinstance(await flip.peek(11, press_id=world.press()), PeekAnswer)
            await world.clock.advance(2.0)  # the extra cards are out, the window is open
            assert isinstance(await flip.peek(11, press_id=world.press()), PeekAnswer)
            assert isinstance(await flip.peek(12, press_id=world.press()), PeekAnswer)
            await world.finish(flip)

            deal = _one_recorded(world).deal
            private = _private_cards(deal)
            boards = {*deal.board1, *deal.board2}
            assert private
            assert boards
            assert private.isdisjoint(boards)

            views = world.gateway.views
            first_showdown = next(
                index for index, view in enumerate(views) if isinstance(view, ShowdownView)
            )
            before = views[:first_showdown]
            assert len(before) == 3 + 1 + 4 + len(
                [view for view in before if isinstance(view, TableView) and view.peeked]
            )
            assert any(isinstance(view, TableView) and view.peeked for view in before)

            for view in before:
                seen = set(_cards_in(view))
                assert seen.isdisjoint(private), type(view).__name__
                if isinstance(view, (LobbyView, CommitmentView)):
                    assert seen == set()
                else:
                    assert isinstance(view, TableView)
                    assert seen <= boards

            for table in _tables(world):
                assert [seat.card_count for seat in table.seats] == [deal.x + table.extra_count] * 3
                assert sum(seat.card_count for seat in table.seats) == 3 * (
                    deal.x + table.extra_count
                )
                assert table.extra_count in (0, deal.extra_count)

            # After the showdown the hands are public, so the walk does see them.
            after = views[first_showdown:]
            assert all(set(_cards_in(view)) >= private for view in after)

    asyncio.run(scenario())


def test_a_peek_answer_holds_only_that_players_own_cards() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            early: dict[int, PeekAnswer] = {}
            late: dict[int, PeekAnswer] = {}
            for user_id in PLAYER_IDS:
                answer = await flip.peek(user_id, press_id=world.press())
                assert isinstance(answer, PeekAnswer)
                early[user_id] = answer
            await world.clock.advance(2.0)
            for user_id in PLAYER_IDS:
                answer = await flip.peek(user_id, press_id=world.press())
                assert isinstance(answer, PeekAnswer)
                late[user_id] = answer
            await world.finish(flip)

            deal = _one_recorded(world).deal
            assert deal.extra_count == 2
            for seat, user_id in enumerate(PLAYER_IDS):
                own = deal.hands[seat]
                others = {
                    card for other in deal.hands if other.seat != seat for card in other.cards
                }
                assert early[user_id].seat == seat
                assert early[user_id].hole == own.hole
                assert early[user_id].extra == ()
                assert early[user_id].cards == own.hole
                assert late[user_id].seat == seat
                assert late[user_id].hole == own.hole
                assert late[user_id].extra == own.extra
                assert late[user_id].cards == own.hole + own.extra
                for answer in (early[user_id], late[user_id]):
                    assert set(answer.cards).isdisjoint(others)

    asyncio.run(scenario())


def test_the_dealing_views_come_in_order_with_boards_and_counts_growing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)

            deal = _one_recorded(world).deal
            tables = _tables(world)
            assert [table.stage for table in tables] == [
                TableStage.HOLE_CARDS,
                TableStage.BOARD1,
                TableStage.BOARD2,
                TableStage.PEEKING,
            ]
            hole, board1, board2, peeking = tables
            assert (hole.board1, hole.board2) == ((), ())
            assert (board1.board1, board1.board2) == (deal.board1, ())
            assert (board2.board1, board2.board2) == (deal.board1, deal.board2)

            for table in (hole, board1, board2):
                assert table.extra_count == 0
                assert [seat.card_count for seat in table.seats] == [world.settings.x] * 3
            assert peeking.extra_count == world.settings.y
            assert [seat.card_count for seat in peeking.seats] == [
                world.settings.x + world.settings.y
            ] * 3

            for table in tables:
                assert table.button_seat == deal.button_seat
                assert [seat.seat for seat in table.seats] == [0, 1, 2]
                assert [seat.player.user_id for seat in table.seats] == PLAYER_IDS

            result = _result(world)
            commitment = next(
                view for view in world.gateway.views if isinstance(view, CommitmentView)
            )
            assert commitment.commitment == fairness.commitment(result.seed)
            carriers: list[TableView | ShowdownView | ResultView] = [
                *tables,
                *_showdowns(world),
                result,
            ]
            assert {view.commitment for view in carriers} == {commitment.commitment}
            assert {view.button_seat for view in carriers} == {deal.button_seat}

    asyncio.run(scenario())


def test_the_boards_stay_on_the_table_while_the_peek_window_is_open() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.deal(flip)
            await flip.peek(11, press_id=world.press())
            await world.clock.advance(5.0)
            await world.finish(flip)
            deal = _one_recorded(world).deal
            peeking = [t for t in _tables(world) if t.stage is TableStage.PEEKING]
            tables = _tables(world)
            assert (tables[0].stage, tables[0].board1, tables[0].board2) == (
                TableStage.HOLE_CARDS,
                (),
                (),
            )
            assert peeking
            for table in peeking:
                assert (table.board1, table.board2) == (deal.board1, deal.board2)

    asyncio.run(scenario())


def test_a_scoop_pays_the_wager_times_z_from_every_other_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = harness()
        seed = seed_where(
            lambda outcome: outcome.scoop_seat is not None,
            player_ids=PLAYER_IDS,
            settings=world.settings,
        )
        expected = outcome_for(seed, PLAYER_IDS, world.settings)
        use_seed(monkeypatch, seed)
        async with world.registry:
            result = await _pinned_flip(world, seed)
            scooper = expected.scoop_seat
            assert scooper is not None
            owed = world.settings.wager * world.settings.z
            assert result.scoop_seat == scooper
            assert result.winners == (scooper,)
            assert [hand.scooped for hand in result.hands] == [seat == scooper for seat in range(3)]
            assert [hand.net_dollars for hand in result.hands] == [
                owed * 2 if seat == scooper else -owed for seat in range(3)
            ]
            assert result.hands[scooper].points == Fraction(3)
            assert _showdowns(world)[-1].scoop_seat == scooper
            assert not result.all_tied

            recorded = _one_recorded(world)
            assert [line.scooped for line in recorded.players] == [
                hand.scooped for hand in result.hands
            ]
            assert [line.net_dollars for line in recorded.players] == [
                hand.net_dollars for hand in result.hands
            ]
            assert [line.points for line in recorded.players] == [
                hand.points for hand in result.hands
            ]

    asyncio.run(scenario())


def test_a_chop_shares_the_pot_between_two_winners_and_pays_the_nets_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = harness()
        seed = seed_where(
            lambda outcome: len(outcome.winners) == 2 and outcome.scoop_seat is None,
            player_ids=PLAYER_IDS,
            settings=world.settings,
        )
        expected = outcome_for(seed, PLAYER_IDS, world.settings)
        use_seed(monkeypatch, seed)
        async with world.registry:
            result = await _pinned_flip(world, seed)
            first, second = expected.winners
            assert result.winners == (first, second)
            assert result.scoop_seat is None
            assert not result.all_tied
            assert result.hands[first].points == result.hands[second].points
            assert all(hand.winner == (hand.seat in (first, second)) for hand in result.hands)

            assert result.pot == world.settings.pot(3) == 150
            winners_take = sum(
                result.hands[seat].net_dollars + world.settings.wager for seat in (first, second)
            )
            assert winners_take == result.pot
            assert [hand.net_dollars for hand in result.hands] == list(expected.settlement.nets)
            assert result.payments == expected.payments
            assert _moved(result.payments, 3) == [hand.net_dollars for hand in result.hands]
            assert not any(hand.scooped for hand in result.hands)

    asyncio.run(scenario())


def test_a_table_that_ties_all_round_moves_no_money(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        world = harness()
        seed = seed_where(
            lambda outcome: outcome.all_tied, player_ids=PLAYER_IDS, settings=world.settings
        )
        use_seed(monkeypatch, seed)
        async with world.registry:
            result = await _pinned_flip(world, seed)
            assert [hand.net_dollars for hand in result.hands] == [0, 0, 0]
            assert result.payments == ()
            assert result.all_tied
            assert result.winners == (0, 1, 2)
            assert all(hand.winner for hand in result.hands)
            assert [hand.points for hand in result.hands] == [Fraction(1)] * 3
            final = _showdowns(world)[-1]
            assert final.all_tied
            assert final.winners == (0, 1, 2)

    asyncio.run(scenario())


def test_a_split_board_shows_its_winners_and_the_points_add_up_to_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = harness()
        seed = seed_where(
            lambda outcome: any(score.split for score in outcome.boards) and not outcome.all_tied,
            player_ids=PLAYER_IDS,
            settings=world.settings,
        )
        expected = outcome_for(seed, PLAYER_IDS, world.settings)
        use_seed(monkeypatch, seed)
        async with world.registry:
            result = await _pinned_flip(world, seed)
            split = [board for board in result.boards if board.split]
            assert [board.board for board in split] == [
                score.board for score in expected.boards if score.split
            ]
            for board in split:
                assert len(board.winners) >= 2
                assert board.share == Fraction(1, len(board.winners))
                assert board.share in (Fraction(1, 2), Fraction(1, 3))
            for board in result.boards:
                assert board.winners == expected.board(board.board).winners
                assert board.share == Fraction(1, len(board.winners))
            assert sum((hand.points for hand in result.hands), Fraction(0)) == 3

    asyncio.run(scenario())


def test_with_no_extra_cards_the_table_and_the_hands_never_grow() -> None:
    async def scenario() -> None:
        settings = Settings(wager=50, x=5, y=0, z=3)
        world = harness(settings=settings)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)

            tables = _tables(world)
            assert tables[-1].stage is TableStage.PEEKING
            for table in tables:
                assert table.extra_count == 0
                assert [seat.card_count for seat in table.seats] == [5, 5, 5]
            finals: list[ShowdownView | ResultView] = [*_showdowns(world), _result(world)]
            for view in finals:
                assert [hand.extra for hand in view.hands] == [(), (), ()]
                assert [len(hand.cards) for hand in view.hands] == [5, 5, 5]
            assert _one_recorded(world).deal.extra_count == 0

    asyncio.run(scenario())


def test_a_deck_too_short_for_an_extra_round_says_there_are_no_extra_cards() -> None:
    async def scenario() -> None:
        settings = Settings(wager=50, x=5, y=3, z=3)
        world = harness(settings=settings)
        async with world.registry:
            flip = await world.open_lobby(joiners=7)
            assert flip.player_count == 8
            await world.start(flip)
            await world.finish(flip)

            deal = _one_recorded(world).deal
            assert deal.y == 3
            assert deal.extra_count == 0
            tables = _tables(world)
            assert tables[-1].stage is TableStage.PEEKING
            for table in tables:
                assert table.extra_count == 0
                assert [seat.card_count for seat in table.seats] == [5] * 8
            result = _result(world)
            assert [hand.extra for hand in result.hands] == [()] * 8

    asyncio.run(scenario())


def test_the_showdown_steps_each_appear_once_in_order_with_the_same_boards_and_hands() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)

            showdowns = _showdowns(world)
            assert [view.step for view in showdowns] == [
                ShowdownStep.HANDS,
                ShowdownStep.BOARD1,
                ShowdownStep.BOARD2,
                ShowdownStep.HAND_STRENGTH,
                ShowdownStep.POINTS,
            ]
            final = showdowns[-1]
            for view in showdowns:
                assert view.boards == final.boards
                assert view.hands == final.hands
                assert view.winners == final.winners
                assert view.scoop_seat == final.scoop_seat
            assert [board.board for board in final.boards] == list(Board)

            result = _result(world)
            assert result.boards == final.boards
            assert result.hands == final.hands
            assert result.winners == final.winners
            assert result.scoop_seat == final.scoop_seat

    asyncio.run(scenario())


def test_each_showdown_step_reveals_the_boards_up_to_its_own() -> None:
    revealed = {
        ShowdownStep.HANDS: [],
        ShowdownStep.BOARD1: [Board.BOARD1],
        ShowdownStep.BOARD2: [Board.BOARD1, Board.BOARD2],
        ShowdownStep.HAND_STRENGTH: [Board.BOARD1, Board.BOARD2, Board.HAND_STRENGTH],
        ShowdownStep.POINTS: [Board.BOARD1, Board.BOARD2, Board.HAND_STRENGTH],
    }
    assert set(revealed) == set(ShowdownStep)
    for step, boards in revealed.items():
        assert [board for board in Board if step.reveals(board)] == boards


def test_every_view_type_the_flip_produces_is_one_the_tests_know() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)
            kinds: set[type[FlipView]] = {type(view) for view in world.gateway.views}
            assert kinds == {LobbyView, CommitmentView, TableView, ShowdownView, ResultView}

    asyncio.run(scenario())


def test_the_lobby_view_carries_the_request_and_is_full_at_its_cap() -> None:
    async def scenario() -> None:
        world = harness()
        request = FlipRequest(wager=50, x=21)
        async with world.registry:
            flip = await world.open_lobby(joiners=0, settings=request)
            (opening,) = world.gateway.posts
            assert isinstance(opening.view, LobbyView)
            assert opening.view.request == request
            assert not hasattr(opening.view, "settings")
            assert opening.view.request.max_players == 2
            assert not opening.view.full

            await flip.join(player(12), press_id=world.press())
            lobby = world.gateway.edits[-1].view
            assert isinstance(lobby, LobbyView)
            assert lobby.request == request
            assert lobby.full

    asyncio.run(scenario())


def test_the_lobby_of_a_plain_settings_flip_carries_it_as_a_request() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            await world.open_lobby(joiners=1)
            lobby = world.gateway.views_of(LobbyView)[-1]
            assert isinstance(lobby, LobbyView)
            assert lobby.request == FlipRequest(wager=50, x=5, y=2, z=3)
            assert not lobby.request.y_defaulted
            assert not lobby.request.z_defaulted

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("request_", "y_defaulted", "resolved"),
    [
        (FlipRequest(wager=50, x=5), True, Settings(wager=50, x=5, y=21, z=3)),
        (FlipRequest(wager=50, x=5, y=21), False, Settings(wager=50, x=5, y=21, z=3)),
        (FlipRequest(wager=50, x=5, z=2), True, Settings(wager=50, x=5, y=21, z=2)),
        (FlipRequest(wager=50, x=5, y=2), False, Settings(wager=50, x=5, y=2, z=3)),
    ],
)
def test_every_dealing_view_carries_the_resolved_settings_and_whether_y_was_left_out(
    request_: FlipRequest, y_defaulted: bool, resolved: Settings
) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2, settings=request_)
            await world.start(flip)
            await world.finish(flip)

            tables = _tables(world)
            showdowns = _showdowns(world)
            assert tables
            assert showdowns
            finals: list[TableView | ShowdownView | ResultView] = [
                *tables,
                *showdowns,
                _result(world),
            ]
            for view in finals:
                assert view.settings == resolved
                assert view.y_defaulted is y_defaulted
            assert _one_recorded(world).settings == resolved

    asyncio.run(scenario())


def test_a_void_notice_for_a_flip_that_never_started_has_a_request_and_no_settings() -> None:
    async def scenario() -> None:
        world = harness()
        request = FlipRequest(wager=50, x=5)
        async with world.registry:
            flip = await world.open_lobby(joiners=1, settings=request)
            await flip.void(VoidReason.TIMED_OUT)

            void = world.gateway.posts[-1].view
            assert isinstance(void, VoidView)
            assert not void.started
            assert void.settings is None
            assert void.request == request
            assert void.seed is None

    asyncio.run(scenario())


def test_a_void_notice_for_a_started_flip_carries_the_resolved_settings_and_the_request() -> None:
    async def scenario() -> None:
        world = harness()
        request = FlipRequest(wager=50, x=5)
        async with world.registry:
            flip = await world.open_lobby(joiners=2, settings=request)
            await world.start(flip)
            await flip.void(VoidReason.TIMED_OUT)

            void = world.gateway.posts[-1].view
            assert isinstance(void, VoidView)
            assert void.started
            assert void.settings == Settings(wager=50, x=5, y=21, z=3)
            assert void.request == request

    asyncio.run(scenario())

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from esgbot.game import fairness
from esgbot.game.cards import parse_cards
from esgbot.game.deal import deal_cards
from esgbot.game.evaluator import BestHand, best_hand, best_hand_for_board, rank_five
from esgbot.game.scoring import (
    BOARD_POINT,
    POINTS_IN_PLAY,
    SCORED_BOARDS,
    Board,
    BoardScore,
    Scorecard,
    score_board,
    score_deal,
)
from tests.scoring_helpers import (
    ACE_HIGH_OTHER_SUITS,
    LADDER,
    board_of,
    make_hand,
    scorecard_of,
)


def test_board_enum_order_and_community_flag() -> None:
    assert [int(b) for b in Board] == [0, 1, 2]
    assert [b.name for b in Board] == ["BOARD1", "BOARD2", "HAND_STRENGTH"]
    assert Board.BOARD1.community
    assert Board.BOARD2.community
    assert not Board.HAND_STRENGTH.community


def test_point_constants() -> None:
    assert SCORED_BOARDS == 3
    assert BOARD_POINT == 1
    assert POINTS_IN_PLAY == 3


def test_score_board_single_winner() -> None:
    score = board_of(Board.BOARD1, [1, 3, 2])
    assert score.winners == (1,)
    assert not score.split
    assert score.share == Fraction(1)
    assert score.rank == rank_five(parse_cards(LADDER[3]))
    assert [score.points_for(s) for s in range(3)] == [0, 1, 0]
    assert score.player_count == 3


def test_score_board_tie_is_by_rank_not_cards() -> None:
    first = make_hand(LADDER[1])
    second = make_hand(ACE_HIGH_OTHER_SUITS)
    assert first.cards != second.cards
    assert first.rank == second.rank
    score = score_board(Board.BOARD2, [first, second, make_hand(LADDER[0])])
    assert score.winners == (0, 1)
    assert score.split


@pytest.mark.parametrize("tied", [2, 3, 4, 5])
def test_share_is_one_over_winner_count(tied: int) -> None:
    score = board_of(Board.HAND_STRENGTH, [3] * tied + [0])
    assert score.winners == tuple(range(tied))
    assert score.share == Fraction(1, tied)
    assert sum(score.points_for(s) for s in range(tied + 1)) == BOARD_POINT


def test_three_way_split_is_exact_thirds() -> None:
    score = board_of(Board.BOARD1, [2, 2, 2, 0])
    assert score.share == Fraction(1, 3)
    assert isinstance(score.share, Fraction)
    assert score.points_for(0) + score.points_for(1) + score.points_for(2) == 1
    assert score.points_for(3) == 0


def test_board_score_accessors_and_index_errors() -> None:
    score = board_of(Board.BOARD1, [0, 1])
    assert score.hand_for_seat(1) == make_hand(LADDER[1])
    for bad in (-1, 2):
        with pytest.raises(IndexError):
            score.hand_for_seat(bad)
        with pytest.raises(IndexError):
            score.points_for(bad)


def test_score_board_rejects_no_hands() -> None:
    with pytest.raises(ValueError, match="at least one player"):
        score_board(Board.BOARD1, [])


def test_board_score_rejects_no_hands_or_no_winner() -> None:
    with pytest.raises(ValueError, match="at least one player"):
        BoardScore(board=Board.BOARD1, hands=(), winners=())
    with pytest.raises(ValueError, match="no winner"):
        BoardScore(board=Board.BOARD1, hands=(make_hand(LADDER[0]),), winners=())


@pytest.mark.parametrize(
    "winners",
    [(0,), (0, 1), (1, 0), (1, 1), (5,), (-1, 1), (0, 2)],
    ids=["loser", "extra loser", "unsorted", "duplicate", "out of range", "negative", "wrong seat"],
)
def test_board_score_rejects_bad_winners(winners: tuple[int, ...]) -> None:
    hands = tuple(make_hand(LADDER[tier]) for tier in (1, 3, 0))
    # The only valid answer here is (1,).
    with pytest.raises(ValueError, match="BOARD1"):
        BoardScore(board=Board.BOARD1, hands=hands, winners=winners)


def test_board_score_rejects_winners_missing_part_of_a_tie() -> None:
    hands = tuple(make_hand(LADDER[tier]) for tier in (3, 3, 0))
    with pytest.raises(ValueError, match="not the seats"):
        BoardScore(board=Board.BOARD1, hands=hands, winners=(0,))
    assert BoardScore(board=Board.BOARD1, hands=hands, winners=(0, 1)).split


def test_scorecard_sole_winner_of_everything_scoops() -> None:
    card = scorecard_of(([3, 0, 0], [2, 1, 0], [1, 0, 0]))
    assert card.points == (Fraction(3), Fraction(0), Fraction(0))
    assert card.scoop_seat == 0
    assert card.scooped
    assert card.winners == (0,)
    assert card.winning_points == 3
    assert not card.all_tied


def test_scorecard_two_points_is_not_a_scoop() -> None:
    card = scorecard_of(([3, 0, 0], [3, 0, 0], [0, 1, 0]))
    assert card.points == (Fraction(2), Fraction(1), Fraction(0))
    assert card.scoop_seat is None
    assert not card.scooped
    assert card.winners == (0,)
    assert card.winning_points == 2


def test_scorecard_boards_split_between_players_chops_on_points() -> None:
    # Seat 0 takes board 1, seat 1 takes board 2, hand strength is tied: 1.5 each.
    card = scorecard_of(([3, 0, 0], [0, 3, 0], [2, 2, 0]))
    assert card.points == (Fraction(3, 2), Fraction(3, 2), Fraction(0))
    assert card.winners == (0, 1)
    assert not card.scooped
    assert not card.all_tied
    assert card.board(Board.BOARD1).winners == (0,)
    assert card.board(Board.BOARD2).winners == (1,)
    assert card.board(Board.HAND_STRENGTH).winners == (0, 1)


def test_scorecard_three_way_split_adds_back_to_one_point() -> None:
    card = scorecard_of(([3, 0, 0, 0], [2, 2, 2, 0], [3, 0, 0, 0]))
    assert card.points == (Fraction(7, 3), Fraction(1, 3), Fraction(1, 3), Fraction(0))
    assert sum(card.points) == POINTS_IN_PLAY
    assert card.board(Board.BOARD2).share * 3 == 1


def test_scorecard_all_tied_by_total_with_different_board_winners() -> None:
    card = scorecard_of(([3, 0, 0], [0, 3, 0], [0, 0, 3]))
    assert card.points == (Fraction(1),) * 3
    assert card.all_tied
    assert card.winners == (0, 1, 2)
    assert card.scoop_seat is None


def test_scorecard_all_tied_when_every_hand_ties() -> None:
    card = scorecard_of(([2, 2], [2, 2], [2, 2]))
    assert card.points == (Fraction(3, 2), Fraction(3, 2))
    assert card.all_tied
    assert not card.scooped


def test_scorecard_single_player_scoops_themselves() -> None:
    card = scorecard_of(([1], [1], [1]))
    assert card.points == (Fraction(3),)
    assert card.scoop_seat == 0


def test_scorecard_accessors() -> None:
    card = scorecard_of(([3, 0, 0], [0, 3, 0], [3, 0, 0]))
    assert card.player_count == 3
    assert [b.board for b in card.boards] == list(Board)
    assert card.points_for(0) == 2
    assert card.points_for(1) == 1
    assert card.points_for(2) == 0
    assert card.board(Board.HAND_STRENGTH) is card.boards[2]
    for bad in (-1, 3):
        with pytest.raises(IndexError):
            card.points_for(bad)


def test_scorecard_rejects_wrong_board_count() -> None:
    boards = tuple(board_of(Board(i), [1, 0]) for i in range(3))
    with pytest.raises(ValueError, match="holds 3 boards"):
        Scorecard(boards=boards[:2])
    with pytest.raises(ValueError, match="holds 3 boards"):
        Scorecard(boards=(*boards, boards[0]))
    with pytest.raises(ValueError, match="holds 3 boards"):
        Scorecard(boards=())


def test_scorecard_rejects_wrong_board_order() -> None:
    boards = [board_of(Board(i), [1, 0]) for i in range(3)]
    with pytest.raises(ValueError, match="Board order"):
        Scorecard(boards=(boards[1], boards[0], boards[2]))
    with pytest.raises(ValueError, match="Board order"):
        Scorecard(boards=(boards[0], boards[0], boards[2]))


def test_scorecard_rejects_mismatched_player_counts() -> None:
    with pytest.raises(ValueError, match="scored"):
        Scorecard(
            boards=(
                board_of(Board.BOARD1, [1, 0]),
                board_of(Board.BOARD2, [1, 0, 0]),
                board_of(Board.HAND_STRENGTH, [1, 0]),
            )
        )


def _expected_winners(scores: list[BestHand]) -> tuple[int, ...]:
    best = max(hand.rank for hand in scores)
    return tuple(seat for seat, hand in enumerate(scores) if hand.rank == best)


@pytest.mark.parametrize(("players", "x", "y"), [(2, 5, 3), (3, 6, 2), (5, 5, 0), (4, 10, 1)])
def test_score_deal_matches_independent_evaluation(players: int, x: int, y: int) -> None:
    rng = random.Random(7_000 + players)
    for _ in range(15):
        deal = deal_cards(
            seed=rng.randbytes(fairness.SEED_SIZE), player_ids=list(range(players)), x=x, y=y
        )
        card = score_deal(deal)
        holdings = [hand.cards for hand in deal.hands]
        expected = {
            Board.BOARD1: _expected_winners(
                [best_hand_for_board(c, deal.board1) for c in holdings]
            ),
            Board.BOARD2: _expected_winners(
                [best_hand_for_board(c, deal.board2) for c in holdings]
            ),
            Board.HAND_STRENGTH: _expected_winners([best_hand(c) for c in holdings]),
        }
        assert [score.board for score in card.boards] == list(Board)
        assert card.player_count == players
        for board, winners in expected.items():
            score = card.board(board)
            assert score.winners == winners
            assert score.share == Fraction(1, len(winners))
            assert score.player_count == players
        assert sum(card.points) == POINTS_IN_PLAY
        assert card.points == tuple(card.points_for(seat) for seat in range(players))
        best = max(card.points)
        assert card.winning_points == best
        assert card.winners == tuple(s for s, p in enumerate(card.points) if p == best)
        holders = [seat for seat, p in enumerate(card.points) if p == POINTS_IN_PLAY]
        assert card.scoop_seat == (holders[0] if holders else None)
        assert card.scooped == bool(holders)
        assert card.all_tied == (len(set(card.points)) == 1)


def test_score_deal_hands_are_seat_indexed() -> None:
    deal = deal_cards(seed=b"\x21" * fairness.SEED_SIZE, player_ids=[1, 2, 3], x=5, y=2)
    card = score_deal(deal)
    for seat in range(3):
        assert card.board(Board.HAND_STRENGTH).hand_for_seat(seat) == best_hand(
            deal.hands[seat].cards
        )
        assert card.board(Board.BOARD1).hand_for_seat(seat) == best_hand_for_board(
            deal.hands[seat].cards, deal.board1
        )


def test_score_deal_is_deterministic() -> None:
    seed = b"\x42" * fairness.SEED_SIZE
    first = score_deal(deal_cards(seed=seed, player_ids=[5, 6, 7, 8], x=5, y=2))
    second = score_deal(deal_cards(seed=seed, player_ids=[5, 6, 7, 8], x=5, y=2))
    assert first == second

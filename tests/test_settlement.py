from __future__ import annotations

import random
from collections.abc import Sequence
from fractions import Fraction

import pytest

from esgbot.game import fairness
from esgbot.game.deal import deal_cards
from esgbot.game.rules import Settings
from esgbot.game.scoring import POINTS_IN_PLAY, Board, score_deal
from esgbot.game.settlement import (
    FlipOutcome,
    Payment,
    SeatResult,
    Settlement,
    payments_for,
    settle,
    settle_deal,
    settle_points,
)
from tests.scoring_helpers import scorecard_of

SQLITE_LIMIT = 2**63
HALF = Fraction(1, 2)
THIRD = Fraction(1, 3)


def cfg(wager: int, z: int = 1, x: int = 5, y: int = 0) -> Settings:
    return Settings(wager=wager, x=x, y=y, z=z)


def moved_by(payments: Sequence[Payment], seats: int) -> list[int]:
    moved = [0] * seats
    for payment in payments:
        moved[payment.payer] -= payment.dollars
        moved[payment.receiver] += payment.dollars
    return moved


# --- outcomes -------------------------------------------------------------------------


def test_one_winner_takes_wager_from_each_other_player() -> None:
    result = settle_points([2, 1, 0], cfg(10), button_seat=0)
    assert result.nets == (20, -10, -10)
    assert result.pot == 30
    assert result.winners == (0,)
    assert not result.scooped
    assert result.scoop_seat is None
    assert not result.all_tied
    assert moved_by(result.payments, 3) == [20, -10, -10]


def test_scoop_with_z1_pays_the_same_as_an_ordinary_win() -> None:
    scoop = settle_points([0, 3, 0, 0], cfg(25, z=1), button_seat=1)
    ordinary = settle_points([0, 2, 1, 0], cfg(25, z=1), button_seat=1)
    assert scoop.nets == ordinary.nets == (-25, 75, -25, -25)
    assert scoop.payments == ordinary.payments
    assert scoop.scooped
    assert scoop.scoop_seat == 1
    assert not ordinary.scooped


def test_scoop_with_z3_pays_triple() -> None:
    result = settle_points([3, 0, 0], cfg(10, z=3), button_seat=2)
    assert result.nets == (60, -30, -30)
    assert result.pot == 30
    assert result.scoop_seat == 0
    assert result.result_for(0).scooped
    assert result.result_for(0).winner
    assert not result.result_for(1).scooped
    assert moved_by(result.payments, 3) == [60, -30, -30]


def test_non_scoop_ignores_z() -> None:
    low = settle_points([2, 1, 0], cfg(10, z=1), button_seat=0)
    high = settle_points([2, 1, 0], cfg(10, z=100), button_seat=0)
    assert low.nets == high.nets


def test_two_way_chop_gives_odd_dollar_to_first_winner_after_button() -> None:
    # Pot $15 split two ways: $7 each and one left over.
    result = settle_points([3 * HALF, 3 * HALF, 0], cfg(5), button_seat=0)
    assert result.nets == (2, 3, -5)
    assert result.winners == (0, 1)
    assert result.pot == 15
    assert sum(result.nets) == 0


def test_three_way_split_of_a_board_point_is_exact_thirds() -> None:
    points = [2 + THIRD, THIRD, THIRD]
    assert sum(points) == POINTS_IN_PLAY
    result = settle_points(points, cfg(10), button_seat=0)
    assert result.nets == (20, -10, -10)
    assert result.result_for(1).points == THIRD
    assert result.result_for(0).points == Fraction(7, 3)
    assert sum(r.points for r in result.seats) == 3


def test_three_thirds_make_one_point_through_a_scorecard() -> None:
    card = scorecard_of(([3, 0, 0, 0], [2, 2, 2, 0], [3, 0, 0, 0]))
    result = settle(card, cfg(9), button_seat=0)
    assert [r.points for r in result.seats] == [Fraction(7, 3), THIRD, THIRD, Fraction(0)]
    assert sum(r.points for r in result.seats) == 3
    assert result.nets == (27, -9, -9, -9)


def test_chop_with_equal_points_but_different_board_winners() -> None:
    card = scorecard_of(([3, 0, 0], [0, 3, 0], [2, 2, 0]))
    result = settle(card, cfg(20), button_seat=1)
    # Pot $60 split two ways: $30 each, so both winners are $10 up and the loser is out $20.
    assert result.winners == (0, 1)
    assert result.nets == (10, 10, -20)
    assert not result.scooped
    assert [r.points for r in result.seats] == [Fraction(3, 2), Fraction(3, 2), Fraction(0)]


@pytest.mark.parametrize(
    ("button", "odd_dollar_seat"),
    [(0, 1), (1, 0), (2, 0)],
)
def test_three_player_one_dollar_chop_leaves_one_winner_at_zero(
    button: int, odd_dollar_seat: int
) -> None:
    result = settle_points([3 * HALF, 3 * HALF, 0], cfg(1), button_seat=button)
    other = 1 - odd_dollar_seat
    assert result.net_for(odd_dollar_seat) == 1
    assert result.net_for(other) == 0
    assert result.net_for(2) == -1
    assert result.result_for(other).winner
    assert result.winners == (0, 1)
    assert result.payments == (Payment(payer=2, receiver=odd_dollar_seat, dollars=1),)


def test_two_point_sole_winner_is_paid_like_a_normal_win() -> None:
    result = settle_points([HALF, 2, HALF, 0], cfg(7, z=50), button_seat=3)
    assert result.winners == (1,)
    assert not result.scooped
    assert result.nets == (-7, 21, -7, -7)
    assert result.net_for(1) == 7 * (4 - 1)


def test_all_players_tied_nets_zero_and_no_payments() -> None:
    for points in ([1, 1, 1], [Fraction(3, 4)] * 4, [HALF * 3, HALF * 3]):
        result = settle_points(points, cfg(50, z=9), button_seat=0)
        assert result.nets == (0,) * len(points)
        assert result.payments == ()
        assert result.all_tied
        assert result.winners == tuple(range(len(points)))
        assert not result.scooped
        assert result.scoop_seat is None
        assert all(r.winner and not r.scooped for r in result.seats)


def test_all_tied_through_a_scorecard() -> None:
    result = settle(scorecard_of(([2, 2], [2, 2], [2, 2])), cfg(40), button_seat=1)
    assert result.nets == (0, 0)
    assert result.payments == ()
    assert result.all_tied


def test_single_player_point_vector_is_a_scoop_of_nothing() -> None:
    result = settle_points([3], cfg(10), button_seat=0)
    assert result.nets == (0,)
    assert result.payments == ()


# --- odd dollars ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("button", "nets"),
    [(0, (2, 3, -5)), (1, (3, 2, -5)), (2, (3, 2, -5))],
)
def test_odd_dollar_follows_the_button_around_three_seats(
    button: int, nets: tuple[int, ...]
) -> None:
    result = settle_points([3 * HALF, 3 * HALF, 0], cfg(5), button_seat=button)
    assert result.nets == nets


@pytest.mark.parametrize(
    ("button", "extra_seats"),
    [(0, {2, 4}), (1, {2, 4}), (2, {4, 0}), (3, {4, 0}), (4, {0, 2})],
)
def test_two_leftover_dollars_go_one_each_in_seat_order_after_button(
    button: int, extra_seats: set[int]
) -> None:
    # Pot $20 over three winners: $6 each and $2 left. Winners 0, 2, 4; seats 1 and 3 lose.
    result = settle_points([1, 0, 1, 0, 1], cfg(4), button_seat=button)
    for seat in range(5):
        if seat in (1, 3):
            assert result.net_for(seat) == -4
        elif seat in extra_seats:
            assert result.net_for(seat) == 3
        else:
            assert result.net_for(seat) == 2
    assert sum(result.nets) == 0


def test_odd_dollar_skips_a_non_winner_between_button_and_winner() -> None:
    # Button on seat 0, seat 1 is a loser: the extra dollar goes to seat 2, not seat 1.
    result = settle_points([1, 0, 2], cfg(1), button_seat=0)
    assert result.nets == (-1, -1, 2)
    result = settle_points([3 * HALF, 0, 3 * HALF], cfg(1), button_seat=0)
    # Pot $3, two winners: $1 each and $1 left, first winner after seat 0 is seat 2.
    assert result.nets == (0, -1, 1)


def test_no_odd_dollars_when_pot_divides_evenly() -> None:
    for button in range(4):
        result = settle_points([3 * HALF, 3 * HALF, 0, 0], cfg(5), button_seat=button)
        assert result.nets == (5, 5, -5, -5)


# --- payments_for --------------------------------------------------------------------


def test_payments_pair_largest_debtor_with_largest_creditor() -> None:
    assert payments_for([-3, -7, 4, 6]) == (
        Payment(payer=1, receiver=3, dollars=6),
        Payment(payer=0, receiver=2, dollars=3),
        Payment(payer=1, receiver=2, dollars=1),
    )


def test_payments_break_ties_to_the_lower_seat() -> None:
    assert payments_for([-2, -2, 2, 2]) == (
        Payment(payer=0, receiver=2, dollars=2),
        Payment(payer=1, receiver=3, dollars=2),
    )
    assert payments_for([5, -5]) == (Payment(payer=1, receiver=0, dollars=5),)


def test_payments_empty_when_every_net_is_zero() -> None:
    assert payments_for([0, 0, 0]) == ()
    assert payments_for([]) == ()


@pytest.mark.parametrize("nets", [[1, 0], [-1, -1, 1], [5]])
def test_payments_reject_nets_that_do_not_sum_to_zero(nets: list[int]) -> None:
    with pytest.raises(ValueError, match="add up to"):
        payments_for(nets)


# --- property test -------------------------------------------------------------------


def random_points(rng: random.Random, players: int) -> list[Fraction]:
    """Point totals from three boards, each won outright or split by a random set of seats."""
    totals = [Fraction(0)] * players
    for _ in range(3):
        winners = rng.sample(range(players), min(rng.choice([1, 1, 1, 2, 3, players]), players))
        for seat in winners:
            totals[seat] += Fraction(1, len(winners))
    return totals


def test_property_nets_sum_to_zero_and_payments_reproduce_them() -> None:
    rng = random.Random(20240615)
    seen_scoop = seen_chop = seen_fraction = seen_all_tied = False
    for _ in range(3000):
        players = rng.randint(2, 8)
        points = random_points(rng, players)
        assert sum(points) == 3
        wager = rng.choice([1, 2, 3, 7, rng.randint(1, 1_000_000)])
        z = rng.randint(1, 100)
        button = rng.randrange(players)
        result = settle_points(points, cfg(wager, z=z), button_seat=button)

        best = max(points)
        winners = [s for s, p in enumerate(points) if p == best]
        scoop = len(winners) == 1 and best == 3
        seen_scoop |= scoop
        seen_chop |= len(winners) > 1
        seen_fraction |= any(p.denominator != 1 for p in points)
        seen_all_tied |= len(winners) == players

        assert sum(result.nets) == 0
        assert result.pot == wager * players
        assert tuple(result.winners) == tuple(winners)
        assert result.scooped == scoop
        assert [r.points for r in result.seats] == points
        for seat in range(players):
            if seat not in winners:
                assert result.net_for(seat) == -(wager * z if scoop else wager)
            elif scoop:
                assert result.net_for(seat) == wager * z * (players - 1)
            else:
                # The winners share the pot; nobody gets less than the rounded-down share.
                assert result.net_for(seat) >= result.pot // len(winners) - wager
                assert result.net_for(seat) <= result.pot // len(winners) + 1 - wager
        assert moved_by(result.payments, players) == list(result.nets)
        assert payments_for(result.nets) == result.payments
        assert payments_for(list(result.nets)) == result.payments
        for payment in result.payments:
            assert payment.dollars > 0
            assert payment.payer != payment.receiver
    assert seen_scoop
    assert seen_chop
    assert seen_fraction
    assert seen_all_tied


# --- limits --------------------------------------------------------------------------


@pytest.mark.parametrize("button", [0, 3, 7])
def test_maximum_limits_scoop_is_exact_and_fits_sqlite(button: int) -> None:
    result = settle_points([3] + [0] * 7, cfg(1_000_000, z=100), button_seat=button)
    assert result.nets == (700_000_000,) + (-100_000_000,) * 7
    assert result.pot == 8_000_000
    assert sum(result.nets) == 0
    assert all(abs(net) < SQLITE_LIMIT for net in result.nets)
    assert moved_by(result.payments, 8) == list(result.nets)
    assert len(result.payments) == 7
    assert all(p.receiver == 0 and p.dollars == 100_000_000 for p in result.payments)


def test_maximum_limits_single_win_and_chop_are_exact() -> None:
    win = settle_points([2, 1] + [0] * 6, cfg(1_000_000, z=100), button_seat=0)
    assert win.nets == (7_000_000,) + (-1_000_000,) * 7
    chop = settle_points([HALF * 3, HALF * 3] + [0] * 6, cfg(1_000_000, z=100), button_seat=0)
    assert chop.nets == (3_000_000, 3_000_000) + (-1_000_000,) * 6
    for result in (win, chop):
        assert all(abs(net) < SQLITE_LIMIT for net in result.nets)
        assert all(isinstance(net, int) for net in result.nets)
        assert moved_by(result.payments, 8) == list(result.nets)


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "points",
    [
        [],
        [1, 1],
        [2, 2, 0],
        [Fraction(7, 2), Fraction(-1, 2)],
        [4, -1, 0],
    ],
    ids=["empty", "too few", "too many", "negative", "negative whole"],
)
def test_settle_points_rejects_bad_points(points: list[Fraction | int]) -> None:
    with pytest.raises(ValueError, match=r"points|player"):
        settle_points(points, cfg(10), button_seat=0)


@pytest.mark.parametrize("button", [-1, 3, 10])
def test_settle_points_rejects_a_bad_button_seat(button: int) -> None:
    with pytest.raises(ValueError, match="button seat"):
        settle_points([1, 1, 1], cfg(10), button_seat=button)


def test_settle_rejects_a_bad_button_seat() -> None:
    card = scorecard_of(([1, 0], [1, 0], [1, 0]))
    with pytest.raises(ValueError, match="button seat"):
        settle(card, cfg(10), button_seat=2)


def test_payments_for_matches_settle_points_payments() -> None:
    result = settle_points([2, HALF, HALF, 0], cfg(3), button_seat=0)
    assert result.payments == payments_for(result.nets)


def test_seat_result_validation() -> None:
    SeatResult(seat=0, points=Fraction(3), net_dollars=0, winner=True, scooped=True)
    with pytest.raises(ValueError, match="seat must be"):
        SeatResult(seat=-1, points=Fraction(1), net_dollars=0, winner=True, scooped=False)
    with pytest.raises(ValueError, match="negative"):
        SeatResult(seat=0, points=Fraction(-1), net_dollars=0, winner=False, scooped=False)
    with pytest.raises(ValueError, match="scooped without winning"):
        SeatResult(seat=0, points=Fraction(1), net_dollars=0, winner=False, scooped=True)


@pytest.mark.parametrize(
    ("payer", "receiver", "dollars"),
    [(-1, 0, 1), (0, -1, 1), (1, 1, 5), (0, 1, 0), (0, 1, -3)],
)
def test_payment_validation(payer: int, receiver: int, dollars: int) -> None:
    with pytest.raises(ValueError, match=r"seats must|pay itself|more than"):
        Payment(payer=payer, receiver=receiver, dollars=dollars)


def _seats(nets: Sequence[int]) -> tuple[SeatResult, ...]:
    return tuple(
        SeatResult(seat=i, points=Fraction(1), net_dollars=n, winner=True, scooped=False)
        for i, n in enumerate(nets)
    )


def test_settlement_validation() -> None:
    good = Settlement(pot=2, seats=_seats([1, -1]), payments=(Payment(1, 0, 1),))
    assert good.nets == (1, -1)
    with pytest.raises(ValueError, match="add up to"):  # nets do not sum to zero
        Settlement(pot=2, seats=_seats([1, 0]), payments=())
    with pytest.raises(ValueError, match="payments move"):  # payments do not reproduce nets
        Settlement(pot=2, seats=_seats([1, -1]), payments=(Payment(1, 0, 2),))
    with pytest.raises(ValueError, match="payments move"):  # no payments for a non-zero net
        Settlement(pot=2, seats=_seats([1, -1]), payments=())
    with pytest.raises(ValueError, match="not a seat"):  # unknown seat
        Settlement(pot=2, seats=_seats([1, -1]), payments=(Payment(1, 5, 1),))
    with pytest.raises(ValueError, match="cannot be negative"):  # negative pot
        Settlement(pot=-1, seats=_seats([0, 0]), payments=())
    with pytest.raises(ValueError, match="at least one seat"):  # no seats
        Settlement(pot=0, seats=(), payments=())
    out_of_order = (_seats([1, -1])[1], _seats([1, -1])[0])
    with pytest.raises(ValueError, match="seat order"):
        Settlement(pot=2, seats=out_of_order, payments=())


def test_settlement_seat_lookups() -> None:
    result = settle_points([2, 1, 0], cfg(10), button_seat=0)
    assert result.player_count == 3
    assert result.result_for(2).net_dollars == -10
    assert result.net_for(0) == 20
    for bad in (-1, 3):
        with pytest.raises(IndexError):
            result.result_for(bad)
        with pytest.raises(IndexError):
            result.net_for(bad)


def test_settle_deal_rejects_settings_that_disagree_with_the_deal() -> None:
    deal = deal_cards(seed=b"\x01" * fairness.SEED_SIZE, player_ids=[1, 2, 3], x=5, y=2)
    with pytest.raises(ValueError, match="disagree"):
        settle_deal(deal, cfg(10, x=6, y=2))
    with pytest.raises(ValueError, match="disagree"):
        settle_deal(deal, cfg(10, x=5, y=3))
    assert settle_deal(deal, cfg(10, x=5, y=2)).player_count == 3


def test_flip_outcome_rejects_mismatched_halves() -> None:
    two = scorecard_of(([1, 0], [1, 0], [1, 0]))
    three = scorecard_of(([1, 0, 0], [1, 0, 0], [1, 0, 0]))
    with pytest.raises(ValueError, match="were scored but"):
        FlipOutcome(scorecard=two, settlement=settle(three, cfg(10), button_seat=0))
    other = scorecard_of(([0, 1], [0, 1], [0, 1]))
    with pytest.raises(ValueError, match="settled for"):
        FlipOutcome(scorecard=two, settlement=settle(other, cfg(10), button_seat=0))


# --- end to end ----------------------------------------------------------------------


def test_settle_deal_agrees_with_score_deal_and_settle_and_is_repeatable() -> None:
    seed = b"\x5a" * fairness.SEED_SIZE
    settings = cfg(10, z=3, x=5, y=2)
    deal = deal_cards(seed=seed, player_ids=[11, 22, 33, 44], x=5, y=2)
    outcome = settle_deal(deal, settings)
    card = score_deal(deal)
    settlement = settle(card, settings, button_seat=deal.button_seat)

    assert outcome.scorecard == card
    assert outcome.settlement == settlement
    assert outcome.points == card.points
    assert outcome.boards == card.boards
    assert outcome.board(Board.BOARD2) == card.board(Board.BOARD2)
    assert outcome.seats == settlement.seats
    assert outcome.payments == settlement.payments
    assert outcome.pot == 40
    assert outcome.winners == settlement.winners == card.winners
    assert outcome.scoop_seat == card.scoop_seat
    assert outcome.all_tied == card.all_tied
    assert outcome.player_count == 4
    assert outcome.result_for(0) == settlement.result_for(0)
    assert sum(outcome.settlement.nets) == 0

    again = settle_deal(deal_cards(seed=seed, player_ids=[11, 22, 33, 44], x=5, y=2), settings)
    assert again == outcome


def test_settle_deal_over_many_deals_is_consistent() -> None:
    rng = random.Random(99)
    for _ in range(25):
        players = rng.randint(2, 6)
        deal = deal_cards(
            seed=rng.randbytes(fairness.SEED_SIZE), player_ids=list(range(players)), x=5, y=1
        )
        settings = cfg(rng.randint(1, 100), z=rng.randint(1, 5), x=5, y=1)
        outcome = settle_deal(deal, settings)
        assert sum(outcome.settlement.nets) == 0
        assert sum(outcome.points) == POINTS_IN_PLAY
        assert outcome.scoop_seat == outcome.scorecard.scoop_seat
        assert moved_by(outcome.payments, players) == list(outcome.settlement.nets)

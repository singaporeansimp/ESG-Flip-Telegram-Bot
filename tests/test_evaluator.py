from __future__ import annotations

import itertools
import random
import time

import pytest
from treys import Card as TreysCard
from treys import Evaluator as TreysEvaluator

from esgbot.game import fairness
from esgbot.game.cards import FULL_DECK, Card, Rank, Suit, parse_cards
from esgbot.game.deal import deal_cards
from esgbot.game.evaluator import (
    BOARD_CARDS_PLAYED,
    HAND_SIZE,
    HOLE_CARDS_PLAYED,
    BestHand,
    Category,
    HandRank,
    best_hand,
    best_hand_for_board,
    rank_five,
)

_TREYS_EVALUATOR = TreysEvaluator()


def _treys_card(card: Card) -> int:
    return int(TreysCard.new(card.rank.label.replace("10", "T") + card.suit.letter))


def _treys_score(cards: tuple[Card, ...]) -> int:
    return int(_TREYS_EVALUATOR.evaluate([_treys_card(card) for card in cards], []))


def test_high_card() -> None:
    rank = rank_five(parse_cards("A♠ 10♥ 7c 5d 2s"))
    assert rank.category is Category.HIGH_CARD
    assert rank.tiebreak == (Rank.ACE, Rank.TEN, Rank.SEVEN, Rank.FIVE, Rank.TWO)
    assert rank.description == "ace high"


def test_pair() -> None:
    rank = rank_five(parse_cards("K♠ K♥ 7c 5d 2s"))
    assert rank.category is Category.PAIR
    assert rank.tiebreak == (Rank.KING, Rank.SEVEN, Rank.FIVE, Rank.TWO)
    assert rank.description == "pair of kings"


def test_two_pair() -> None:
    rank = rank_five(parse_cards("10♠ 10♥ 4c 4d 2s"))
    assert rank.category is Category.TWO_PAIR
    assert rank.tiebreak == (Rank.TEN, Rank.FOUR, Rank.TWO)
    assert rank.description == "two pair, tens and fours"


def test_three_of_a_kind() -> None:
    rank = rank_five(parse_cards("A♠ A♥ A♦ 5d 2s"))
    assert rank.category is Category.THREE_OF_A_KIND
    assert rank.tiebreak == (Rank.ACE, Rank.FIVE, Rank.TWO)
    assert rank.description == "three of a kind, aces"


def test_straight() -> None:
    rank = rank_five(parse_cards("5♠ 4♥ 3♦ 2c As"))
    assert rank.category is Category.STRAIGHT
    assert rank.tiebreak == (Rank.FIVE,)
    assert rank.description == "straight, five high"


def test_flush() -> None:
    rank = rank_five(parse_cards("Qh Jh 9h 5h 2h"))
    assert rank.category is Category.FLUSH
    assert rank.tiebreak == (Rank.QUEEN, Rank.JACK, Rank.NINE, Rank.FIVE, Rank.TWO)
    assert rank.description == "flush, queen high"


def test_full_house() -> None:
    rank = rank_five(parse_cards("Ks Kh Kd 7c 7d"))
    assert rank.category is Category.FULL_HOUSE
    assert rank.tiebreak == (Rank.KING, Rank.SEVEN)
    assert rank.description == "full house, kings full of sevens"


def test_four_of_a_kind() -> None:
    rank = rank_five(parse_cards("Qs Qh Qd Qc 7c"))
    assert rank.category is Category.FOUR_OF_A_KIND
    assert rank.tiebreak == (Rank.QUEEN, Rank.SEVEN)
    assert rank.description == "four of a kind, queens"


def test_straight_flush() -> None:
    rank = rank_five(parse_cards("9h 8h 7h 6h 5h"))
    assert rank.category is Category.STRAIGHT_FLUSH
    assert rank.tiebreak == (Rank.NINE,)
    assert rank.description == "straight flush, nine high"


def test_royal_flush_description_is_special_cased() -> None:
    rank = rank_five(parse_cards("As Ks Qs Js 10s"))
    assert rank.category is Category.STRAIGHT_FLUSH
    assert rank.tiebreak == (Rank.ACE,)
    assert rank.description == "royal flush"


def test_wheel_tiebreak_and_card_order() -> None:
    hand = best_hand(parse_cards("As 2h 3d 4c 5s"))
    assert hand.rank.category is Category.STRAIGHT
    assert hand.rank.tiebreak == (Rank.FIVE,)
    assert hand.description == "straight, five high"
    assert [card.rank for card in hand.cards] == [
        Rank.FIVE,
        Rank.FOUR,
        Rank.THREE,
        Rank.TWO,
        Rank.ACE,
    ]


def test_wheel_straight_flush_card_order() -> None:
    hand = best_hand(parse_cards("Ah 2h 3h 4h 5h"))
    assert hand.rank.category is Category.STRAIGHT_FLUSH
    assert hand.rank.tiebreak == (Rank.FIVE,)
    assert [card.rank for card in hand.cards] == [
        Rank.FIVE,
        Rank.FOUR,
        Rank.THREE,
        Rank.TWO,
        Rank.ACE,
    ]


def test_wheel_ranks_below_six_high_straight() -> None:
    wheel = rank_five(parse_cards("As 2h 3d 4c 5s"))
    six_high = rank_five(parse_cards("6s 5h 4d 3c 2s"))
    assert wheel < six_high


def test_wheel_ranks_above_king_high_no_pair() -> None:
    wheel = rank_five(parse_cards("As 2h 3d 4c 5s"))
    king_high = rank_five(parse_cards("Ks Qh Jd 9c 7s"))
    assert wheel > king_high


def test_wheel_straight_flush_ranks_below_six_high_straight_flush() -> None:
    wheel_flush = rank_five(parse_cards("Ah 2h 3h 4h 5h"))
    six_high_flush = rank_five(parse_cards("6c 5c 4c 3c 2c"))
    assert wheel_flush.category is Category.STRAIGHT_FLUSH
    assert six_high_flush.category is Category.STRAIGHT_FLUSH
    assert wheel_flush < six_high_flush


def test_category_order_straight_beats_two_pair_and_trips() -> None:
    straight = rank_five(parse_cards("6s 5h 4d 3c 2s"))
    two_pair = rank_five(parse_cards("As Ah Ks Kh 7s"))
    trips = rank_five(parse_cards("As Ah Ad Ks 7s"))
    assert straight > two_pair
    assert straight > trips


def test_flush_beats_straight() -> None:
    flush = rank_five(parse_cards("Qh Jh 9h 5h 2h"))
    straight = rank_five(parse_cards("9s 8h 7d 6c 5s"))
    assert flush > straight


def test_straight_flush_beats_quads() -> None:
    straight_flush = rank_five(parse_cards("9h 8h 7h 6h 5h"))
    quads = rank_five(parse_cards("As Ah Ad Ac Ks"))
    assert straight_flush > quads


def test_full_house_beats_flush() -> None:
    full_house = rank_five(parse_cards("Ks Kh Kd 7c 7d"))
    flush = rank_five(parse_cards("Qh Jh 9h 5h 2h"))
    assert full_house > flush


def test_kicker_tie_breaks_pair() -> None:
    higher = rank_five(parse_cards("As Ah Kd 7c 5s"))
    lower = rank_five(parse_cards("As Ah Qd 7c 5s"))
    assert higher > lower


def test_equal_hands_in_different_suits_compare_equal() -> None:
    first = rank_five(parse_cards("As Ah Kd 7c 5s"))
    second = rank_five(parse_cards("Ad Ac Kh 7s 5d"))
    assert first == second


def test_hand_rank_is_orderable() -> None:
    assert isinstance(rank_five(parse_cards("As Ah Kd 7c 5s")), HandRank)
    ranks = sorted(
        [
            rank_five(parse_cards("9h 8h 7h 6h 5h")),
            rank_five(parse_cards("2s 3h 4d 5c 7s")),
        ]
    )
    assert ranks[0].category is Category.HIGH_CARD


def test_best_hand_is_not_orderable() -> None:
    hand = best_hand(parse_cards("As Ah Kd 7c 5s"))
    assert isinstance(hand, BestHand)
    with pytest.raises(TypeError):
        hand < hand  # type: ignore[operator]  # noqa: B015


def test_best_hand_exactly_five_cards_plays_them_all() -> None:
    cards = parse_cards("As Ah Kd 7c 5s")
    hand = best_hand(cards)
    assert set(hand.cards) == set(cards)


def test_best_hand_picks_best_of_many() -> None:
    pool = parse_cards("As Ks Qs Js 10s 2h 3h 4h 5h 6h 7d 8d 9d Tc Jc Qc Kc Ac 2c 3c 4c")
    assert len(pool) == 21
    hand = best_hand(pool)
    assert hand.rank.category is Category.STRAIGHT_FLUSH
    assert hand.rank.tiebreak == (Rank.ACE,)
    assert hand.description == "royal flush"


def test_best_hand_for_board_four_hearts_two_on_board_no_flush() -> None:
    hand = best_hand_for_board(parse_cards("Ah Kh Qh Jh 2c"), parse_cards("9h 8h 7s 2s 3c"))
    assert hand.rank.category is Category.PAIR
    assert hand.description == "pair of twos"


def test_best_hand_for_board_one_heart_four_on_board_no_flush() -> None:
    hand = best_hand_for_board(parse_cards("Ah 2c 3d 4s 5c"), parse_cards("Kh Qh Jh 9h 2s"))
    assert hand.rank.category is Category.PAIR
    assert hand.description == "pair of twos"


def test_best_hand_for_board_three_and_three_is_a_legal_flush() -> None:
    # The ticket's own wording ("four hearts with a three-heart board cannot make a
    # flush") is mistaken: the Omaha rule is "exactly 2 hole + exactly 3 board", not
    # "no flushes are possible at all". 3 hearts in hand + 3 on board is a legal flush.
    hand = best_hand_for_board(parse_cards("Ah Kh Qh 2c 3d"), parse_cards("9h 8h 7h 2s 3c"))
    assert hand.rank.category is Category.FLUSH
    assert hand.description == "flush, ace high"


def test_best_hand_for_board_cannot_play_board_alone() -> None:
    board = parse_cards("Ah Kh Qh Jh 10h")
    hand = best_hand_for_board(parse_cards("2c 3d 4s 5c 6d"), board)
    assert hand.rank.category is Category.HIGH_CARD
    assert hand.description == "ace high"


def test_best_hand_for_board_quad_aces_on_board_only_three_playable() -> None:
    board = parse_cards("As Ah Ad Ac Kh")
    hand = best_hand_for_board(parse_cards("2c 3d 4s 5c"), board)
    assert hand.rank.category is Category.THREE_OF_A_KIND
    assert hand.rank.tiebreak[0] is Rank.ACE


def test_best_hand_for_board_composes_with_a_real_deal() -> None:
    deal = deal_cards(seed=b"\x55" * fairness.SEED_SIZE, player_ids=[1, 2, 3, 4], x=5, y=2)
    for seat_hand in deal.hands:
        assert len(seat_hand.cards) == 7  # 5 hole + 2 extra: exercises the concatenation.
        own_cards = set(seat_hand.cards)
        for board in (deal.board1, deal.board2):
            hand = best_hand_for_board(seat_hand.cards, board)
            assert len(hand.cards) == HAND_SIZE
            assert len(set(hand.cards)) == HAND_SIZE
            from_hand = [card for card in hand.cards if card in own_cards]
            from_board = [card for card in hand.cards if card in set(board)]
            assert len(from_hand) == HOLE_CARDS_PLAYED
            assert len(from_board) == BOARD_CARDS_PLAYED

        strength_hand = best_hand(seat_hand.cards)
        assert set(strength_hand.cards) <= own_cards


def test_rank_five_rejects_wrong_card_count() -> None:
    with pytest.raises(ValueError, match="5"):
        rank_five(parse_cards("As Ah Kd 7c"))


def test_rank_five_rejects_repeated_card() -> None:
    with pytest.raises(ValueError, match="twice"):
        rank_five(parse_cards("As As Kd 7c 5s"))


def test_best_hand_rejects_too_few_cards() -> None:
    with pytest.raises(ValueError, match="at least"):
        best_hand(parse_cards("As Ah Kd 7c"))


def test_best_hand_rejects_repeated_card() -> None:
    with pytest.raises(ValueError, match="twice"):
        best_hand(parse_cards("As As Kd 7c 5s"))


def test_best_hand_for_board_rejects_wrong_board_size() -> None:
    with pytest.raises(ValueError, match="5"):
        best_hand_for_board(parse_cards("As Ah Kd 7c"), parse_cards("2c 3d 4s 5c"))


def test_best_hand_for_board_rejects_too_few_hole_cards() -> None:
    with pytest.raises(ValueError, match="2"):
        best_hand_for_board(parse_cards("As"), parse_cards("2c 3d 4s 5c 6d"))


def test_best_hand_for_board_rejects_repeated_card_within_hand() -> None:
    with pytest.raises(ValueError, match="twice"):
        best_hand_for_board(parse_cards("As As Kd 7c"), parse_cards("2c 3d 4s 5c 6d"))


def test_best_hand_for_board_rejects_repeated_card_across_hand_and_board() -> None:
    with pytest.raises(ValueError, match="twice"):
        best_hand_for_board(parse_cards("As Ah Kd 7c"), parse_cards("As 3d 4s 5c 6d"))


def test_names_importable_from_game_package() -> None:
    from esgbot import game

    assert game.rank_five is rank_five
    assert game.best_hand is best_hand
    assert game.best_hand_for_board is best_hand_for_board
    assert game.Category is Category
    assert game.HandRank is HandRank
    assert game.BestHand is BestHand


def test_constants() -> None:
    assert HAND_SIZE == 5
    assert HOLE_CARDS_PLAYED == 2
    assert BOARD_CARDS_PLAYED == 3


def test_timing_for_twenty_one_cards() -> None:
    rng = random.Random(2024)
    pool = tuple(rng.sample(FULL_DECK, 21))
    board1 = tuple(rng.sample([card for card in FULL_DECK if card not in pool], 5))
    remaining = [card for card in FULL_DECK if card not in pool and card not in board1]
    board2 = tuple(rng.sample(remaining, 5))

    start = time.perf_counter()
    best_hand(pool)
    best_hand_for_board(pool, board1)
    best_hand_for_board(pool, board2)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5


def _resuit(cards: tuple[Card, ...], permutation: tuple[Suit, ...]) -> tuple[Card, ...]:
    """Relabel every card's suit by the same whole-suit permutation.

    A consistent relabelling preserves category and every tiebreak (a flush stays a
    flush, a straight flush stays a straight flush), so the result must rank equal.
    """
    remap = dict(zip(list(Suit), permutation, strict=True))
    return tuple(Card(card.rank, remap[card.suit]) for card in cards)


@pytest.mark.parametrize("trial", range(50))
def test_rank_five_matches_treys_over_random_hands(trial: int) -> None:
    rng = random.Random(1000 + trial)
    suit_permutations = list(itertools.permutations(Suit))
    for _ in range(200):
        hand = tuple(rng.sample(FULL_DECK, HAND_SIZE))
        ours = rank_five(hand)
        theirs = _treys_score(hand)
        _assert_consistent_with_treys(ours, theirs, hand, rng)
        _assert_equal_under_resuiting(ours, theirs, hand, rng, suit_permutations)


def _assert_consistent_with_treys(
    ours: HandRank, theirs: int, hand: tuple[Card, ...], rng: random.Random
) -> None:
    other_hand = tuple(rng.sample([c for c in FULL_DECK if c not in hand], HAND_SIZE))
    other_ours = rank_five(other_hand)
    other_theirs = _treys_score(other_hand)

    our_sign = (ours > other_ours) - (ours < other_ours)
    # treys: lower score wins, so flip the sign to compare in the same direction.
    their_sign = (theirs < other_theirs) - (theirs > other_theirs)
    assert our_sign == their_sign

    if theirs == other_theirs:
        assert ours == other_ours


def _assert_equal_under_resuiting(
    ours: HandRank,
    theirs: int,
    hand: tuple[Card, ...],
    rng: random.Random,
    suit_permutations: list[tuple[Suit, ...]],
) -> None:
    """A whole-suit relabelling of the same hand must rank equal in both evaluators."""
    permutation = rng.choice(suit_permutations)
    resuited = _resuit(hand, permutation)
    resuited_ours = rank_five(resuited)
    resuited_theirs = _treys_score(resuited)
    assert ours == resuited_ours
    assert theirs == resuited_theirs


def test_best_hand_matches_treys_over_random_pools() -> None:
    rng = random.Random(42)
    for pool_size in range(6, 10):
        for _ in range(30):
            pool = tuple(rng.sample(FULL_DECK, pool_size))
            ours = best_hand(pool)
            best_treys_score = min(
                _treys_score(combo) for combo in itertools.combinations(pool, HAND_SIZE)
            )
            assert _treys_score(ours.cards) == best_treys_score

"""Hand ranking: what five cards are worth, and the best five a player can make.

Everything here is pure and synchronous, because the engine runs the showdown in a
worker thread.

What a five-card hand is worth is a :class:`HandRank`: a :class:`Category` plus the
tiebreak ranks that separate two hands inside that category. Ranks compare directly,
so ``rank_five(a) > rank_five(b)`` says which hand wins and two hands that tie
compare equal. Suits never break a tie.

A-2-3-4-5 is a straight with the ace low: it ranks as a five-high straight, below
2-3-4-5-6, and the same goes for the five-high straight flush.

Two searches find a player's best hand, one for each kind of board ESG scores:

* :func:`best_hand` -- the hand-strength board. The best five of the player's own
  cards, nothing else.
* :func:`best_hand_for_board` -- Board 1 and Board 2. Exactly two of the player's
  cards and exactly three of the board's five, which is the Omaha rule. So a player
  can never play the board alone, and four cards of one suit in hand with three of
  that suit on the board is not a flush.

A player holds 5 to 21 cards. The worst case, 21 cards, checks C(21,5) = 20,349
five-card hands for the hand-strength board and C(21,2) x C(5,3) = 2,100 for each
community board: tens of milliseconds in all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from itertools import combinations
from typing import Final

from esgbot.game.cards import Card, Rank, format_cards
from esgbot.game.rules import BOARD_SIZE

__all__ = [
    "BOARD_CARDS_PLAYED",
    "HAND_SIZE",
    "HOLE_CARDS_PLAYED",
    "BestHand",
    "Category",
    "HandRank",
    "best_hand",
    "best_hand_for_board",
    "rank_five",
]

HAND_SIZE: Final = 5
"""Cards in a poker hand."""

HOLE_CARDS_PLAYED: Final = 2
"""Cards a player must take from their own hand to play a community board."""

BOARD_CARDS_PLAYED: Final = 3
"""Cards a player must take from the board to play a community board."""


class Category(IntEnum):
    """Hand category, numbered so that a stronger category is a larger number."""

    HIGH_CARD = 0
    PAIR = 1
    TWO_PAIR = 2
    THREE_OF_A_KIND = 3
    STRAIGHT = 4
    FLUSH = 5
    FULL_HOUSE = 6
    FOUR_OF_A_KIND = 7
    STRAIGHT_FLUSH = 8


@dataclass(frozen=True, slots=True, order=True)
class HandRank:
    """What a five-card hand is worth, and all that decides who wins.

    ``tiebreak`` holds the ranks that separate two hands of the same category, most
    significant first, so comparing two ranks compares the category and then the
    tiebreaks in order. Within one category every tiebreak has the same length:

    ===================  =======================================================
    Category             ``tiebreak``
    ===================  =======================================================
    high card            all five ranks, highest first
    pair                 the pair, then the three kickers, highest first
    two pair             the higher pair, the lower pair, the kicker
    three of a kind      the trips, then the two kickers, highest first
    straight             the high card alone; ``FIVE`` for A-2-3-4-5
    flush                all five ranks, highest first
    full house           the trips, then the pair
    four of a kind       the quads, then the kicker
    straight flush       the high card alone; ``FIVE`` for A-2-3-4-5
    ===================  =======================================================
    """

    category: Category
    tiebreak: tuple[Rank, ...]

    @property
    def description(self) -> str:
        """Short readable text for the showdown, for example ``kings full of sevens``."""
        return _describe(self.category, self.tiebreak)


@dataclass(frozen=True, slots=True)
class BestHand:
    """The best five cards a search found, and what they are worth.

    Compare two of these by :attr:`rank` only. :attr:`cards` is for display, and
    suits never break a tie, so a :class:`BestHand` is deliberately not orderable.
    """

    rank: HandRank
    cards: tuple[Card, ...]

    @property
    def description(self) -> str:
        """Short readable text for the showdown, for example ``kings full of sevens``."""
        return self.rank.description


_RankKey = tuple[Category, tuple[Rank, ...]]

_CATEGORY_BY_SHAPE: Final[dict[tuple[int, ...], Category]] = {
    (4, 1): Category.FOUR_OF_A_KIND,
    (3, 2): Category.FULL_HOUSE,
    (3, 1, 1): Category.THREE_OF_A_KIND,
    (2, 2, 1): Category.TWO_PAIR,
    (2, 1, 1, 1): Category.PAIR,
}

_WHEEL: Final[tuple[Rank, ...]] = (Rank.ACE, Rank.FIVE, Rank.FOUR, Rank.THREE, Rank.TWO)
"""The five-high straight's ranks, highest first, as the tiebreak order produces them."""


def _rank_key(cards: Sequence[Card]) -> _RankKey:
    """Rank exactly five cards, without checking them. The hot path of every search."""
    counts: dict[Rank, int] = {}
    for card in cards:
        rank = card.rank
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(counts.items(), key=lambda group: (group[1], group[0]), reverse=True)
    tiebreak = tuple(rank for rank, _ in groups)
    if len(groups) < HAND_SIZE:
        shape = tuple(count for _, count in groups)
        return (_CATEGORY_BY_SHAPE[shape], tiebreak)

    # Five distinct ranks: a straight, a flush, both, or nothing.
    flush = len({card.suit for card in cards}) == 1
    if tiebreak == _WHEEL:
        high = (Rank.FIVE,)
    elif tiebreak[0] - tiebreak[-1] == HAND_SIZE - 1:
        high = (tiebreak[0],)
    else:
        return (Category.FLUSH if flush else Category.HIGH_CARD, tiebreak)
    return (Category.STRAIGHT_FLUSH if flush else Category.STRAIGHT, high)


def _straight_order(high: Rank) -> tuple[Rank, ...]:
    """The ranks of a straight, highest first. The wheel's ace comes last, below the two."""
    if high is Rank.FIVE:
        return (Rank.FIVE, Rank.FOUR, Rank.THREE, Rank.TWO, Rank.ACE)
    return tuple(Rank(value) for value in range(high, high - HAND_SIZE, -1))


def _best_hand(cards: Sequence[Card], key: _RankKey) -> BestHand:
    """Pair a ranked five-card hand with its cards in reading order."""
    category, tiebreak = key
    if category is Category.STRAIGHT or category is Category.STRAIGHT_FLUSH:
        order = _straight_order(tiebreak[0])
    else:
        order = tiebreak
    position = {rank: place for place, rank in enumerate(order)}
    ordered = sorted(cards, key=lambda card: (position[card.rank], card.index))
    return BestHand(rank=HandRank(category=category, tiebreak=tiebreak), cards=tuple(ordered))


def rank_five(cards: Sequence[Card]) -> HandRank:
    """What these five cards are worth.

    Args:
        cards: exactly five distinct cards, in any order.

    Returns:
        The rank, which compares directly against any other rank.

    Raises:
        ValueError: if there are not exactly five cards, or a card repeats.
    """
    if len(cards) != HAND_SIZE:
        raise ValueError(f"a hand is {HAND_SIZE} cards, got {len(cards)}")
    _reject_repeats(cards)
    category, tiebreak = _rank_key(cards)
    return HandRank(category=category, tiebreak=tiebreak)


def best_hand(cards: Sequence[Card]) -> BestHand:
    """The best five-card hand from all of a player's cards: the hand-strength board.

    Args:
        cards: the player's cards, 5 to 21 of them, all distinct.

    Returns:
        The best hand, with the five cards it chose in reading order.

    Raises:
        ValueError: if there are fewer than five cards, or a card repeats.
    """
    if len(cards) < HAND_SIZE:
        raise ValueError(f"a player needs at least {HAND_SIZE} cards, got {len(cards)}")
    _reject_repeats(cards)
    if len(cards) == HAND_SIZE:
        chosen: tuple[Card, ...] = tuple(cards)
    else:
        chosen = max(combinations(cards, HAND_SIZE), key=_rank_key)
    return _best_hand(chosen, _rank_key(chosen))


def best_hand_for_board(cards: Sequence[Card], board: Sequence[Card]) -> BestHand:
    """The best five-card hand on a community board: exactly two in hand plus three of five.

    This is the Omaha rule, and it is exact. A player cannot play fewer or more than
    two of their own cards, so the board can never be played alone and four cards of
    one suit in hand with three on the board is not a flush.

    Args:
        cards: the player's cards, at least two of them.
        board: the board's five cards.

    Returns:
        The best hand, with the five cards it chose in reading order.

    Raises:
        ValueError: if the board is not five cards, the player holds fewer than two,
            or any card repeats within or across the two.
    """
    if len(board) != BOARD_SIZE:
        raise ValueError(f"a board is {BOARD_SIZE} cards, got {len(board)}")
    if len(cards) < HOLE_CARDS_PLAYED:
        raise ValueError(
            f"a community board needs {HOLE_CARDS_PLAYED} of the player's cards, "
            f"and the player holds {len(cards)}"
        )
    _reject_repeats((*cards, *board))
    chosen = max(
        (
            hole + played
            for hole in combinations(cards, HOLE_CARDS_PLAYED)
            for played in combinations(board, BOARD_CARDS_PLAYED)
        ),
        key=_rank_key,
    )
    return _best_hand(chosen, _rank_key(chosen))


def _reject_repeats(cards: Sequence[Card]) -> None:
    """Raise if the same card appears twice. A real deal never repeats one."""
    if len(set(cards)) != len(cards):
        raise ValueError(f"the same card appears twice: {format_cards(cards)}")


_RANK_WORDS: Final[dict[Rank, str]] = {
    Rank.TWO: "two",
    Rank.THREE: "three",
    Rank.FOUR: "four",
    Rank.FIVE: "five",
    Rank.SIX: "six",
    Rank.SEVEN: "seven",
    Rank.EIGHT: "eight",
    Rank.NINE: "nine",
    Rank.TEN: "ten",
    Rank.JACK: "jack",
    Rank.QUEEN: "queen",
    Rank.KING: "king",
    Rank.ACE: "ace",
}

_RANK_PLURALS: Final[dict[Rank, str]] = {
    **{rank: f"{word}s" for rank, word in _RANK_WORDS.items()},
    Rank.SIX: "sixes",
}


def _describe(category: Category, tiebreak: tuple[Rank, ...]) -> str:
    """The showdown text for a rank. See :attr:`HandRank.description`."""
    word = _RANK_WORDS[tiebreak[0]]
    plural = _RANK_PLURALS[tiebreak[0]]
    if category is Category.STRAIGHT_FLUSH:
        if tiebreak[0] is Rank.ACE:
            return "royal flush"
        return f"straight flush, {word} high"
    if category is Category.FOUR_OF_A_KIND:
        return f"four of a kind, {plural}"
    if category is Category.FULL_HOUSE:
        return f"full house, {plural} full of {_RANK_PLURALS[tiebreak[1]]}"
    if category is Category.FLUSH:
        return f"flush, {word} high"
    if category is Category.STRAIGHT:
        return f"straight, {word} high"
    if category is Category.THREE_OF_A_KIND:
        return f"three of a kind, {plural}"
    if category is Category.TWO_PAIR:
        return f"two pair, {plural} and {_RANK_PLURALS[tiebreak[1]]}"
    if category is Category.PAIR:
        return f"pair of {plural}"
    return f"{word} high"

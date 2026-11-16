"""The card model: ranks, suits, and the stable 0..51 card index.

The index is *suit-major* and is part of the published fairness procedure
(``docs/fairness-spec.md``), so it must never change::

    index = suit * 13 + (rank - 2)

with suits ordered clubs, diamonds, hearts, spades (0..3) and ranks ascending
from two (2) to ace (14). Index 0 is ``2♣``, index 12 is ``A♣``, index 51 is ``A♠``.

A :class:`Card` is immutable, hashable and ordered by that index. Cards are
written as rank text plus a suit, either as a symbol (``A♠``, ``10♥``) or as a
letter (``As``, ``10h``, ``Th``); :meth:`Card.parse` reads every one of those.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Self

__all__ = [
    "DECK_SIZE",
    "FULL_DECK",
    "RANKS_PER_SUIT",
    "Card",
    "Rank",
    "Suit",
    "card_indexes",
    "cards_from_indexes",
    "format_cards",
    "parse_cards",
]

DECK_SIZE: Final = 52
"""Cards in one deck. No jokers."""

RANKS_PER_SUIT: Final = 13


class Suit(IntEnum):
    """Suit, numbered in the order used by the card index."""

    CLUBS = 0
    DIAMONDS = 1
    HEARTS = 2
    SPADES = 3

    @property
    def symbol(self) -> str:
        """The suit symbol used in display text, for example ``♠``."""
        return _SUIT_SYMBOLS[self]

    @property
    def letter(self) -> str:
        """The lowercase ASCII suit letter, for example ``s``."""
        return _SUIT_LETTERS[self]


class Rank(IntEnum):
    """Rank. The value is the poker value, so the ace is high at 14."""

    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6
    SEVEN = 7
    EIGHT = 8
    NINE = 9
    TEN = 10
    JACK = 11
    QUEEN = 12
    KING = 13
    ACE = 14

    @property
    def label(self) -> str:
        """The rank as it is written on a card: ``2``..``10``, ``J``, ``Q``, ``K``, ``A``."""
        return _RANK_LABELS[self]


_SUIT_SYMBOLS: Final[dict[Suit, str]] = {
    Suit.CLUBS: "♣",
    Suit.DIAMONDS: "♦",
    Suit.HEARTS: "♥",
    Suit.SPADES: "♠",
}

_SUIT_LETTERS: Final[dict[Suit, str]] = {
    Suit.CLUBS: "c",
    Suit.DIAMONDS: "d",
    Suit.HEARTS: "h",
    Suit.SPADES: "s",
}

_RANK_LABELS: Final[dict[Rank, str]] = {
    Rank.TWO: "2",
    Rank.THREE: "3",
    Rank.FOUR: "4",
    Rank.FIVE: "5",
    Rank.SIX: "6",
    Rank.SEVEN: "7",
    Rank.EIGHT: "8",
    Rank.NINE: "9",
    Rank.TEN: "10",
    Rank.JACK: "J",
    Rank.QUEEN: "Q",
    Rank.KING: "K",
    Rank.ACE: "A",
}

_SUITS_BY_TEXT: Final[dict[str, Suit]] = {
    **{symbol: suit for suit, symbol in _SUIT_SYMBOLS.items()},
    **{letter: suit for suit, letter in _SUIT_LETTERS.items()},
}

_RANKS_BY_TEXT: Final[dict[str, Rank]] = {
    **{label: rank for rank, label in _RANK_LABELS.items()},
    "T": Rank.TEN,
}


@dataclass(frozen=True, slots=True, repr=False)
class Card:
    """One playing card.

    Equality and hashing come from the rank and suit, which the index encodes
    one-to-one, so ``Card.from_index(c.index) == c`` for every card.
    """

    rank: Rank
    suit: Suit

    @property
    def index(self) -> int:
        """This card's stable index, 0..51. See the module docstring."""
        return self.suit * RANKS_PER_SUIT + (self.rank - Rank.TWO)

    @property
    def code(self) -> str:
        """ASCII form: rank label plus suit letter, for example ``As`` or ``10h``."""
        return f"{self.rank.label}{self.suit.letter}"

    @classmethod
    def from_index(cls, index: int) -> Self:
        """Build the card with this index.

        Raises:
            ValueError: if ``index`` is outside 0..51.
        """
        if not 0 <= index < DECK_SIZE:
            raise ValueError(f"card index must be 0..{DECK_SIZE - 1}, got {index}")
        suit, offset = divmod(index, RANKS_PER_SUIT)
        return cls(Rank(offset + Rank.TWO), Suit(suit))

    @classmethod
    def parse(cls, text: str) -> Self:
        """Read a card from text such as ``A♠``, ``As``, ``10♥``, ``10h`` or ``Th``.

        Rank letters are case-insensitive. Raises :class:`ValueError` on anything else.
        """
        cleaned = text.strip()
        if len(cleaned) < 2:
            raise ValueError(f"not a card: {text!r}")
        rank_text, suit_text = cleaned[:-1], cleaned[-1]
        rank = _RANKS_BY_TEXT.get(rank_text.upper())
        suit = _SUITS_BY_TEXT.get(suit_text.lower())
        if rank is None or suit is None:
            raise ValueError(f"not a card: {text!r}")
        return cls(rank, suit)

    def __str__(self) -> str:
        """Display text, for example ``A♠`` or ``10♥``."""
        return f"{self.rank.label}{self.suit.symbol}"

    def __repr__(self) -> str:
        return f"Card({str(self)!r})"

    def __lt__(self, other: Card) -> bool:
        return self.index < other.index

    def __le__(self, other: Card) -> bool:
        return self.index <= other.index

    def __gt__(self, other: Card) -> bool:
        return self.index > other.index

    def __ge__(self, other: Card) -> bool:
        return self.index >= other.index


FULL_DECK: Final[tuple[Card, ...]] = tuple(Card.from_index(i) for i in range(DECK_SIZE))
"""All 52 cards in index order, so ``FULL_DECK[i].index == i``."""


def cards_from_indexes(indexes: Iterable[int]) -> tuple[Card, ...]:
    """Turn card indexes into cards. Raises :class:`ValueError` on a bad index."""
    return tuple(Card.from_index(index) for index in indexes)


def card_indexes(cards: Iterable[Card]) -> list[int]:
    """The indexes of these cards, in the order given. The JSON form of a hand."""
    return [card.index for card in cards]


def parse_cards(text: str) -> tuple[Card, ...]:
    """Read whitespace-separated cards, for example ``"A♠ 10♥ 2c"``. Empty text gives ``()``."""
    return tuple(Card.parse(token) for token in text.split())


def format_cards(cards: Sequence[Card]) -> str:
    """Display text for a run of cards, space separated, for example ``"A♠ 10♥"``."""
    return " ".join(str(card) for card in cards)

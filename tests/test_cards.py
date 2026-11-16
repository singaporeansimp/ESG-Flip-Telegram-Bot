from __future__ import annotations

import pytest

from esgbot.game.cards import (
    DECK_SIZE,
    FULL_DECK,
    Card,
    Rank,
    Suit,
    card_indexes,
    cards_from_indexes,
    format_cards,
    parse_cards,
)

_ANCHORS: tuple[tuple[int, Rank, Suit], ...] = (
    (0, Rank.TWO, Suit.CLUBS),
    (12, Rank.ACE, Suit.CLUBS),
    (13, Rank.TWO, Suit.DIAMONDS),
    (39, Rank.TWO, Suit.SPADES),
    (51, Rank.ACE, Suit.SPADES),
)


@pytest.mark.parametrize("index", range(DECK_SIZE))
def test_index_round_trip(index: int) -> None:
    card = Card.from_index(index)
    assert card.index == index
    assert FULL_DECK[index] == card


@pytest.mark.parametrize(("index", "rank", "suit"), _ANCHORS)
def test_documented_anchors(index: int, rank: Rank, suit: Suit) -> None:
    card = Card.from_index(index)
    assert card.rank is rank
    assert card.suit is suit


@pytest.mark.parametrize(
    ("index", "text"),
    [(0, "2♣"), (12, "A♣"), (13, "2♦"), (39, "2♠"), (51, "A♠"), (34, "10♥")],
)
def test_display_text(index: int, text: str) -> None:
    assert str(Card.from_index(index)) == text


@pytest.mark.parametrize("index", [-1, 52, 100, -100])
def test_from_index_rejects_out_of_range(index: int) -> None:
    with pytest.raises(ValueError, match=r"0\.\.51"):
        Card.from_index(index)


def _all_accepted_forms() -> list[tuple[str, Card]]:
    forms: list[tuple[str, Card]] = []
    for card in FULL_DECK:
        symbol = card.suit.symbol
        letter = card.suit.letter
        rank_label = card.rank.label
        forms.append((f"{rank_label}{symbol}", card))
        forms.append((f"{rank_label}{letter}", card))
        forms.append((f"{rank_label}{letter.upper()}", card))
        if card.rank is Rank.TEN:
            forms.append((f"T{letter}", card))
            forms.append((f"t{letter}", card))
    for card in FULL_DECK:
        forms.append((f"{card.rank.label.lower()}{card.suit.letter}", card))
    return forms


@pytest.mark.parametrize(("text", "expected"), _all_accepted_forms())
def test_parse_accepts_every_documented_form(text: str, expected: Card) -> None:
    assert Card.parse(text) == expected


@pytest.mark.parametrize("text", ["", "2", "Z♠", "1♠", "A", "AX", "Ax ", "1 0h", "10", "AZ"])
def test_parse_rejects_junk_text(text: str) -> None:
    with pytest.raises(ValueError, match="not a card"):
        Card.parse(text)


def test_card_code_round_trip() -> None:
    for card in FULL_DECK:
        assert Card.parse(card.code) == card


def test_card_str_round_trip() -> None:
    for card in FULL_DECK:
        assert Card.parse(str(card)) == card


def test_ordering_matches_index() -> None:
    assert sorted(FULL_DECK, reverse=True) == list(reversed(FULL_DECK))
    assert FULL_DECK[0] < FULL_DECK[1]
    assert FULL_DECK[51] > FULL_DECK[50]
    assert FULL_DECK[0] <= FULL_DECK[0]
    assert FULL_DECK[0] >= FULL_DECK[0]


def test_cards_from_indexes_and_card_indexes_round_trip() -> None:
    indexes = [5, 10, 51, 0]
    cards = cards_from_indexes(indexes)
    expected = (Card.from_index(5), Card.from_index(10), Card.from_index(51), Card.from_index(0))
    assert cards == expected
    assert card_indexes(cards) == indexes


def test_cards_from_indexes_rejects_bad_index() -> None:
    with pytest.raises(ValueError, match=r"0\.\.51"):
        cards_from_indexes([0, 52])


def test_parse_cards_and_format_cards_round_trip() -> None:
    text = "A♠ 10♥ 2c"
    cards = parse_cards(text)
    assert cards == (Card.parse("A♠"), Card.parse("10♥"), Card.parse("2c"))
    assert format_cards(cards) == "A♠ 10♥ 2♣"


def test_parse_cards_empty_text_gives_empty_tuple() -> None:
    assert parse_cards("") == ()
    assert parse_cards("   ") == ()


def test_full_deck_has_52_unique_cards() -> None:
    assert len(FULL_DECK) == DECK_SIZE
    assert len(set(FULL_DECK)) == DECK_SIZE

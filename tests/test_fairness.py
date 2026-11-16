from __future__ import annotations

import pytest

from esgbot.game.cards import DECK_SIZE, FULL_DECK
from esgbot.game.fairness import (
    SEED_SIZE,
    button_seat,
    card_sort_key,
    commitment,
    deck_order,
    generate_seed,
    seed_from_hex,
    seed_to_hex,
    validate_seed,
)
from tests.fairness_vectors import FairnessVector, load_vectors

VECTORS = load_vectors()


def test_at_least_three_vectors() -> None:
    assert len(VECTORS) >= 3


def test_generate_seed_length_and_variability() -> None:
    first = generate_seed()
    second = generate_seed()
    assert len(first) == SEED_SIZE == 32
    assert first != second


@pytest.mark.parametrize("length", [0, 1, 31, 33, 64])
def test_validate_seed_rejects_wrong_length(length: int) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        validate_seed(b"\x00" * length)


def test_validate_seed_returns_seed_unchanged() -> None:
    seed = b"\x01" * SEED_SIZE
    assert validate_seed(seed) is seed


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"vector_{v.number}")
def test_seed_hex_round_trip(vector: FairnessVector) -> None:
    hex_text = seed_to_hex(vector.seed)
    assert hex_text == vector.seed.hex()
    assert seed_from_hex(hex_text) == vector.seed
    assert seed_from_hex(hex_text.upper()) == vector.seed


def test_seed_from_hex_rejects_non_hex_text() -> None:
    with pytest.raises(ValueError, match="64 hex digits"):
        seed_from_hex("not hex")


def test_seed_from_hex_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        seed_from_hex("00" * 31)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"vector_{v.number}")
def test_commitment_matches_vector(vector: FairnessVector) -> None:
    assert commitment(vector.seed) == vector.commitment


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"vector_{v.number}")
def test_deck_order_first_ten_matches_vector(vector: FairnessVector) -> None:
    order = deck_order(vector.seed)
    assert order[:10] == vector.deck_first_ten


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"vector_{v.number}")
def test_deck_order_is_a_permutation(vector: FairnessVector) -> None:
    order = deck_order(vector.seed)
    assert len(order) == DECK_SIZE
    assert sorted(card.index for card in order) == list(range(DECK_SIZE))
    assert set(order) == set(FULL_DECK)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"vector_{v.number}")
def test_button_seat_matches_vector(vector: FairnessVector) -> None:
    assert button_seat(vector.seed, 2) == vector.button_2
    assert button_seat(vector.seed, 5) == vector.button_5


def test_button_seat_rejects_bad_player_count() -> None:
    seed = b"\x00" * SEED_SIZE
    with pytest.raises(ValueError, match="at least 1"):
        button_seat(seed, 0)


def test_card_sort_key_rejects_bad_index() -> None:
    seed = b"\x00" * SEED_SIZE
    with pytest.raises(ValueError, match=r"0\.\.51"):
        card_sort_key(seed, 52)


def test_determinism_deck_order_and_button_are_stable() -> None:
    seed = b"\x42" * SEED_SIZE
    assert deck_order(seed) == deck_order(seed)
    assert button_seat(seed, 7) == button_seat(seed, 7)


def test_intermediate_values_match_spec_table() -> None:
    vector1 = next(v for v in VECTORS if v.number == 1)
    seed = vector1.seed
    assert seed == b"\x00" * SEED_SIZE

    key_0 = "7f9c9e31ac8256ca2f258583df262dbc7d6f68f2a03043d5c99a4ae5a7396ce9"
    key_1 = "1fd4247443c9440cb3c48c28851937196bc156032d70a96c98e127ecb347e45f"
    key_51 = "154a2797f271dcb9dddce221f5ec00865387ac25d721045987cc5cb693bef22e"

    assert card_sort_key(seed, 0).hex() == key_0
    assert card_sort_key(seed, 1).hex() == key_1
    assert card_sort_key(seed, 51).hex() == key_51

    smallest_key = "086fb60bd968fe68ecec6a8d826ea5aa7d3d8020e644d7c5d0e07ded456ca3e8"
    assert card_sort_key(seed, 5).hex() == smallest_key
    assert deck_order(seed)[0].index == 5

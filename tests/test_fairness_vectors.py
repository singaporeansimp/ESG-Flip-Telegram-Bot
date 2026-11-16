from __future__ import annotations

from esgbot.game.fairness import SEED_SIZE
from tests.fairness_vectors import load_vectors

VECTORS = load_vectors()


def test_load_vectors_finds_at_least_three() -> None:
    assert len(VECTORS) >= 3


def test_vector_numbers_are_sequential_from_one() -> None:
    numbers = [vector.number for vector in VECTORS]
    assert numbers == sorted(numbers)
    assert numbers[0] == 1


def test_every_vector_has_a_well_formed_seed_and_commitment() -> None:
    for vector in VECTORS:
        assert len(vector.seed) == SEED_SIZE
        assert len(vector.commitment) == 64
        assert all(char in "0123456789abcdef" for char in vector.commitment)


def test_every_vector_deal_is_internally_consistent() -> None:
    for vector in VECTORS:
        assert len(vector.players) == vector.player_count
        assert len(vector.hands) == vector.player_count
        assert 0 <= vector.button_seat < vector.player_count
        assert len(vector.board1) == 5
        assert len(vector.board2) == 5
        for hand in vector.hands:
            assert len(hand.hole) == vector.x
            assert len(hand.extra) <= vector.y

from __future__ import annotations

import random
from typing import Any

import pytest

from esgbot.game import fairness, rules
from esgbot.game.cards import Card
from esgbot.game.deal import Deal, PlayerHand, deal_cards, extra_rounds_for, seat_order
from tests.fairness_vectors import FairnessVector, load_vectors

VECTORS = load_vectors()


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"vector_{v.number}")
def test_deal_matches_vector(vector: FairnessVector) -> None:
    deal = deal_cards(seed=vector.seed, player_ids=list(vector.players), x=vector.x, y=vector.y)
    assert deal.button_seat == vector.button_seat
    assert deal.board1 == vector.board1
    assert deal.board2 == vector.board2
    assert deal.player_ids == vector.players
    for expected_hand in vector.hands:
        actual = deal.hand_for_seat(expected_hand.seat)
        assert actual.hole == expected_hand.hole
        assert actual.extra == expected_hand.extra


def test_determinism_same_seed_and_players_give_same_deal() -> None:
    seed = b"\x07" * fairness.SEED_SIZE
    player_ids = [11, 22, 33]
    first = deal_cards(seed=seed, player_ids=player_ids, x=6, y=2)
    second = deal_cards(seed=seed, player_ids=player_ids, x=6, y=2)
    assert first == second


@pytest.mark.parametrize(
    ("player_count", "x", "y"),
    [(2, 5, 21), (3, 6, 2), (8, 5, 3), (4, 10, 1), (2, 21, 21)],
)
def test_no_duplicate_cards_across_random_seeds(player_count: int, x: int, y: int) -> None:
    rng = random.Random(20240615)
    for _ in range(25):
        seed = rng.randbytes(fairness.SEED_SIZE)
        player_ids = list(range(player_count))
        deal = deal_cards(seed=seed, player_ids=player_ids, x=x, y=y)
        all_cards = deal.all_cards()
        assert len(all_cards) == len(set(all_cards))


def test_eight_players_x5_leaves_cards_over_no_extras() -> None:
    seed = b"\x11" * fairness.SEED_SIZE
    deal = deal_cards(seed=seed, player_ids=list(range(8)), x=5, y=21)
    assert deal.extra_count == 0
    for hand in deal.hands:
        assert hand.extra == ()


def test_two_players_x5_y21_get_sixteen_extra_each() -> None:
    seed = b"\x11" * fairness.SEED_SIZE
    deal = deal_cards(seed=seed, player_ids=[1, 2], x=5, y=21)
    assert deal.extra_count == 16
    for hand in deal.hands:
        assert len(hand.extra) == 16
        assert len(hand.cards) == 21


def test_extra_rounds_capped_below_deck_allows() -> None:
    assert extra_rounds_for(player_count=3, x=6, y=2) == 2
    assert extra_rounds_for(player_count=3, x=6, y=21) == min(21, (52 - 18 - 10) // 3)


def test_extra_rounds_y_zero_gives_no_extras() -> None:
    assert extra_rounds_for(player_count=4, x=8, y=0) == 0
    deal = deal_cards(seed=b"\x22" * fairness.SEED_SIZE, player_ids=[1, 2, 3, 4], x=8, y=0)
    assert deal.extra_count == 0


def test_extra_rounds_for_rejects_bad_player_count() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        extra_rounds_for(player_count=0, x=5, y=1)


def test_extra_rounds_for_rejects_too_many_players() -> None:
    with pytest.raises(ValueError, match="need more than"):
        extra_rounds_for(player_count=9, x=5, y=1)


def test_dealing_order_derived_from_deck_order() -> None:
    rng = random.Random(987654321)
    seed = rng.randbytes(fairness.SEED_SIZE)
    player_ids = (501, 502, 503, 504)
    x, y = 6, 4
    n = len(player_ids)

    deal = deal_cards(seed=seed, player_ids=player_ids, x=x, y=y)
    deck = fairness.deck_order(seed)
    order = seat_order(deal.button_seat, n)

    assert deal.hand_for_seat(order[0]).hole[0] == deck[0]

    for round_index in range(x):
        for position, seat in enumerate(order):
            expected = deck[round_index * n + position]
            assert deal.hand_for_seat(seat).hole[round_index] == expected

    board_start = x * n
    assert deal.board1 == deck[board_start : board_start + rules.BOARD_SIZE]
    assert deal.board2 == deck[board_start + rules.BOARD_SIZE : board_start + 2 * rules.BOARD_SIZE]

    extra_base = board_start + 2 * rules.BOARD_SIZE
    for round_index in range(deal.extra_count):
        for position, seat in enumerate(order):
            expected = deck[extra_base + round_index * n + position]
            assert deal.hand_for_seat(seat).extra[round_index] == expected


def test_seat_order_starts_after_button() -> None:
    assert seat_order(0, 4) == (1, 2, 3, 0)
    assert seat_order(3, 4) == (0, 1, 2, 3)
    assert seat_order(0, 1) == (0,)


def test_seat_order_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        seat_order(0, 0)
    with pytest.raises(ValueError, match=r"0\.\.3"):
        seat_order(4, 4)


def test_deal_cards_rejects_bad_seed_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        deal_cards(seed=b"\x00" * 31, player_ids=[1, 2], x=5, y=0)


def test_deal_cards_rejects_empty_player_ids() -> None:
    with pytest.raises(ValueError, match="at least one player"):
        deal_cards(seed=b"\x00" * fairness.SEED_SIZE, player_ids=[], x=5, y=0)


def test_deal_cards_rejects_duplicate_player_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        deal_cards(seed=b"\x00" * fairness.SEED_SIZE, player_ids=[1, 1, 2], x=5, y=0)


@pytest.mark.parametrize("x", [4, 22])
def test_deal_cards_rejects_x_outside_limits(x: int) -> None:
    with pytest.raises(ValueError, match="X must be"):
        deal_cards(seed=b"\x00" * fairness.SEED_SIZE, player_ids=[1, 2], x=x, y=0)


@pytest.mark.parametrize("y", [-1, 22])
def test_deal_cards_rejects_y_outside_limits(y: int) -> None:
    with pytest.raises(ValueError, match="Y must be"):
        deal_cards(seed=b"\x00" * fairness.SEED_SIZE, player_ids=[1, 2], x=5, y=y)


def test_deal_cards_rejects_too_many_players_for_x() -> None:
    with pytest.raises(ValueError, match="do not fit"):
        deal_cards(seed=b"\x00" * fairness.SEED_SIZE, player_ids=list(range(9)), x=5, y=0)


def _sample_deal() -> Deal:
    return deal_cards(seed=b"\x33" * fairness.SEED_SIZE, player_ids=[1, 2], x=5, y=21)


def test_deal_json_round_trip_21_cards() -> None:
    deal = _sample_deal()
    assert deal.extra_count == 16
    restored = Deal.from_json(deal.to_json())
    assert restored == deal


def test_deal_json_round_trip_8_players() -> None:
    deal = deal_cards(seed=b"\x44" * fairness.SEED_SIZE, player_ids=list(range(8)), x=5, y=3)
    assert deal.extra_count == 0
    restored = Deal.from_json(deal.to_json())
    assert restored == deal


def test_deal_json_payload_holds_indexes_and_version() -> None:
    deal = _sample_deal()
    payload = deal.to_json_dict()
    assert payload["version"] == 1
    assert all(isinstance(index, int) for index in payload["board1"])
    assert all(isinstance(index, int) for index in payload["board2"])
    for hand in payload["hands"]:
        assert all(isinstance(index, int) for index in hand["hole"])
        assert all(isinstance(index, int) for index in hand["extra"])


def _valid_payload() -> dict[str, Any]:
    return _sample_deal().to_json_dict()


def test_from_json_dict_rejects_wrong_version() -> None:
    payload = _valid_payload()
    payload["version"] = 2
    with pytest.raises(ValueError, match="unsupported deal JSON version"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_missing_key() -> None:
    payload = _valid_payload()
    del payload["x"]
    with pytest.raises(ValueError, match="missing 'x'"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_non_int_card() -> None:
    payload = _valid_payload()
    payload["board1"][0] = "not-a-card"
    with pytest.raises(ValueError, match="must hold card indexes"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_out_of_range_index() -> None:
    payload = _valid_payload()
    payload["board1"][0] = 52
    with pytest.raises(ValueError, match=r"0\.\.51"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_duplicate_card() -> None:
    payload = _valid_payload()
    payload["board1"][0] = payload["board2"][0]
    with pytest.raises(ValueError, match="dealt twice"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_seat_out_of_order() -> None:
    payload = _valid_payload()
    payload["hands"][0]["seat"] = 1
    payload["hands"][1]["seat"] = 0
    with pytest.raises(ValueError, match="seat order"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_wrong_board_size() -> None:
    payload = _valid_payload()
    payload["board1"] = payload["board1"][:-1]
    with pytest.raises(ValueError, match="board1 must hold"):
        Deal.from_json_dict(payload)


def test_from_json_dict_rejects_wrong_hole_card_count() -> None:
    payload = _valid_payload()
    payload["hands"][0]["hole"] = payload["hands"][0]["hole"][:-1]
    with pytest.raises(ValueError, match="expected X ="):
        Deal.from_json_dict(payload)


def test_from_json_rejects_invalid_json_text() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        Deal.from_json("{not json")


def test_deal_post_init_rejects_empty_hands() -> None:
    with pytest.raises(ValueError, match="at least one player"):
        Deal(x=5, y=0, button_seat=0, hands=(), board1=(), board2=())


def test_deal_post_init_rejects_bad_button_seat() -> None:
    hand = PlayerHand(seat=0, player_id=1, hole=(Card.from_index(0),) * 5, extra=())
    board = tuple(Card.from_index(i) for i in range(5, 10))
    with pytest.raises(ValueError, match="button seat"):
        Deal(x=5, y=0, button_seat=1, hands=(hand,), board1=board, board2=board)

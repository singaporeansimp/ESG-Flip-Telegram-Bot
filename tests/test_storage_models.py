from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from fractions import Fraction

import pytest

from esgbot.game.rules import Y_DEFAULT, FlipRequest, Settings
from esgbot.storage import ActiveFlip, FinishedFlip, FlipPlayer, FlipState, PlayerStats, StoredFlip
from tests.storage_helpers import EPOCH, Line, make_flip

LINES = [
    Line(11, "Alice", 50, None, Fraction(2)),
    Line(22, "Bob", -25, None, Fraction(1, 2)),
    Line(33, "Cara", -25, None, Fraction(1, 2)),
]


def good() -> FinishedFlip:
    return make_flip(-1, LINES, seed_byte=1)


def test_valid_flip_exposes_deal_properties() -> None:
    flip = good()
    assert flip.player_count == 3
    assert flip.button_seat == flip.deal.button_seat
    assert flip.player_at(1) is flip.players[1]
    with pytest.raises(IndexError):
        flip.player_at(3)
    with pytest.raises(IndexError):
        flip.player_at(-1)


def test_players_must_match_deal() -> None:
    flip = good()
    with pytest.raises(ValueError, match="players recorded"):
        replace(flip, players=flip.players[:2])
    with pytest.raises(ValueError, match="seat order"):
        replace(flip, players=(flip.players[1], flip.players[0], flip.players[2]))
    with pytest.raises(ValueError, match="but the deal has"):
        replace(flip, players=(replace(flip.players[0], user_id=999), *flip.players[1:]))


def test_settings_must_match_deal() -> None:
    flip = good()
    # Both settings are legal in themselves; what they are not is this deal's.
    with pytest.raises(ValueError, match="disagree"):
        replace(flip, settings=Settings(wager=50, x=6, y=2, z=3))
    with pytest.raises(ValueError, match="disagree"):
        replace(flip, settings=Settings(wager=50, x=5, y=3, z=3))


def test_seed_and_commitment_are_checked() -> None:
    flip = good()
    with pytest.raises(ValueError, match="seed"):
        replace(flip, seed=b"short")
    with pytest.raises(ValueError, match="commitment"):
        replace(flip, commitment="abc")
    with pytest.raises(ValueError, match="commitment"):
        replace(flip, commitment="G" * 64)


def test_only_one_player_may_scoop() -> None:
    flip = good()
    players = tuple(replace(p, scooped=True) for p in flip.players[:2]) + flip.players[2:]
    with pytest.raises(ValueError, match="only one player can scoop"):
        replace(flip, players=players)


def test_times_must_be_aware_and_ordered() -> None:
    flip = good()
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(flip, started_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(flip, finished_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="before"):
        replace(flip, finished_at=flip.started_at - timedelta(seconds=1))


def test_player_active_and_stats_validation() -> None:
    with pytest.raises(ValueError, match="seat"):
        FlipPlayer(1, "A", None, -1, Fraction(0), 0, False)
    with pytest.raises(ValueError, match="points"):
        FlipPlayer(1, "A", None, 0, Fraction(-1), 0, False)
    with pytest.raises(ValueError, match="token"):
        ActiveFlip("", 1, None, 1, FlipState.LOBBY, None, EPOCH)
    with pytest.raises(ValueError, match="seed"):
        ActiveFlip("t", 1, None, 1, FlipState.LOBBY, b"short", EPOCH)
    with pytest.raises(ValueError, match="timezone-aware"):
        ActiveFlip("t", 1, None, 1, FlipState.LOBBY, None, datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="flip number"):
        StoredFlip(0, good())
    with pytest.raises(ValueError, match="2 wins"):
        PlayerStats(1, "A", None, 0, 1, 2, 0)
    with pytest.raises(ValueError, match="2 scoops"):
        PlayerStats(1, "A", None, 0, 1, 0, 2)
    with pytest.raises(ValueError, match="negative"):
        PlayerStats(1, "A", None, 0, -1, 0, 0)


def test_flip_state_values() -> None:
    assert FlipState.LOBBY.value == "lobby"
    assert FlipState.RUNNING.value == "running"


def with_settings(
    settings: Settings, *, y_defaulted: bool = False, z_defaulted: bool = False
) -> FinishedFlip:
    flip = make_flip(-1, LINES[:2], seed_byte=1, settings=settings)
    return replace(flip, y_defaulted=y_defaulted, z_defaulted=z_defaulted)


def test_a_flip_is_typed_out_in_full_unless_it_says_otherwise() -> None:
    flip = good()
    assert flip.y_defaulted is False
    assert flip.z_defaulted is False
    assert flip.request == FlipRequest(50, 5, 2, 3)


@pytest.mark.parametrize(
    ("y_defaulted", "z_defaulted"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_the_flags_travel_and_the_request_drops_what_was_defaulted(
    y_defaulted: bool, z_defaulted: bool
) -> None:
    settings = Settings(wager=50, x=5, y=Y_DEFAULT if y_defaulted else 4, z=2 if z_defaulted else 9)
    flip = with_settings(settings, y_defaulted=y_defaulted, z_defaulted=z_defaulted)
    assert flip.y_defaulted is y_defaulted
    assert flip.z_defaulted is z_defaulted
    assert flip.settings == settings
    assert flip.request == FlipRequest(
        50, 5, None if y_defaulted else 4, None if z_defaulted else 9
    )


def test_a_defaulted_y_must_be_the_default() -> None:
    with pytest.raises(ValueError, match="Y was left out"):
        with_settings(Settings(wager=50, x=5, y=20, z=2), y_defaulted=True)


def test_a_defaulted_z_must_be_the_player_count() -> None:
    with pytest.raises(ValueError, match="Z was left out"):
        with_settings(Settings(wager=50, x=5, y=2, z=3), z_defaulted=True)  # two players
    with pytest.raises(ValueError, match="Z was left out"):
        replace(good(), z_defaulted=True, settings=Settings(wager=50, x=5, y=2, z=2))  # three


def test_the_flags_not_the_numbers_decide_what_was_defaulted() -> None:
    # y is 21 and z is the player count, but both were typed out.
    flip = with_settings(Settings(wager=50, x=5, y=Y_DEFAULT, z=2))
    assert flip.request == FlipRequest(50, 5, Y_DEFAULT, 2)
    assert flip.request.y_defaulted is False
    assert flip.request.z_defaulted is False

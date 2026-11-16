"""The data types stored flips are written with and read back as.

These are the shapes the engine and the commands share, so neither has to know
any SQL:

:class:`FinishedFlip`
    One flip that reached its result, with its settings, the seed and commitment,
    the full :class:`~esgbot.game.deal.Deal` and a :class:`FlipPlayer` per seat.
    It carries no flip number: the public number is assigned when the flip is
    saved, and :class:`StoredFlip` is what comes back out.
:class:`ActiveFlip`
    One flip still in progress, keyed by its internal token. Rows like this are
    what a restart finds and voids.
:class:`PlayerStats`
    One player's figures in one group, for ``/stats`` and the leaderboard.

Points are exact rational numbers (:class:`fractions.Fraction`), never floats, so
a three-way split of a board point stays a third. Money is whole dollars, so every
amount here is an :class:`int`. Times are timezone-aware; naive datetimes are
rejected rather than guessed at, because the stored value is read back as UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from typing import Final

from esgbot.game import fairness
from esgbot.game.deal import Deal
from esgbot.game.rules import Settings

__all__ = [
    "COMMITMENT_LENGTH",
    "ActiveFlip",
    "FinishedFlip",
    "FlipPlayer",
    "FlipState",
    "GroupMigration",
    "PlayerStats",
    "StoredFlip",
    "normalize_username",
]

COMMITMENT_LENGTH: Final = 64
"""Characters in a commitment: SHA-256 as lowercase hex."""

_HEX_DIGITS: Final = frozenset("0123456789abcdef")


class FlipState(StrEnum):
    """What an in-progress flip is doing: waiting for players, or dealing."""

    LOBBY = "lobby"
    RUNNING = "running"


def normalize_username(username: str | None) -> str | None:
    """A Telegram username in the one form that is stored and looked up.

    Trims whitespace and drops a leading ``@``, and turns anything that is empty
    afterwards into ``None``, so "no username" has a single representation. Case
    is left alone; lookups match case-insensitively instead.
    """
    if username is None:
        return None
    text = username.strip()
    if text.startswith("@"):
        text = text[1:].strip()
    return text or None


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{label} must be timezone-aware, got {value!r}")
    return value


def _require_commitment(value: str) -> str:
    if len(value) != COMMITMENT_LENGTH or not _HEX_DIGITS.issuperset(value):
        raise ValueError(
            f"commitment must be {COMMITMENT_LENGTH} lowercase hex digits, got {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class FlipPlayer:
    """One player's line in a finished flip, as they were at the time.

    ``display_name`` and ``username`` are kept as recorded rather than looked up
    again later, so old flips still read the way they did when they were played.
    ``points`` is exact, ``net_dollars`` is whole dollars and may be negative, and
    ``scooped`` marks the one player who took all three points.
    """

    user_id: int
    display_name: str
    username: str | None
    seat: int
    points: Fraction
    net_dollars: int
    scooped: bool

    def __post_init__(self) -> None:
        if self.seat < 0:
            raise ValueError(f"seat must be 0 or more, got {self.seat}")
        if self.points < 0:
            raise ValueError(f"points cannot be negative, got {self.points}")


@dataclass(frozen=True, slots=True)
class FinishedFlip:
    """A flip that reached its result, ready to be recorded.

    The player count and the button seat are not fields: they come from ``deal``,
    so the stored row cannot disagree with the stored deal. ``players`` holds one
    entry per seat, in seat order, matching the deal's player ids.

    The commitment is checked for shape only, not recomputed from the seed.
    Storage records what it is given; proving that the seed, deal and results
    belong together is what ``/verify`` does.

    Raises:
        ValueError: if the players do not match the deal seat for seat, the
            settings disagree with the deal's X or Y, the seed or commitment is
            malformed, more than one player scooped, or a time is naive or the
            flip finishes before it started.
    """

    group_id: int
    settings: Settings
    seed: bytes
    commitment: str
    deal: Deal
    players: tuple[FlipPlayer, ...]
    started_at: datetime
    finished_at: datetime

    def __post_init__(self) -> None:
        fairness.validate_seed(self.seed)
        _require_commitment(self.commitment)
        _require_aware(self.started_at, "started_at")
        _require_aware(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError(
                f"finished_at {self.finished_at.isoformat()} is before "
                f"started_at {self.started_at.isoformat()}"
            )
        if self.settings.x != self.deal.x or self.settings.y != self.deal.y:
            raise ValueError(
                f"settings X = {self.settings.x}, Y = {self.settings.y} disagree with the "
                f"deal's X = {self.deal.x}, Y = {self.deal.y}"
            )
        if len(self.players) != self.deal.player_count:
            raise ValueError(
                f"{len(self.players)} players recorded for a {self.deal.player_count}-player deal"
            )
        for seat, player in enumerate(self.players):
            if player.seat != seat:
                raise ValueError(
                    f"player {seat} has seat {player.seat}; players must be in seat order"
                )
            expected_id = self.deal.hands[seat].player_id
            if player.user_id != expected_id:
                raise ValueError(
                    f"seat {seat} records user {player.user_id} but the deal has {expected_id}"
                )
        scoopers = [player.seat for player in self.players if player.scooped]
        if len(scoopers) > 1:
            raise ValueError(f"only one player can scoop, got seats {scoopers}")

    @property
    def player_count(self) -> int:
        """How many players were in the flip, which is how many seats it had."""
        return self.deal.player_count

    @property
    def button_seat(self) -> int:
        """The seat that held the button, from the deal."""
        return self.deal.button_seat

    def player_at(self, seat: int) -> FlipPlayer:
        """The player recorded in this seat.

        Raises:
            IndexError: if there is no such seat.
        """
        if not 0 <= seat < len(self.players):
            raise IndexError(f"no seat {seat} in a {self.player_count}-player flip")
        return self.players[seat]


@dataclass(frozen=True, slots=True)
class StoredFlip:
    """A finished flip read back out, with the public number it was given."""

    flip_number: int
    flip: FinishedFlip

    def __post_init__(self) -> None:
        if self.flip_number < 1:
            raise ValueError(f"flip number must be 1 or more, got {self.flip_number}")


@dataclass(frozen=True, slots=True)
class ActiveFlip:
    """A flip still in progress, as tracked so a restart can clean it up.

    ``token`` is the flip's internal identifier, which is also what button data
    carries. ``topic_id`` is set only in groups with topics. ``message_id`` is the
    lobby message, which later becomes the table. ``seed`` is ``None`` until Start
    reveals the player list and generates it.
    """

    token: str
    group_id: int
    topic_id: int | None
    message_id: int
    state: FlipState
    seed: bytes | None
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("an active flip needs a token")
        if self.seed is not None:
            fairness.validate_seed(self.seed)
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class PlayerStats:
    """One player's figures in one group, for both ``/stats`` and the leaderboard.

    A flip counts once its result is saved, so cancelled and voided flips never
    appear here. A win is a flip the player's net came out above zero, which means
    a chop winner who nets $0 played but did not win.
    """

    user_id: int
    display_name: str
    username: str | None
    net_dollars: int
    flips_played: int
    wins: int
    scoops: int

    def __post_init__(self) -> None:
        if self.flips_played < 0:
            raise ValueError(f"flips played cannot be negative, got {self.flips_played}")
        if not 0 <= self.wins <= self.flips_played:
            raise ValueError(f"{self.wins} wins in {self.flips_played} flips")
        if not 0 <= self.scoops <= self.flips_played:
            raise ValueError(f"{self.scoops} scoops in {self.flips_played} flips")

    @property
    def win_rate(self) -> float:
        """Wins divided by flips played, and ``0.0`` for a player with no flips."""
        if self.flips_played == 0:
            return 0.0
        return self.wins / self.flips_played


@dataclass(frozen=True, slots=True)
class GroupMigration:
    """How many rows a group id change moved, for the log line that follows it."""

    flips: int
    active_flips: int

    @property
    def rows(self) -> int:
        """Rows moved in total."""
        return self.flips + self.active_flips

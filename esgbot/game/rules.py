"""Flip settings: the four values, their limits, and how text becomes them.

A flip is configured with a wager and the three game settings:

===========  =================================================  ==============
Setting      Meaning                                            Limits
===========  =================================================  ==============
``wager``    Whole dollars each player puts in                   1..1,000,000
``x``        Private cards dealt to each player first            5..21
``y``        Maximum extra private cards per player              0..21
``z``        Scoop multiplier (``1`` means no scoop bonus)       1..100
===========  =================================================  ==============

Values are plain whole numbers written in digits only. Signs, decimal points,
thousands separators, whitespace and currency symbols are rejected rather than
guessed at, so ``$50``, ``5.0``, ``1,000`` and ``-3`` are all errors.

Two boards take ten cards from the 52, which leaves 42 for the players, so at
most ``floor(42 / x)`` players fit in one flip.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BOARD_CARDS",
    "BOARD_COUNT",
    "BOARD_SIZE",
    "MIN_PLAYERS",
    "PLAYER_CARD_BUDGET",
    "SETTING_FIELDS",
    "WAGER_MAX",
    "WAGER_MIN",
    "X_MAX",
    "X_MIN",
    "Y_MAX",
    "Y_MIN",
    "Z_MAX",
    "Z_MIN",
    "Settings",
    "SettingsError",
    "max_players",
    "parse_settings",
    "parse_settings_tokens",
    "parse_whole_number",
]

WAGER_MIN: Final = 1
WAGER_MAX: Final = 1_000_000
X_MIN: Final = 5
X_MAX: Final = 21
Y_MIN: Final = 0
Y_MAX: Final = 21
Z_MIN: Final = 1
Z_MAX: Final = 100

BOARD_COUNT: Final = 2
"""Community boards dealt in every flip."""

BOARD_SIZE: Final = 5
"""Cards on each community board."""

BOARD_CARDS: Final = BOARD_COUNT * BOARD_SIZE
"""Cards the two boards take from the deck."""

PLAYER_CARD_BUDGET: Final = 52 - BOARD_CARDS
"""Cards left for the players once both boards are dealt: 42."""

MIN_PLAYERS: Final = 2
"""A flip needs at least two players. The engine enforces this when Start is pressed."""

SETTING_FIELDS: Final = ("wager", "X", "Y", "Z")
"""Field names used in error messages, in the order settings are given."""

_LIMITS: Final[dict[str, tuple[int, int]]] = {
    "wager": (WAGER_MIN, WAGER_MAX),
    "X": (X_MIN, X_MAX),
    "Y": (Y_MIN, Y_MAX),
    "Z": (Z_MIN, Z_MAX),
}

_DIGITS: Final = frozenset("0123456789")
_MAX_DIGITS: Final = 18
_ECHO_LIMIT: Final = 24


class SettingsError(ValueError):
    """A setting could not be read or is outside its limits.

    ``str(error)`` is a complete, user-readable sentence. ``field`` names the
    setting it is about, one of :data:`SETTING_FIELDS`, or ``""`` when the error
    is about the set of values as a whole.
    """

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def max_players(x: int) -> int:
    """How many players fit in a flip that deals ``x`` cards each: ``floor(42 / x)``.

    Raises:
        ValueError: if ``x`` is below 1.
    """
    if x < 1:
        raise ValueError(f"X must be at least 1, got {x}")
    return PLAYER_CARD_BUDGET // x


@dataclass(frozen=True, slots=True)
class Settings:
    """One flip's validated settings. Every field is within its limits.

    The constructor is where that is true rather than merely intended: it checks
    all four values against the table above, so there is no way to make a
    ``Settings`` the limits do not hold for, however it was reached --
    :func:`parse_settings` from a command, or a row read back out of a database
    somebody has edited.

    Raises:
        SettingsError: if a value is not a whole number or is outside its
            limits. It is a :class:`ValueError`, so a caller reading stored
            settings catches it with everything else a damaged row raises.
    """

    wager: int
    x: int
    y: int
    z: int

    def __post_init__(self) -> None:
        for field, value in zip(SETTING_FIELDS, (self.wager, self.x, self.y, self.z), strict=True):
            _check_setting(field, value)

    @property
    def max_players(self) -> int:
        """The player cap for this flip's X: ``floor(42 / x)``."""
        return max_players(self.x)

    def pot(self, player_count: int) -> int:
        """The pot for this many players: ``wager * player_count``."""
        if player_count < 1:
            raise ValueError(f"player count must be at least 1, got {player_count}")
        return self.wager * player_count


def _echo(text: str) -> str:
    """The user's input, quoted and shortened, for an error message."""
    shown = text if len(text) <= _ECHO_LIMIT else text[:_ECHO_LIMIT] + "…"
    return f'"{shown}"'


def _range_clause(minimum: int, maximum: int) -> str:
    return f"a whole number from {minimum:,} to {maximum:,}"


def _check_setting(field: str, value: object) -> None:
    """Check one setting as a value, not as text, for :class:`Settings`.

    :func:`parse_whole_number` does this for text a person typed, down to how it
    was written; this is the same limits applied to whatever a caller hands the
    constructor, including a number read back out of storage.

    Raises:
        SettingsError: if the value is not a whole number -- ``True`` is not one
            -- or is outside this field's limits.
    """
    minimum, maximum = _LIMITS[field]
    allowed = _range_clause(minimum, maximum)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(f"{field} must be {allowed}. Got {value!r}.", field=field)
    if not minimum <= value <= maximum:
        raise SettingsError(f"{field} must be {allowed}. Got {value:,}.", field=field)


def parse_whole_number(text: str, *, field: str, minimum: int, maximum: int) -> int:
    """Read one setting from text.

    Accepts digits only, then checks the range. Every failure raises a
    :class:`SettingsError` whose message names the field, says what is allowed and
    echoes what was given.
    """
    allowed = _range_clause(minimum, maximum)
    if not text:
        raise SettingsError(f"{field} is missing. It must be {allowed}.", field=field)
    if not _DIGITS.issuperset(text):
        raise SettingsError(
            f"{field} must be {allowed}, written in digits only, with no spaces, signs, "
            f"decimal points, separators or currency symbols. Got {_echo(text)}.",
            field=field,
        )
    if len(text) > _MAX_DIGITS:
        raise SettingsError(f"{field} must be {allowed}. Got {_echo(text)}.", field=field)
    value = int(text)
    if not minimum <= value <= maximum:
        raise SettingsError(f"{field} must be {allowed}. Got {value:,}.", field=field)
    return value


def parse_settings(*, wager: str, x: str, y: str, z: str) -> Settings:
    """Read all four settings from text.

    Fields are checked in the order wager, X, Y, Z, and the first failure raises.

    Raises:
        SettingsError: with a user-readable message naming the field at fault.
    """
    values = {
        field: parse_whole_number(
            text, field=field, minimum=_LIMITS[field][0], maximum=_LIMITS[field][1]
        )
        for field, text in zip(SETTING_FIELDS, (wager, x, y, z), strict=True)
    }
    return Settings(wager=values["wager"], x=values["X"], y=values["Y"], z=values["Z"])


def parse_settings_tokens(tokens: Sequence[str]) -> Settings:
    """Read settings from four text tokens in the order wager, X, Y, Z.

    Raises:
        SettingsError: if there are not exactly four tokens, or one of them is invalid.
    """
    if len(tokens) != len(SETTING_FIELDS):
        raise SettingsError(
            f"Give four whole numbers: wager, X, Y and Z. For example: 50 5 2 3. Got {len(tokens)}."
        )
    wager, x, y, z = tokens
    return parse_settings(wager=wager, x=x, y=y, z=z)

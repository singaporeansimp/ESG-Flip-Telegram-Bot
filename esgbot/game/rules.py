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

**Only the wager and X have to be given.** ``/flip`` takes them in order, so a
command may stop after X or after Y: :class:`FlipRequest` is what a command asks
for, with a ``None`` standing for a value the bot fills in --
:data:`Y_DEFAULT` for ``Y``, which the deal reads as "as many extra cards as the
deck can give every seat", and the number of players for ``Z``, which is only known
once the player list is locked. :meth:`FlipRequest.resolve` turns a request into
:class:`Settings` at a table of a given size, and from there nothing downstream
can tell a value that was typed from one that was filled in: a
:class:`Settings` is always all four values, inside their limits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Self

__all__ = [
    "BOARD_CARDS",
    "BOARD_COUNT",
    "BOARD_SIZE",
    "FLIP_FIELDS_MIN",
    "MIN_PLAYERS",
    "PLAYER_CARD_BUDGET",
    "SETTING_FIELDS",
    "WAGER_MAX",
    "WAGER_MIN",
    "X_MAX",
    "X_MIN",
    "Y_DEFAULT",
    "Y_MAX",
    "Y_MIN",
    "Z_MAX",
    "Z_MIN",
    "FlipRequest",
    "Settings",
    "SettingsError",
    "max_players",
    "parse_flip_request",
    "parse_settings",
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

Y_DEFAULT: Final = Y_MAX
"""What ``Y`` means when a command leaves it out: the maximum.

The deal never sends out a round of extra cards it cannot give every seat, so
asking for 21 asks for as many as fit rather than for 21 of them. See
:func:`esgbot.game.deal.extra_rounds_for`.
"""

SETTING_FIELDS: Final = ("wager", "X", "Y", "Z")
"""Field names used in error messages, in the order settings are given."""

FLIP_FIELDS_MIN: Final = 2
"""Settings a ``/flip`` must give: the wager and ``X``. ``Y`` and ``Z`` may be left out."""

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


@dataclass(frozen=True, slots=True)
class FlipRequest:
    """What a ``/flip`` asked for, which is not yet what it will play.

    The wager and ``X`` are always given. ``y`` and ``z`` are ``None`` when the
    command left them out for the bot to fill in, and that distinction is kept
    rather than resolved on the spot for two reasons: ``Z`` *cannot* be resolved
    until Start locks the player list, and ``/again`` repeats a flip as it was
    typed, so a defaulted value is defaulted again for the new table.

    Every value that is given is checked against the same limits
    :class:`Settings` applies, so a request is as valid as it can be while two of
    its values are still open.

    :meth:`resolve` is the one way to get :class:`Settings` out of it, and what
    it returns is ordinary, fully resolved settings: nothing downstream -- the
    deal, the money, the stored flip, ``/verify`` -- has to know or ask how the
    command was written.

    Raises:
        SettingsError: if a value that was given is not a whole number or is
            outside its limits.
    """

    wager: int
    x: int
    y: int | None = None
    z: int | None = None

    def __post_init__(self) -> None:
        _check_setting("wager", self.wager)
        _check_setting("X", self.x)
        if self.y is not None:
            _check_setting("Y", self.y)
        if self.z is not None:
            _check_setting("Z", self.z)

    @classmethod
    def from_settings(
        cls, settings: Settings, *, y_defaulted: bool = False, z_defaulted: bool = False
    ) -> Self:
        """The request these settings came from, given which of them were filled in.

        This is how a stored flip says what was typed: the row holds the resolved
        values and a flag per defaulted one, and the two together are the command
        again. With neither flag it is the same flip written out in full.
        """
        return cls(
            wager=settings.wager,
            x=settings.x,
            y=None if y_defaulted else settings.y,
            z=None if z_defaulted else settings.z,
        )

    @property
    def y_defaulted(self) -> bool:
        """Whether ``Y`` was left out, so the bot filled in :data:`Y_DEFAULT`."""
        return self.y is None

    @property
    def z_defaulted(self) -> bool:
        """Whether ``Z`` was left out, so it becomes the number of players."""
        return self.z is None

    @property
    def max_players(self) -> int:
        """The player cap for this request's X: ``floor(42 / x)``."""
        return max_players(self.x)

    def resolve(self, player_count: int) -> Settings:
        """The settings this request plays at a table of this size.

        A ``Y`` that was left out becomes :data:`Y_DEFAULT`, and a ``Z`` that was
        left out becomes ``player_count``: a scoop then pays the wager once for
        every player at the table. The player cap is ``floor(42 / x)``, which is
        at most eight, so a resolved ``Z`` is always well inside its limits --
        and :class:`Settings` checks it anyway, as it checks everything else.

        Raises:
            ValueError: if ``player_count`` is below one.
            SettingsError: if the resolved settings are outside their limits,
                which takes a player count no flip can have.
        """
        if player_count < 1:
            raise ValueError(f"player count must be at least 1, got {player_count}")
        return Settings(
            wager=self.wager,
            x=self.x,
            y=Y_DEFAULT if self.y is None else self.y,
            z=player_count if self.z is None else self.z,
        )


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


def _parse_field(field: str, text: str) -> int:
    """Read one named setting from text, at that field's own limits."""
    minimum, maximum = _LIMITS[field]
    return parse_whole_number(text, field=field, minimum=minimum, maximum=maximum)


def parse_settings(*, wager: str, x: str, y: str, z: str) -> Settings:
    """Read all four settings from text.

    Fields are checked in the order wager, X, Y, Z, and the first failure raises.

    Raises:
        SettingsError: with a user-readable message naming the field at fault.
    """
    values = {
        field: _parse_field(field, text)
        for field, text in zip(SETTING_FIELDS, (wager, x, y, z), strict=True)
    }
    return Settings(wager=values["wager"], x=values["X"], y=values["Y"], z=values["Z"])


def parse_flip_request(tokens: Sequence[str]) -> FlipRequest:
    """Read a ``/flip`` command's settings: the wager and X, then Y and Z if given.

    Two, three or four tokens, in the order wager, X, Y, Z. They are positional,
    so ``Z`` can only be given after a ``Y``; what is left off the end is what
    the bot fills in. Fields are checked in that order and the first failure
    raises.

    Raises:
        SettingsError: if there are fewer than :data:`FLIP_FIELDS_MIN` tokens or
            more than there are settings, or one of them is invalid.
    """
    if not FLIP_FIELDS_MIN <= len(tokens) <= len(SETTING_FIELDS):
        raise SettingsError(
            "Give the wager and X, then Y and Z if you want them. "
            f"For example: 50 5, or 50 5 2 3. Got {len(tokens)}."
        )
    values = [
        _parse_field(field, text) for field, text in zip(SETTING_FIELDS, tokens, strict=False)
    ]
    wager, x = values[0], values[1]
    return FlipRequest(
        wager=wager,
        x=x,
        y=values[2] if len(values) > 2 else None,
        z=values[3] if len(values) > 3 else None,
    )

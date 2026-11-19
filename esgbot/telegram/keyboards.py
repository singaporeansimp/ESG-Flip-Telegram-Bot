"""The buttons under a flip, and the data they carry back.

Callback data is ``<press><separator><token>``: one letter for what was pressed
and the flip's internal token, which is what lets a press be matched to its flip
after the lobby message has scrolled away -- or, when the token means nothing to
this process, be answered as expired. That is 13 characters for an
:func:`~esgbot.engine.new_token` token, well inside Telegram's
:data:`CALLBACK_DATA_LIMIT` of 64 bytes; :func:`pack` refuses anything longer
rather than letting Telegram silently drop a button.

A result's Again button is the exception: what it carries is the flip's public
number, not a token, because the flip is over and the registry has forgotten it.
The handler reads the settings back from storage by that number, in the group the
press came from.

:func:`markup_for` is the one rule about which view carries which buttons: an
open lobby has four, a live table has Peek, a recorded result has Again, and
everything else final has none.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from esgbot.engine.views import FlipView, LobbyStatus, LobbyView, ResultView, TableView

__all__ = [
    "CALLBACK_DATA_LIMIT",
    "SEPARATOR",
    "Press",
    "PressData",
    "lobby_markup",
    "markup_for",
    "pack",
    "parse",
    "result_markup",
    "table_markup",
]

CALLBACK_DATA_LIMIT: Final = 64
"""Bytes of callback data Telegram allows on one button."""

SEPARATOR: Final = ":"
"""Between the press and the token. Never appears in a token."""


class Press(StrEnum):
    """What a button does. The value is what travels in the callback data."""

    JOIN = "j"
    LEAVE = "l"
    START = "s"
    CANCEL = "c"
    PEEK = "p"
    AGAIN = "a"


@dataclass(frozen=True, slots=True)
class PressData:
    """A button press, read back off the callback data."""

    press: Press
    token: str


def pack(press: Press, token: str) -> str:
    """The callback data for one button.

    Raises:
        ValueError: if the token is empty, holds the separator, or would push the
            data past :data:`CALLBACK_DATA_LIMIT` bytes.
    """
    if not token:
        raise ValueError("a button needs the flip's token")
    if SEPARATOR in token:
        raise ValueError(f"token {token!r} cannot contain {SEPARATOR!r}")
    data = f"{press.value}{SEPARATOR}{token}"
    size = len(data.encode())
    if size > CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback data is {size} bytes, over the {CALLBACK_DATA_LIMIT} allowed")
    return data


def parse(data: str | None) -> PressData | None:
    """Read a press back, or ``None`` for anything this bot did not write.

    Callback data comes from the client, so a button from an older version of the
    bot, or from nowhere at all, has to be a plain miss rather than an error.
    """
    if not data:
        return None
    letter, separator, token = data.partition(SEPARATOR)
    if not separator or not token:
        return None
    try:
        press = Press(letter)
    except ValueError:
        return None
    return PressData(press=press, token=token)


def lobby_markup(token: str) -> InlineKeyboardMarkup:
    """Join and Leave, then Start and Cancel."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Join", callback_data=pack(Press.JOIN, token)),
                InlineKeyboardButton(text="🚪 Leave", callback_data=pack(Press.LEAVE, token)),
            ],
            [
                InlineKeyboardButton(text="▶️ Start", callback_data=pack(Press.START, token)),
                InlineKeyboardButton(text="❌ Cancel", callback_data=pack(Press.CANCEL, token)),
            ],
        ]
    )


def table_markup(token: str) -> InlineKeyboardMarkup:
    """The one button on a live table."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👀 Peek", callback_data=pack(Press.PEEK, token))]
        ]
    )


def result_markup(flip_number: int) -> InlineKeyboardMarkup:
    """The one button on a recorded result: play that flip's settings again."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Again", callback_data=pack(Press.AGAIN, str(flip_number))
                )
            ]
        ]
    )


def markup_for(view: FlipView) -> InlineKeyboardMarkup | None:
    """The buttons this view carries, or ``None`` for a message that is final.

    A voided table keeps its cards and loses its button, which is how a flip that
    was abandoned stops taking presses while staying readable.
    """
    match view:
        case LobbyView(status=LobbyStatus.OPEN):
            return lobby_markup(view.token)
        case TableView(voided=False):
            return table_markup(view.token)
        case ResultView(flip_number=int() as flip_number):
            return result_markup(flip_number)
        case _:
            return None

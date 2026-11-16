"""The engine's one way out: post a message, edit a message, answer a press.

Three calls, no text. The engine passes :mod:`view data <esgbot.engine.views>`
and the Telegram layer turns it into a message, which is why the engine can be
tested with a fake gateway that only writes down what it was asked to show.

Two rules the engine keeps, and a test checks:

* A button press is answered before any group message that follows it, so a Peek
  is never stuck behind a slow edit.
* Posts and edits for one flip go out in order, one at a time.

Two rules the gateway keeps, and the Telegram ticket implements:

* Waiting is its business. When Telegram asks the bot to wait, the gateway waits
  and retries the same call. The flip is late but its order never changes.
* After three failed attempts it raises :class:`PermanentFailureError`, which
  tells the engine the flip cannot be shown any more and voids it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from esgbot.engine.views import FlipView, PressAnswer

__all__ = ["Gateway", "MessageTarget", "PermanentFailureError"]


class PermanentFailureError(Exception):
    """The gateway cannot deliver, and trying again will not help.

    The Telegram gateway raises this once a send or an edit has failed three
    times: the bot has been removed from the group, or has lost permission to
    post there. The engine voids the flip, clears its row and records nothing,
    and posts no notice, because posting is exactly what is not working.

    A failed button-press answer does not raise it. One lost pop-up is not a lost
    group, and the flip carries on.
    """


@dataclass(frozen=True, slots=True)
class MessageTarget:
    """Where a flip's messages go: one group, and one forum topic within it.

    ``topic_id`` is ``None`` in groups without topics, which is most of them.
    Every message of a flip goes to the same place.
    """

    group_id: int
    topic_id: int | None = None


@runtime_checkable
class Gateway(Protocol):
    """Output, as the engine sees it."""

    async def post(self, target: MessageTarget, view: FlipView) -> int:
        """Post a new message and return its id.

        Raises:
            PermanentFailureError: if the message cannot be posted at all.
        """
        ...

    async def edit(self, target: MessageTarget, message_id: int, view: FlipView) -> int:
        """Edit a message and return the id that now holds the view.

        That is ``message_id`` almost always. If the message has been deleted,
        the gateway posts a fresh one and returns its id instead; the engine then
        edits that one from here on and writes the new id to the flip's row.

        An edit Telegram rejects as unchanged is not an error and is ignored.

        Raises:
            PermanentFailureError: if the view cannot be shown at all.
        """
        ...

    async def answer_press(self, press_id: str, answer: PressAnswer) -> bool:
        """Answer a button press with a private pop-up, seen only by whoever pressed.

        ``answer`` is a :class:`~esgbot.engine.views.Notice` saying what the press
        did, or a :class:`~esgbot.engine.views.PeekAnswer` holding that player's
        own cards. Every press is answered exactly once, before any group message
        the press leads to.

        Returns:
            Whether the pop-up went out. A pop-up is never retried -- an answer
            is only valid for a few seconds -- and a lost one never costs the
            flip, so this does not raise. It matters all the same: a Peek the
            player never saw is not a peek, and does not count towards everyone
            having peeked.
        """
        ...

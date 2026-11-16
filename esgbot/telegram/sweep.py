"""What the bot does about the flips that were open when it last stopped.

Live flip state is in memory and nothing else, so a restart is the end of every
flip that was running. What survives is one row per open flip, and this is the
sweep that reads them: for each, take the buttons off whatever message it was
living on, tell the group it was voided -- with the seed, if it had dealt, so the
deal can still be checked -- and forget the row. Nothing goes into stats.

A row left here is a flip whose outcome never reached its group. One that
reached its result had its row deleted by the same transaction that recorded it,
so there is no window in which a recorded flip is also a leftover and nothing
here can announce one as voided. One whose result could not be recorded keeps
its row until the group has been told -- that row holds the only durable copy of
the seed -- and a void does the same, so what is left over when the bot starts
again is exactly the flips whose seed nobody has seen. Revealing it here is the
last chance to, which is why this runs before anything else.

It runs before polling starts, so no press can arrive while the sweep is still
going. Presses that arrive afterwards carry tokens this process has never heard
of, and :mod:`esgbot.telegram.handlers` answers those as expired.

One group being unreachable does not stop the sweep: the notice is dropped, the
row still goes, and the next start has nothing left to do about it.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable

from esgbot.engine import FlipView, MessageTarget, PermanentFailureError, VoidReason, VoidView
from esgbot.logging_setup import get_logger
from esgbot.storage import ActiveFlip, StorageError

__all__ = ["SweepGateway", "SweepStore", "void_leftover_flips"]

_logger = get_logger(__name__)


@runtime_checkable
class SweepStore(Protocol):
    """The two storage calls the sweep makes. :class:`esgbot.storage.Database` is one."""

    async def list_active_flips(self) -> tuple[ActiveFlip, ...]:
        """Every flip that was still open, oldest first."""
        ...

    async def remove_active_flip(self, token: str) -> bool:
        """Forget one flip's row."""
        ...


@runtime_checkable
class SweepGateway(Protocol):
    """What the sweep needs to say its piece. :class:`~esgbot.telegram.TelegramGateway` is one."""

    async def post(self, target: MessageTarget, view: FlipView) -> int:
        """Post a message and return its id."""
        ...

    async def remove_buttons(self, target: MessageTarget, message_id: int) -> None:
        """Take the buttons off a message, leaving its text alone."""
        ...


async def void_leftover_flips(*, store: SweepStore, gateway: SweepGateway) -> int:
    """Void every flip left open by the last run, and return how many there were.

    A database that will not answer means the sweep does nothing and says so;
    there is nothing to void if the rows cannot be read, and refusing to start
    over it would leave the bot down for a problem it cannot fix.
    """
    try:
        leftovers = await store.list_active_flips()
    except (StorageError, sqlite3.Error) as error:
        _logger.error("could not read the flips left open by the last run: %s", error)
        return 0
    if not leftovers:
        _logger.info("no flips were left open by the last run")
        return 0
    _logger.info("voiding %d flip(s) left open by the last run", len(leftovers))
    for row in leftovers:
        await _void(gateway, row)
        await _forget(store, row)
    return len(leftovers)


async def _void(gateway: SweepGateway, row: ActiveFlip) -> None:
    """Close one leftover flip's message and tell its group.

    The seed comes straight off the row, which is ``None`` for a lobby that never
    dealt and set for one that had started, so the notice reveals exactly as much
    as there is to reveal.
    """
    target = MessageTarget(group_id=row.group_id, topic_id=row.topic_id)
    notice = VoidView(token=row.token, reason=VoidReason.RESTART, seed=row.seed)
    try:
        await gateway.remove_buttons(target, row.message_id)
        await gateway.post(target, notice)
    except PermanentFailureError:
        _logger.warning("flip %s: could not tell group %d it was voided", row.token, row.group_id)


async def _forget(store: SweepStore, row: ActiveFlip) -> None:
    """Drop the row. A row that stays only costs the next start another notice."""
    try:
        await store.remove_active_flip(row.token)
    except (StorageError, sqlite3.Error) as error:
        _logger.error("flip %s: could not clear its row: %s", row.token, error)

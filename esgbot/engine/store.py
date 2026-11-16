"""The four storage calls a flip makes, as an interface it can be tested without.

:class:`esgbot.storage.Database` satisfies this as it is; the engine takes the
narrower type so that what it touches is written down, and so a test can hand it
a store that fails on purpose. Reading flips back, the stats queries and the
startup sweep are nobody's business here.

How the engine treats a failure in each call:

=============================  ==============================================================
:meth:`~FlipStore.add_active_flip`     Raised on to the caller. The flip is not created.
:meth:`~FlipStore.update_active_flip`  Logged, except for the one write that stores the
                                       seed: the commitment is a promise to reveal it, so a
                                       round that cannot store it is voided before anything
                                       is published. A stale message id or group id is only
                                       logged -- it costs a voided flip after a restart, or
                                       that void notice the chat the flip moved to, which is
                                       not worth losing a live flip over.
:meth:`~FlipStore.record_flip`         Logged, and the result is still posted, saying the
                                       flip was not recorded and showing no number.
:meth:`~FlipStore.remove_active_flip`  Logged. The next restart voids the leftover row.
=============================  ==============================================================

A failure arrives either as :class:`~esgbot.storage.StorageError` or, when the
database itself refuses, as :class:`sqlite3.Error`; the engine catches both.

:meth:`~FlipStore.record_flip` takes the flip's token and retires its
in-progress row in the same transaction. That is what makes recording a flip one
decision rather than two: a flip that has been recorded has no row, so the next
start's sweep cannot find it and announce it voided, and the engine never has to
guess which of the two writes got through. A cancelled write may still have
committed -- the worker thread cannot be called back -- so the engine waits for
it rather than assuming either way.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from esgbot.storage import ActiveFlip, FinishedFlip, FlipState

__all__ = ["FlipStore"]


@runtime_checkable
class FlipStore(Protocol):
    """Storage, as the engine sees it."""

    async def record_flip(self, flip: FinishedFlip, *, token: str | None = None) -> int:
        """Record a finished flip with its deal and players, and return its public number.

        ``token`` is the flip's in-progress token, whose row is retired in the
        same transaction.
        """
        ...

    async def add_active_flip(self, active: ActiveFlip) -> None:
        """Write the row that says this flip is in progress."""
        ...

    async def update_active_flip(
        self,
        token: str,
        *,
        message_id: int | None = None,
        state: FlipState | None = None,
        seed: bytes | None = None,
        group_id: int | None = None,
    ) -> bool:
        """Change an in-progress flip's message id, state, seed or group id.

        Returns ``False`` if no flip has this token any more.
        """
        ...

    async def remove_active_flip(self, token: str) -> bool:
        """Forget an in-progress flip. Returns ``False`` if it was already gone."""
        ...

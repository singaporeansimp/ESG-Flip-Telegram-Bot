"""The async face of storage: one database, one connection, one call at a time.

Everything the bot does with the database goes through :class:`Database`. Its
methods are the operations in :mod:`esgbot.storage.queries`, each run in a worker
thread with :func:`asyncio.to_thread` so a disk write never blocks Telegram
polling or another group's timers, and serialised behind a lock so a transaction
is never interleaved with anything else. sqlite is fast enough for one flip at a
time per group; the point of the thread is to keep the event loop free, not to
get parallel writes.

Opening the database applies any outstanding schema migrations::

    database = await Database.open(path)
    try:
        number = await database.record_flip(flip)
    finally:
        await database.close()

Every method raises what the matching function in :mod:`esgbot.storage.queries`
raises: :class:`sqlite3.Error` if the database itself fails, and
:class:`~esgbot.storage.errors.CorruptRecordError` from a read whose row will not
parse. Calling a method after :meth:`close` raises :class:`sqlite3.ProgrammingError`.

Cancelling a call does not cancel the work: the worker thread is already in
sqlite and cannot be interrupted, so the call waits for it, keeps the connection
to itself until it is done, and only then raises
:exc:`asyncio.CancelledError`. A cancelled write may therefore have committed,
and the caller has to treat it as such -- which is why a flip's result is
recorded and its in-progress row retired by the one call.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from os import PathLike
from types import TracebackType
from typing import Self, TypeVar

from esgbot.game.rules import FlipRequest
from esgbot.logging_setup import get_logger
from esgbot.storage import queries, schema
from esgbot.storage.models import (
    ActiveFlip,
    AutoJoiner,
    FinishedFlip,
    FlipState,
    GroupMigration,
    PlayerStats,
    StoredFlip,
)
from esgbot.tasks import see_through

__all__ = ["Database"]

_T = TypeVar("_T")

_logger = get_logger(__name__)


class Database:
    """The bot's database. Build one with :meth:`open`, and close it when done.

    One instance belongs to one event loop, because the lock that serialises its
    calls attaches to the loop it is first awaited in. The bot has a single loop
    so this never comes up there; a test that reaches for two
    :func:`asyncio.run` calls should do the whole scenario inside one instead.
    Opening a second ``Database`` on the same file is fine, and is how a test
    checks that rows survive a close and reopen.
    """

    __slots__ = ("_connection", "_lock", "_path")

    def __init__(self, connection: sqlite3.Connection, *, path: str) -> None:
        """Wrap an open, migrated connection. Prefer :meth:`open`."""
        self._connection = connection
        self._lock = asyncio.Lock()
        self._path = path

    @classmethod
    async def open(cls, path: str | PathLike[str]) -> Self:
        """Open the database file, bringing its schema up to date.

        Creates the file if it is not there. ``":memory:"`` works too, which is
        what tests that do not care about the file use.

        Raises:
            SchemaTooNewError: if the file was written by a newer bot version.
            sqlite3.Error: if the file cannot be opened or migrated.
        """

        def setup() -> sqlite3.Connection:
            connection = schema.connect(path)
            try:
                schema.apply_migrations(connection)
            except BaseException:
                connection.close()
                raise
            return connection

        connection = await asyncio.to_thread(setup)
        _logger.info("opened database %s at schema version %d", path, schema.SCHEMA_VERSION)
        return cls(connection, path=str(path))

    @property
    def path(self) -> str:
        """The file this database was opened from, for log messages."""
        return self._path

    async def close(self) -> None:
        """Close the connection. Closing twice is harmless."""
        await self._run(lambda connection: connection.close())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _run(self, work: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run one blocking operation in a worker thread, alone.

        The lock is held until the worker has really finished, cancellation
        included. :func:`asyncio.to_thread` cannot interrupt a thread that is
        already in sqlite, so a cancelled call that let the lock go would leave
        that worker using the connection while the next one opened a transaction
        on it -- "cannot start a transaction within a transaction" -- or while
        :meth:`close` closed it underneath. A cancelled call therefore waits here
        and only then raises :exc:`asyncio.CancelledError`, and the work it
        started may well have committed by that point: see
        :func:`esgbot.tasks.see_through`.
        """
        async with self._lock:
            worker = asyncio.ensure_future(asyncio.to_thread(work, self._connection))
            return await see_through(worker)

    async def record_flip(self, flip: FinishedFlip, *, token: str | None = None) -> int:
        """Record a finished flip and its players, and return its public number.

        The whole flip is written in one transaction: either it is all there with
        a number, or none of it is.

        ``token`` is the flip's in-progress token, whose active-flip row is
        removed in that same transaction, so a flip that has been recorded never
        leaves a row behind for the next start to void. Leave it out when there
        is no such row.
        """
        return await self._run(
            lambda connection: queries.record_flip(connection, flip, token=token)
        )

    async def fetch_flip(self, *, group_id: int, flip_number: int) -> StoredFlip | None:
        """One finished flip with its players and stored deal, or ``None``.

        A flip number that belongs to another group reads as missing, which is
        what ``/verify`` reports for it.

        Raises:
            CorruptRecordError: if the stored flip will not read back.
        """
        return await self._run(
            lambda connection: queries.fetch_flip(
                connection, group_id=group_id, flip_number=flip_number
            )
        )

    async def last_flip_request(self, group_id: int) -> FlipRequest | None:
        """What the last flip this group finished was asked for, or ``None``.

        The settings as they were typed, which is what ``/again`` repeats: a
        value the command left out comes back left out.

        Raises:
            CorruptRecordError: if the stored row will not read back.
        """
        return await self._run(lambda connection: queries.last_flip_request(connection, group_id))

    async def add_active_flip(self, active: ActiveFlip) -> None:
        """Write the row that says this flip is in progress."""
        await self._run(lambda connection: queries.add_active_flip(connection, active))

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

        Arguments left out are left as they are. Returns ``False`` if no flip has
        this token any more.
        """
        return await self._run(
            lambda connection: queries.update_active_flip(
                connection,
                token,
                message_id=message_id,
                state=state,
                seed=seed,
                group_id=group_id,
            )
        )

    async def remove_active_flip(self, token: str) -> bool:
        """Forget an in-progress flip. Returns ``False`` if it was already gone."""
        return await self._run(lambda connection: queries.remove_active_flip(connection, token))

    async def list_active_flips(self) -> tuple[ActiveFlip, ...]:
        """Every in-progress flip, oldest first: the sweep a restart begins with.

        Raises:
            CorruptRecordError: if one of the rows will not read back.
        """
        return await self._run(queries.list_active_flips)

    async def leaderboard(
        self, group_id: int, *, limit: int | None = queries.DEFAULT_LEADERBOARD_LIMIT
    ) -> tuple[PlayerStats, ...]:
        """One group's players ranked by net winnings, then wins, then name.

        Raises:
            CorruptRecordError: if a player's totals will not read back.
        """
        return await self._run(
            lambda connection: queries.leaderboard(connection, group_id, limit=limit)
        )

    async def player_stats(self, *, group_id: int, user_id: int) -> PlayerStats | None:
        """One player's figures in one group, or ``None`` if they never played there.

        Raises:
            CorruptRecordError: if their totals will not read back.
        """
        return await self._run(
            lambda connection: queries.player_stats(connection, group_id=group_id, user_id=user_id)
        )

    async def player_stats_by_username(self, *, group_id: int, username: str) -> PlayerStats | None:
        """The same, found by the username the player last used in this group.

        Returns ``None`` when no flip in the group recorded that name.

        Raises:
            CorruptRecordError: if their totals will not read back.
        """
        return await self._run(
            lambda connection: queries.player_stats_by_username(
                connection, group_id=group_id, username=username
            )
        )

    async def set_autojoin(self, *, group_id: int, joiner: AutoJoiner) -> None:
        """Put this player into this group's flips up to ``joiner.max_wager``."""
        await self._run(
            lambda connection: queries.set_autojoin(connection, group_id=group_id, joiner=joiner)
        )

    async def clear_autojoin(self, *, group_id: int, user_id: int) -> bool:
        """Stop auto-joining this player here. ``False`` if they never asked."""
        return await self._run(
            lambda connection: queries.clear_autojoin(
                connection, group_id=group_id, user_id=user_id
            )
        )

    async def autojoin_limit(self, *, group_id: int, user_id: int) -> int | None:
        """The wager this player auto-joins up to in this group, or ``None``."""
        return await self._run(
            lambda connection: queries.autojoin_limit(
                connection, group_id=group_id, user_id=user_id
            )
        )

    async def autojoiners(self, *, group_id: int, wager: int) -> tuple[AutoJoiner, ...]:
        """Who auto-joins a flip at this wager in this group, first asker first."""
        return await self._run(
            lambda connection: queries.autojoiners(connection, group_id=group_id, wager=wager)
        )

    async def migrate_group_id(self, *, old_group_id: int, new_group_id: int) -> GroupMigration:
        """Move a group's history and open flips to the id Telegram now uses."""
        return await self._run(
            lambda connection: queries.migrate_group_id(
                connection, old_group_id=old_group_id, new_group_id=new_group_id
            )
        )

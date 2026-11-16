"""SQLite storage: finished flips, flips in progress, and the stats questions.

The database answers four kinds of question, and nothing else knows any SQL:

* **Record a flip.** :meth:`Database.record_flip` writes a finished flip with its
  players and its full deal in one transaction and returns the public flip
  number. The number is handed out by that insert, so a flip has one only once it
  is saved. Cancelled and voided flips are never written, which is why they never
  show up in stats.
* **Track flips in progress.** :meth:`Database.add_active_flip`,
  :meth:`Database.update_active_flip` and :meth:`Database.remove_active_flip`
  keep one row per open flip, keyed by its internal token.
  :meth:`Database.list_active_flips` is the sweep a restart starts with: whatever
  is still there was interrupted and gets voided.
* **Stats.** :meth:`Database.leaderboard`, :meth:`Database.player_stats` and
  :meth:`Database.player_stats_by_username`, all scoped to one group. A win is a
  flip the player's net came out above zero, so a chop winner who nets $0 played
  without winning. The name shown is the one from that player's most recent flip
  in the group.
* **Read one flip back.** :meth:`Database.fetch_flip` returns a flip with its
  players and stored deal, which is what ``/verify`` re-runs.
* **Repeat a group's last flip.** :meth:`Database.last_flip_request` gives the
  settings of the last flip a group finished, as they were typed, which is what
  ``/again`` opens a new lobby with. One row per group, written by
  :meth:`Database.record_flip` in the same transaction as the flip that set it.

:meth:`Database.migrate_group_id` moves a group's rows when Telegram turns it
into a supergroup, so stats and its last settings carry over.

Values keep their exact types: points are :class:`fractions.Fraction`, money is
whole-dollar :class:`int`, times are timezone-aware, and seeds are the same 32
raw bytes the rest of the game uses. The fraction strings and hex in the file are
an encoding detail of :mod:`esgbot.storage.queries`.

A row that cannot be read back as what it should be raises
:class:`CorruptRecordError` rather than coming back half-built, so a hand-edited
or truncated database is a loud failure and never a flip that looks real.
"""

from __future__ import annotations

from esgbot.storage.database import Database
from esgbot.storage.errors import CorruptRecordError, SchemaTooNewError, StorageError
from esgbot.storage.models import (
    ActiveFlip,
    FinishedFlip,
    FlipPlayer,
    FlipState,
    GroupMigration,
    PlayerStats,
    StoredFlip,
    normalize_username,
)
from esgbot.storage.queries import DEFAULT_LEADERBOARD_LIMIT
from esgbot.storage.schema import SCHEMA_VERSION

__all__ = [
    "DEFAULT_LEADERBOARD_LIMIT",
    "SCHEMA_VERSION",
    "ActiveFlip",
    "CorruptRecordError",
    "Database",
    "FinishedFlip",
    "FlipPlayer",
    "FlipState",
    "GroupMigration",
    "PlayerStats",
    "SchemaTooNewError",
    "StorageError",
    "StoredFlip",
    "normalize_username",
]

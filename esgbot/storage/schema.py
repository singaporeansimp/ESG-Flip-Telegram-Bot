"""The database file: how it is opened, and the tables inside it.

Three tables, all described in the Tech Plan's data model:

``flips``
    One row per finished flip, holding its settings, the seed and commitment, the
    full deal as JSON and when it started and finished. The primary key is the
    public flip number, so it is handed out by the insert and never reused.
``flip_players``
    One row per seat in a finished flip, with the player's name as it was at the
    time, their exact points, their net dollars and whether they scooped.
``active_flips``
    One row per flip in progress, keyed by the flip's internal token. A restart
    reads whatever is left here and voids it.

Only finished flips reach ``flips``, so cancelled and voided flips are absent
from stats by construction rather than by a filter.

The schema version lives in ``PRAGMA user_version``. :func:`apply_migrations`
walks it forward one step at a time, inside a transaction, which leaves room to
add a step later without a migration framework. A file written by a newer version
is refused rather than guessed at.

The CHECK constraints repeat the settings limits from :mod:`esgbot.game.rules`.
They are a backstop against a bug writing nonsense, not the place those limits
are defined.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from os import PathLike
from typing import Final

from esgbot.game.fairness import SEED_SIZE
from esgbot.game.rules import (
    WAGER_MAX,
    WAGER_MIN,
    X_MAX,
    X_MIN,
    Y_MAX,
    Y_MIN,
    Z_MAX,
    Z_MIN,
)
from esgbot.logging_setup import get_logger
from esgbot.storage.errors import SchemaTooNewError
from esgbot.storage.models import COMMITMENT_LENGTH, FlipState

__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "apply_migrations",
    "connect",
    "schema_version",
]

SCHEMA_VERSION: Final = 1
"""The newest schema version this code understands."""

BUSY_TIMEOUT_MS: Final = 5_000
"""How long a statement waits for a lock before giving up."""

_SEED_HEX_LENGTH: Final = SEED_SIZE * 2

_STATE_VALUES: Final = ", ".join(f"'{state.value}'" for state in FlipState)

_MIGRATION_1: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE flips (
        flip_number  INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id     INTEGER NOT NULL,
        wager        INTEGER NOT NULL CHECK (wager BETWEEN {WAGER_MIN} AND {WAGER_MAX}),
        x            INTEGER NOT NULL CHECK (x BETWEEN {X_MIN} AND {X_MAX}),
        y            INTEGER NOT NULL CHECK (y BETWEEN {Y_MIN} AND {Y_MAX}),
        z            INTEGER NOT NULL CHECK (z BETWEEN {Z_MIN} AND {Z_MAX}),
        player_count INTEGER NOT NULL CHECK (player_count >= 1),
        button_seat  INTEGER NOT NULL CHECK (button_seat >= 0 AND button_seat < player_count),
        seed         TEXT NOT NULL CHECK (length(seed) = {_SEED_HEX_LENGTH}),
        commitment   TEXT NOT NULL CHECK (length(commitment) = {COMMITMENT_LENGTH}),
        deal         TEXT NOT NULL,
        started_at   TEXT NOT NULL,
        finished_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE flip_players (
        flip_number  INTEGER NOT NULL REFERENCES flips(flip_number) ON DELETE CASCADE,
        user_id      INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        username     TEXT,
        seat         INTEGER NOT NULL CHECK (seat >= 0),
        points       TEXT NOT NULL,
        net_dollars  INTEGER NOT NULL,
        scooped      INTEGER NOT NULL CHECK (scooped IN (0, 1)),
        PRIMARY KEY (flip_number, seat)
    )
    """,
    f"""
    CREATE TABLE active_flips (
        token      TEXT PRIMARY KEY,
        group_id   INTEGER NOT NULL,
        topic_id   INTEGER,
        message_id INTEGER NOT NULL,
        state      TEXT NOT NULL CHECK (state IN ({_STATE_VALUES})),
        seed       TEXT CHECK (seed IS NULL OR length(seed) = {_SEED_HEX_LENGTH}),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX flips_by_group ON flips(group_id, flip_number)",
    "CREATE UNIQUE INDEX flip_players_by_user ON flip_players(flip_number, user_id)",
    "CREATE INDEX flip_players_by_player ON flip_players(user_id, flip_number)",
    "CREATE INDEX flip_players_by_username ON flip_players(username, flip_number)",
    # One active flip per group is the engine's rule, not the database's: a row
    # being removed can briefly overlap the next one, so this index only makes
    # the startup sweep and the group-id migration cheap.
    "CREATE INDEX active_flips_by_group ON active_flips(group_id)",
)

_MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (_MIGRATION_1,)

_logger = get_logger(__name__)


def connect(path: str | PathLike[str]) -> sqlite3.Connection:
    """Open the database file and put the connection in the shape queries expect.

    WAL journalling, foreign keys on, rows as :class:`sqlite3.Row`, and autocommit
    (``isolation_level=None``) so transactions are the explicit ``BEGIN``/``COMMIT``
    pairs in :mod:`esgbot.storage.queries` rather than implicit ones.

    The connection is usable from more than one thread, because every call runs in
    whichever worker thread :class:`~esgbot.storage.database.Database` hands it to.
    That class serialises the calls; nothing else should share the connection.

    ``":memory:"`` works, which is handy in tests, but the file gets no WAL.
    """
    connection = sqlite3.connect(
        path,
        isolation_level=None,
        check_same_thread=False,
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    """The schema version recorded in the file. A fresh file reports ``0``."""
    row: Sequence[int] = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Bring the file up to :data:`SCHEMA_VERSION` and return the version reached.

    Each step runs in its own transaction together with the version bump, so an
    interrupted upgrade leaves the file on the last version that completed. A file
    already at the current version is left alone.

    Raises:
        SchemaTooNewError: if the file's version is newer than this code supports.
    """
    found = schema_version(connection)
    if found > SCHEMA_VERSION:
        raise SchemaTooNewError(found=found, supported=SCHEMA_VERSION)
    for step, statements in enumerate(_MIGRATIONS[found:], start=found + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {step}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        _logger.info("applied database schema version %d", step)
    return SCHEMA_VERSION

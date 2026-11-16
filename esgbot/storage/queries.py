"""Every statement the bot runs, as plain synchronous functions.

Each one takes an open :class:`sqlite3.Connection` and blocks. The async face is
:class:`esgbot.storage.database.Database`, which runs these in a worker thread;
they are separate so the SQL can be read, and tested, without an event loop.

Two conversions happen here and nowhere else:

* **Points** are exact rational numbers in code and fraction strings in the
  database: :class:`~fractions.Fraction` ``1/3`` is stored as ``"1/3"``. Money is
  whole dollars, stored as integers.
* **Times** are timezone-aware :class:`~datetime.datetime` values in code and
  ISO-8601 UTC text in the database.

A row that will not convert back is damaged, not merely unexpected, so reads
raise :class:`~esgbot.storage.errors.CorruptRecordError` instead of returning a
half-built value. That is the only way a read fails short of sqlite itself
failing: a flip that is simply not there comes back as ``None``.

A column's declared type is no guarantee of what came out of it. sqlite's
INTEGER affinity keeps a value it cannot store as an integer -- ``wager = 50.9``
stays a float -- and ``int()`` would hand that back as a plausible 50. So every
whole number is read with :func:`_whole`, which takes the stored type as it
finds it and fails the row otherwise, and the figures a flip row repeats about
its own deal are checked against the deal.

A flip also stores which of its settings the command left to the bot, as one 0
or 1 per value, and the group's own ``group_last_flip`` row holds the same
settings and flags for the last flip it finished -- that row is what ``/again``
reads, and :func:`record_flip` writes it in the same transaction as the flip
that set it. On the way back out a flag is read as strictly as a number is: a
column holding anything but 0 or 1 fails the row.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Final

from esgbot.game.deal import Deal
from esgbot.game.fairness import seed_from_hex, seed_to_hex
from esgbot.game.rules import FlipRequest, Settings
from esgbot.logging_setup import get_logger
from esgbot.storage.errors import CorruptRecordError
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

__all__ = [
    "DEFAULT_LEADERBOARD_LIMIT",
    "add_active_flip",
    "fetch_flip",
    "last_flip_request",
    "leaderboard",
    "list_active_flips",
    "migrate_group_id",
    "player_stats",
    "player_stats_by_username",
    "record_flip",
    "remove_active_flip",
    "transaction",
    "update_active_flip",
]

DEFAULT_LEADERBOARD_LIMIT: Final = 20
"""Rows the leaderboard returns unless the caller asks for a different number."""

_logger = get_logger(__name__)

_INSERT_FLIP: Final = """
INSERT INTO flips (
    group_id, wager, x, y, z, y_defaulted, z_defaulted, player_count, button_seat,
    seed, commitment, deal, started_at, finished_at
) VALUES (
    :group_id, :wager, :x, :y, :z, :y_defaulted, :z_defaulted, :player_count, :button_seat,
    :seed, :commitment, :deal, :started_at, :finished_at
)
"""

# One row per group, replaced by every flip that group finishes: the settings as
# they were typed, which is what ``/again`` repeats. ``updated_at`` is the flip's
# own finishing time, so the row says when it was last set -- which is what
# decides the merge if a group that already has one is given a new id.
_UPSERT_LAST_FLIP: Final = """
INSERT INTO group_last_flip (
    group_id, wager, x, y, z, y_defaulted, z_defaulted, updated_at
) VALUES (
    :group_id, :wager, :x, :y, :z, :y_defaulted, :z_defaulted, :updated_at
)
ON CONFLICT(group_id) DO UPDATE SET
    wager = excluded.wager,
    x = excluded.x,
    y = excluded.y,
    z = excluded.z,
    y_defaulted = excluded.y_defaulted,
    z_defaulted = excluded.z_defaulted,
    updated_at = excluded.updated_at
"""

_SELECT_LAST_FLIP: Final = """
SELECT wager, x, y, z, y_defaulted, z_defaulted
FROM group_last_flip
WHERE group_id = :group_id
"""

_INSERT_PLAYER: Final = """
INSERT INTO flip_players (
    flip_number, user_id, display_name, username, seat, points, net_dollars, scooped
) VALUES (
    :flip_number, :user_id, :display_name, :username, :seat, :points, :net_dollars, :scooped
)
"""

_SELECT_FLIP: Final = """
SELECT flip_number, group_id, wager, x, y, z, y_defaulted, z_defaulted,
       player_count, button_seat, seed, commitment, deal, started_at, finished_at
FROM flips
WHERE group_id = :group_id AND flip_number = :flip_number
"""

_SELECT_FLIP_PLAYERS: Final = """
SELECT user_id, display_name, username, seat, points, net_dollars, scooped
FROM flip_players
WHERE flip_number = :flip_number
ORDER BY seat
"""

_INSERT_ACTIVE: Final = """
INSERT INTO active_flips (token, group_id, topic_id, message_id, state, seed, created_at)
VALUES (:token, :group_id, :topic_id, :message_id, :state, :seed, :created_at)
"""

_SELECT_ACTIVE: Final = """
SELECT token, group_id, topic_id, message_id, state, seed, created_at
FROM active_flips
ORDER BY created_at, token
"""

_DELETE_ACTIVE: Final = "DELETE FROM active_flips WHERE token = :token"

# Totals for every player in one group, with the flip number of each player's most
# recent flip there, which is where the name to show comes from.
#
# The last two columns count the rows the sums above would otherwise read as
# zero: sqlite adds text up as nothing and a stray fraction as a fraction, so a
# tampered-with amount or scoop flag would come back as a figure that looks
# real. A comparison is 1 or 0 in sqlite, so summing one counts the rows it is
# true of. ``net_dollars`` is any whole number, so its own type is the test; a
# scoop flag has only two allowed values, and nothing but the integers 0 and 1
# equals either -- text and blobs sort above every number, and a real 0.0 or 1.0
# is stored as an integer by the column's affinity -- so the range is the test
# there and it catches a 2 as well. Both cost nothing on top of the scan the
# sums already do, and :func:`_player_stats` fails the read on them.
_TOTALS: Final = """
WITH totals AS (
    SELECT
        p.user_id                                          AS user_id,
        SUM(p.net_dollars)                                 AS net_dollars,
        COUNT(*)                                           AS flips_played,
        SUM(CASE WHEN p.net_dollars > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(p.scooped)                                     AS scoops,
        SUM(typeof(p.net_dollars) <> 'integer')            AS damaged_dollars,
        SUM(p.scooped NOT IN (0, 1))                       AS damaged_scoops,
        MAX(p.flip_number)                                 AS last_flip
    FROM flip_players AS p
    JOIN flips AS f ON f.flip_number = p.flip_number
    WHERE f.group_id = :group_id{extra}
    GROUP BY p.user_id
)
SELECT
    totals.user_id         AS user_id,
    totals.net_dollars     AS net_dollars,
    totals.flips_played    AS flips_played,
    totals.wins            AS wins,
    totals.scoops          AS scoops,
    totals.damaged_dollars AS damaged_dollars,
    totals.damaged_scoops  AS damaged_scoops,
    latest.display_name    AS display_name,
    latest.username        AS username
FROM totals
JOIN flip_players AS latest
  ON latest.flip_number = totals.last_flip AND latest.user_id = totals.user_id
"""

_SELECT_GROUP_TOTALS: Final = _TOTALS.format(extra="")
_SELECT_PLAYER_TOTALS: Final = _TOTALS.format(extra=" AND p.user_id = :user_id")

# The most recent flip in this group that recorded this username. Telegram treats
# usernames case-insensitively, and they are ASCII, so NOCASE is the right match.
_SELECT_USER_BY_USERNAME: Final = """
SELECT p.user_id AS user_id
FROM flip_players AS p
JOIN flips AS f ON f.flip_number = p.flip_number
WHERE f.group_id = :group_id AND p.username = :username COLLATE NOCASE
ORDER BY p.flip_number DESC
LIMIT 1
"""


@contextmanager
def transaction(connection: sqlite3.Connection, *, mode: str = "IMMEDIATE") -> Iterator[None]:
    """Run a block in one transaction, committing it or rolling all of it back.

    The connection is in autocommit mode, so transactions are only ever the ones
    opened here. Writes use the default ``IMMEDIATE`` so they take the write lock
    up front; multi-statement reads pass ``mode="DEFERRED"`` to see one snapshot.
    """
    connection.execute(f"BEGIN {mode}")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def record_flip(
    connection: sqlite3.Connection, flip: FinishedFlip, *, token: str | None = None
) -> int:
    """Record a finished flip and its players, and return its public flip number.

    The flip row and every player row go in one transaction, so a failure part
    way through leaves nothing behind and no number is consumed for it. The
    number comes from the insert, which is why it is only known once the flip is
    saved.

    ``token`` is the flip's in-progress token. Its ``active_flips`` row is
    deleted inside the same transaction, which is what makes "this flip was
    recorded" and "this flip is no longer in progress" one fact rather than two:
    the next start sweeps whatever rows are left, and a recorded flip never
    leaves one for it to announce as voided. Leave it out when there is no such
    row -- nothing is deleted then, and a token whose row has already gone is
    not an error either.

    The group's ``group_last_flip`` row is written in that same transaction, so
    the settings ``/again`` repeats are always a flip this group really
    finished: a write that fails leaves the old row exactly as it was, and a
    cancelled or voided flip, which is never recorded at all, never touches it.

    Usernames are stored in the normalised form of
    :func:`~esgbot.storage.models.normalize_username`.

    Raises:
        sqlite3.Error: if the write fails. Nothing was recorded and the row, if
            there was one, is still there. The caller still has a result to
            post; the Tech Plan has it say the flip was not recorded.
    """
    settings = {
        "wager": flip.settings.wager,
        "x": flip.settings.x,
        "y": flip.settings.y,
        "z": flip.settings.z,
        "y_defaulted": int(flip.y_defaulted),
        "z_defaulted": int(flip.z_defaulted),
    }
    with transaction(connection):
        cursor = connection.execute(
            _INSERT_FLIP,
            {
                "group_id": flip.group_id,
                **settings,
                "player_count": flip.player_count,
                "button_seat": flip.button_seat,
                "seed": seed_to_hex(flip.seed),
                "commitment": flip.commitment,
                "deal": flip.deal.to_json(),
                "started_at": _time_to_text(flip.started_at),
                "finished_at": _time_to_text(flip.finished_at),
            },
        )
        flip_number = cursor.lastrowid
        if flip_number is None:  # pragma: no cover - sqlite always reports it
            raise sqlite3.DatabaseError("sqlite did not report a flip number for the new flip")
        connection.executemany(
            _INSERT_PLAYER,
            [
                {
                    "flip_number": flip_number,
                    "user_id": player.user_id,
                    "display_name": player.display_name,
                    "username": normalize_username(player.username),
                    "seat": player.seat,
                    "points": str(player.points),
                    "net_dollars": player.net_dollars,
                    "scooped": int(player.scooped),
                }
                for player in flip.players
            ],
        )
        connection.execute(
            _UPSERT_LAST_FLIP,
            {
                "group_id": flip.group_id,
                **settings,
                "updated_at": _time_to_text(flip.finished_at),
            },
        )
        if token is not None:
            connection.execute(_DELETE_ACTIVE, {"token": token})
    _logger.info(
        "recorded flip %d in group %d with %d players",
        flip_number,
        flip.group_id,
        flip.player_count,
    )
    return flip_number


def fetch_flip(
    connection: sqlite3.Connection, *, group_id: int, flip_number: int
) -> StoredFlip | None:
    """One finished flip with its players and stored deal, for ``/verify``.

    The group is part of the lookup, so a flip number from another group reads as
    missing.

    Returns:
        The flip, or ``None`` if this group has no flip with that number.

    Raises:
        CorruptRecordError: if the row is there but its seed, deal, points, times
            or player list will not read back. Nothing partial is returned.
    """
    with transaction(connection, mode="DEFERRED"):
        row = connection.execute(
            _SELECT_FLIP, {"group_id": group_id, "flip_number": flip_number}
        ).fetchone()
        if row is None:
            return None
        player_rows = connection.execute(
            _SELECT_FLIP_PLAYERS, {"flip_number": flip_number}
        ).fetchall()
    return _stored_flip(row, player_rows)


def last_flip_request(connection: sqlite3.Connection, group_id: int) -> FlipRequest | None:
    """What the last flip this group finished was asked for, for ``/again``.

    The settings as they were typed: a ``Y`` or ``Z`` the command left out comes
    back left out, so repeating the request fills it in again for whoever is at
    the table this time. The figure beside a flag is what that value was filled
    in with at the time and is not read.

    Groups never mix: this is one row per group, written by the flips that group
    finished and by nothing else.

    Returns:
        The request, or ``None`` if this group has never finished a flip.

    Raises:
        CorruptRecordError: if the row is there but will not read back.
    """
    row = connection.execute(_SELECT_LAST_FLIP, {"group_id": group_id}).fetchone()
    if row is None:
        return None
    try:
        settings = _settings_from(row)
        return FlipRequest.from_settings(
            settings,
            y_defaulted=_flag(row["y_defaulted"], "y_defaulted"),
            z_defaulted=_flag(row["z_defaulted"], "z_defaulted"),
        )
    except (ValueError, TypeError) as error:
        record = f"the last flip settings of group {group_id}"
        _logger.warning("%s cannot be read back: %s", record, error)
        raise CorruptRecordError(record, str(error)) from error


def add_active_flip(connection: sqlite3.Connection, active: ActiveFlip) -> None:
    """Write the row that says this flip is in progress.

    Raises:
        sqlite3.IntegrityError: if a row with this token already exists.
    """
    connection.execute(
        _INSERT_ACTIVE,
        {
            "token": active.token,
            "group_id": active.group_id,
            "topic_id": active.topic_id,
            "message_id": active.message_id,
            "state": active.state.value,
            "seed": None if active.seed is None else seed_to_hex(active.seed),
            "created_at": _time_to_text(active.created_at),
        },
    )


def update_active_flip(
    connection: sqlite3.Connection,
    token: str,
    *,
    message_id: int | None = None,
    state: FlipState | None = None,
    seed: bytes | None = None,
    group_id: int | None = None,
) -> bool:
    """Change an in-progress flip: a reposted message, Start, the seed, or its group.

    Only the arguments that are given change; ``None`` means "leave it as it is".
    None of these fields is ever cleared back to NULL, so nothing is lost by that
    reading: a message id, a state and a group id always have values, and a seed
    is set once at Start and never unset.

    ``group_id`` is the one field :func:`migrate_group_id` also moves. It is here
    so one flip's row can follow that flip by token, which is how the engine keeps
    a live flip's row pointing at the chat the flip is actually playing in.

    Returns:
        ``True`` if a row was updated, ``False`` if no flip has this token, which
        is what a flip that has already been voided looks like.

    Raises:
        ValueError: if no change was asked for.
    """
    assignments: list[str] = []
    parameters: dict[str, Any] = {"token": token}
    if message_id is not None:
        assignments.append("message_id = :message_id")
        parameters["message_id"] = message_id
    if state is not None:
        assignments.append("state = :state")
        parameters["state"] = state.value
    if seed is not None:
        assignments.append("seed = :seed")
        parameters["seed"] = seed_to_hex(seed)
    if group_id is not None:
        assignments.append("group_id = :group_id")
        parameters["group_id"] = group_id
    if not assignments:
        raise ValueError(
            "update_active_flip needs at least one of message_id, state, seed or group_id"
        )
    cursor = connection.execute(
        f"UPDATE active_flips SET {', '.join(assignments)} WHERE token = :token", parameters
    )
    return cursor.rowcount > 0


def remove_active_flip(connection: sqlite3.Connection, token: str) -> bool:
    """Forget an in-progress flip, because it finished, was cancelled or was voided.

    Returns:
        ``True`` if a row was removed, ``False`` if there was nothing to remove.
        Removing twice is not an error.
    """
    cursor = connection.execute(_DELETE_ACTIVE, {"token": token})
    return cursor.rowcount > 0


def list_active_flips(connection: sqlite3.Connection) -> tuple[ActiveFlip, ...]:
    """Every in-progress flip, oldest first. This is the startup sweep.

    Raises:
        CorruptRecordError: if a row's state, seed or time will not read back.
    """
    return tuple(_active_flip(row) for row in connection.execute(_SELECT_ACTIVE).fetchall())


def leaderboard(
    connection: sqlite3.Connection,
    group_id: int,
    *,
    limit: int | None = DEFAULT_LEADERBOARD_LIMIT,
) -> tuple[PlayerStats, ...]:
    """One group's players, best first, for ``/leaderboard``.

    Ranked by net winnings, then wins, then name, with the user id settling the
    last tie so the same history always gives the same order. The name compared,
    and shown, is the one from that player's most recent flip in the group.

    Args:
        group_id: the group to report on. Groups never mix.
        limit: how many rows to return at most, or ``None`` for all of them.

    Returns:
        The top rows, or an empty tuple for a group with no finished flips.

    Raises:
        ValueError: if ``limit`` is negative.
        CorruptRecordError: if a player's totals will not read back. The whole
            board fails rather than quietly leaving one player out of it.
    """
    if limit is not None and limit < 0:
        raise ValueError(f"limit cannot be negative, got {limit}")
    rows = connection.execute(_SELECT_GROUP_TOTALS, {"group_id": group_id}).fetchall()
    ranked = sorted((_player_stats(row, group_id) for row in rows), key=_rank_key)
    return tuple(ranked if limit is None else ranked[:limit])


def player_stats(
    connection: sqlite3.Connection, *, group_id: int, user_id: int
) -> PlayerStats | None:
    """One player's figures in one group, by Telegram user id.

    Returns:
        Their figures, or ``None`` if they have no finished flips in this group.
        A player who has never played has no recorded name either, which is why
        this is an empty answer rather than a row of zeroes.

    Raises:
        CorruptRecordError: if their totals will not read back.
    """
    row = connection.execute(
        _SELECT_PLAYER_TOTALS, {"group_id": group_id, "user_id": user_id}
    ).fetchone()
    return None if row is None else _player_stats(row, group_id)


def player_stats_by_username(
    connection: sqlite3.Connection, *, group_id: int, username: str
) -> PlayerStats | None:
    """One player's figures in one group, by the username they played under.

    The name is matched case-insensitively against the most recent flip in this
    group that recorded it, so a username that has since changed hands belongs to
    whoever used it last. A leading ``@`` and surrounding spaces are ignored.

    Returns:
        Their figures, or ``None`` if no flip in this group recorded that name.

    Raises:
        CorruptRecordError: if the user id behind the name, or the totals it
            leads to, will not read back.
    """
    wanted = normalize_username(username)
    if wanted is None:
        return None
    with transaction(connection, mode="DEFERRED"):
        row = connection.execute(
            _SELECT_USER_BY_USERNAME, {"group_id": group_id, "username": wanted}
        ).fetchone()
        if row is None:
            return None
        try:
            user_id = _whole(row["user_id"], "user_id")
        except (ValueError, TypeError) as error:
            raise _damaged_stats(row["user_id"], group_id, error) from error
        return player_stats(connection, group_id=group_id, user_id=user_id)


def migrate_group_id(
    connection: sqlite3.Connection, *, old_group_id: int, new_group_id: int
) -> GroupMigration:
    """Move a group's history and in-progress flips to its new id.

    Telegram changes the id when a group becomes a supergroup. All three tables
    move in one transaction, so stats, any open flip and the settings ``/again``
    repeats stay together.

    The last-flip row is one per group, so the two ids could in principle each
    have one. **The more recently set of the two is kept**, and a tie keeps the
    new id's: a group that has been playing under the new id already has the
    newer history, and whatever the old id was holding is the older fact. In
    practice Telegram migrates a group to an id nothing has used, so this does
    not arise; it is written down because a rule nobody can state is worse than
    the clash it covers.

    Returns:
        How many rows moved in ``flips`` and ``active_flips``. Both are zero when
        the two ids are the same, which is a no-op rather than an error. The
        last-flip row is one row either way and is not counted.
    """
    if old_group_id == new_group_id:
        return GroupMigration(flips=0, active_flips=0)
    parameters = {"old": old_group_id, "new": new_group_id}
    with transaction(connection):
        flips = connection.execute(
            "UPDATE flips SET group_id = :new WHERE group_id = :old", parameters
        ).rowcount
        actives = connection.execute(
            "UPDATE active_flips SET group_id = :new WHERE group_id = :old", parameters
        ).rowcount
        _move_last_flip(connection, parameters)
    migration = GroupMigration(flips=flips, active_flips=actives)
    if migration.rows:
        _logger.info(
            "moved group %d to %d: %d flips and %d active flips",
            old_group_id,
            new_group_id,
            migration.flips,
            migration.active_flips,
        )
    return migration


def _move_last_flip(connection: sqlite3.Connection, parameters: Mapping[str, int]) -> None:
    """Move one group's last-flip row to its new id, keeping the newer of a clash.

    Three statements, in this order, so that whichever row is kept is the only
    one left to move: drop the new id's row if the old id's is strictly newer,
    then drop the old id's if anything is still sitting at the new id, then move
    what remains. The times are ISO-8601 UTC, which sorts chronologically as
    text, and a missing row makes the comparison ``NULL`` so nothing is dropped.
    """
    connection.execute(
        """
        DELETE FROM group_last_flip
        WHERE group_id = :new
          AND updated_at < (SELECT updated_at FROM group_last_flip WHERE group_id = :old)
        """,
        parameters,
    )
    connection.execute(
        """
        DELETE FROM group_last_flip
        WHERE group_id = :old
          AND EXISTS (SELECT 1 FROM group_last_flip AS kept WHERE kept.group_id = :new)
        """,
        parameters,
    )
    connection.execute(
        "UPDATE group_last_flip SET group_id = :new WHERE group_id = :old", parameters
    )


def _rank_key(stats: PlayerStats) -> tuple[int, int, str, str, int]:
    """Leaderboard order: net winnings, then wins, then name, then user id."""
    return (
        -stats.net_dollars,
        -stats.wins,
        stats.display_name.casefold(),
        stats.display_name,
        stats.user_id,
    )


def _time_to_text(value: datetime) -> str:
    """A timezone-aware time as ISO-8601 UTC, the form every time column holds."""
    return value.astimezone(UTC).isoformat()


def _time_from_text(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text, got {type(value).__name__}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{label} has no time zone: {value!r}")
    return parsed.astimezone(UTC)


def _points_from_text(value: object) -> Fraction:
    if not isinstance(value, str):
        raise ValueError(f"points must be text, got {type(value).__name__}")
    return Fraction(value)


def _whole(value: object, label: str) -> int:
    """One stored whole number, and nothing that merely looks like one.

    Every integer column here holds a count, an identity, a seat or a setting,
    and ``int`` would read a fraction or numeric text as one of those and quietly
    drop what it could not keep: sqlite's INTEGER affinity stores ``50.9`` as a
    REAL, and ``int`` would hand it back as a perfectly plausible 50 that
    ``/verify`` then passes. A boolean is not a number either, though no column
    stores one, so the check says what it means.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number, got {value!r}")
    return value


def _dollars_from(value: object) -> int:
    """Money as it is stored: a whole number, which may be negative."""
    return _whole(value, "net_dollars")


def _flag(value: object, label: str) -> bool:
    """One stored yes-or-no: the 0 or 1 that is in the column, and nothing else.

    ``bool`` would read text, or any number at all, as an answer, so a damaged
    flag would come back as a yes rather than as the damage it is.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValueError(f"{label} must be 0 or 1, got {value!r}")
    return bool(value)


def _scooped_from(value: object) -> bool:
    """Whether this player scooped, as it is stored."""
    return _flag(value, "scooped")


def _settings_from(row: Mapping[str, Any]) -> Settings:
    """The four settings out of any row that holds them, limits checked again."""
    return Settings(
        wager=_whole(row["wager"], "wager"),
        x=_whole(row["x"], "x"),
        y=_whole(row["y"], "y"),
        z=_whole(row["z"], "z"),
    )


def _player_stats(row: Mapping[str, Any], group_id: int) -> PlayerStats:
    """Build one player's totals from their row, or say whose totals are damaged.

    The totals are counted by the query, so what will not read back here is a
    figure that cannot be true of any history -- more scoops than flips, say --
    which is what tampered-with player rows add up to. The exception is the two
    columns sqlite adds up whatever is in them: ``damaged_dollars`` and
    ``damaged_scoops`` are how many of this player's rows do not hold whole
    dollars, and a 0 or a 1 for the scoop, and one of either is enough for the
    totals to mean nothing.
    """
    try:
        damaged_dollars = row["damaged_dollars"]
        if damaged_dollars:
            raise ValueError(
                f"{damaged_dollars} of this player's flips do not record whole dollars"
            )
        damaged_scoops = row["damaged_scoops"]
        if damaged_scoops:
            raise ValueError(
                f"{damaged_scoops} of this player's flips do not record a 0 or 1 for the scoop"
            )
        return PlayerStats(
            user_id=_whole(row["user_id"], "user_id"),
            display_name=str(row["display_name"]),
            username=normalize_username(row["username"]),
            net_dollars=_whole(row["net_dollars"], "net_dollars"),
            flips_played=_whole(row["flips_played"], "flips_played"),
            wins=_whole(row["wins"], "wins"),
            scoops=_whole(row["scoops"], "scoops"),
        )
    except (ValueError, TypeError) as error:
        raise _damaged_stats(row["user_id"], group_id, error) from error


def _damaged_stats(user_id: object, group_id: int, error: Exception) -> CorruptRecordError:
    """The error a stats row that will not read back raises, logged as it is made."""
    record = f"stats for player {user_id!r} in group {group_id}"
    _logger.warning("%s cannot be read back: %s", record, error)
    return CorruptRecordError(record, str(error))


def _flip_player(row: Mapping[str, Any]) -> FlipPlayer:
    return FlipPlayer(
        user_id=_whole(row["user_id"], "user_id"),
        display_name=str(row["display_name"]),
        username=normalize_username(row["username"]),
        seat=_whole(row["seat"], "seat"),
        points=_points_from_text(row["points"]),
        net_dollars=_dollars_from(row["net_dollars"]),
        scooped=_scooped_from(row["scooped"]),
    )


def _stored_flip(row: Mapping[str, Any], player_rows: Sequence[Mapping[str, Any]]) -> StoredFlip:
    """Build a flip from its row and its players', or say which flip is damaged.

    The flip's own columns for the player count and the button seat are read
    too, and have to agree with the stored deal. They are the deal's own figures
    written out beside it, so the two disagreeing is damage however plausible
    each looks on its own -- and the deal is what ``/verify`` re-deals, so
    without this check a row saying a different player held the button would
    verify.
    """
    flip_number = int(row["flip_number"])
    try:
        player_count = _whole(row["player_count"], "player_count")
        button_seat = _whole(row["button_seat"], "button_seat")
        flip = FinishedFlip(
            group_id=_whole(row["group_id"], "group_id"),
            settings=_settings_from(row),
            y_defaulted=_flag(row["y_defaulted"], "y_defaulted"),
            z_defaulted=_flag(row["z_defaulted"], "z_defaulted"),
            seed=seed_from_hex(str(row["seed"])),
            commitment=str(row["commitment"]),
            deal=Deal.from_json(str(row["deal"])),
            players=tuple(_flip_player(player) for player in player_rows),
            started_at=_time_from_text(row["started_at"], "started_at"),
            finished_at=_time_from_text(row["finished_at"], "finished_at"),
        )
        if (player_count, button_seat) != (flip.player_count, flip.button_seat):
            raise ValueError(
                f"the row records {player_count} players and button seat {button_seat}, "
                f"but its deal has {flip.player_count} and {flip.button_seat}"
            )
    except (ValueError, TypeError, ZeroDivisionError) as error:
        _logger.warning("flip %d cannot be read back: %s", flip_number, error)
        raise CorruptRecordError(f"flip {flip_number}", str(error)) from error
    return StoredFlip(flip_number=flip_number, flip=flip)


def _active_flip(row: Mapping[str, Any]) -> ActiveFlip:
    """Build an in-progress flip from its row, or say which token is damaged."""
    token = str(row["token"])
    stored_seed = row["seed"]
    stored_topic = row["topic_id"]
    try:
        return ActiveFlip(
            token=token,
            group_id=_whole(row["group_id"], "group_id"),
            topic_id=None if stored_topic is None else _whole(stored_topic, "topic_id"),
            message_id=_whole(row["message_id"], "message_id"),
            state=FlipState(str(row["state"])),
            seed=None if stored_seed is None else seed_from_hex(str(stored_seed)),
            created_at=_time_from_text(row["created_at"], "created_at"),
        )
    except (ValueError, TypeError) as error:
        _logger.warning("active flip %r cannot be read back: %s", token, error)
        raise CorruptRecordError(f"active flip {token!r}", str(error)) from error

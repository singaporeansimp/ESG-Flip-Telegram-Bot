"""Taking a copy of the database while the bot is still running.

``cp esgbot.sqlite3 somewhere`` is the wrong way to back this file up. The
database runs in WAL mode, so a committed flip lives in ``esgbot.sqlite3-wal``
until a checkpoint folds it into the main file: a copy of the main file alone can
be missing the last flips, and a copy of the two files taken a moment apart can
be a torn one that no longer reads. SQLite's online backup API exists for exactly
this. It reads the database through one read transaction, which sees the WAL, and
produces a standalone file holding every transaction that had committed when it
started and no part of any that had not.

That API is in the standard library, as :meth:`sqlite3.Connection.backup`, so
backups need nothing installed beyond the bot itself -- in particular not the
``sqlite3`` command-line tool, which is a separate package on most distributions
and is often absent on a small VPS.

Run it with ``deploy/backup.sh``, which is what the systemd timer calls, or
directly::

    python -m esgbot.backup --database /var/lib/esgbot/esgbot.sqlite3 \\
        --backup-dir /var/backups/esgbot --keep-days 14

Each run writes ``esgbot-<timestamp>.sqlite3``, and three things make the file in
that name trustworthy. The copy is written under a ``.partial`` name,
``PRAGMA integrity_check`` is run against it, and only then is it renamed, so a
run that dies halfway or produces a damaged file leaves no file that looks like a
backup. Pruning happens after that rename and only ever removes files whose own
name says how old they are, so the backup just taken is never a candidate and a
file this module did not write is never touched.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from esgbot.logging_setup import get_logger, setup_logging
from esgbot.telegram.settings import DB_PATH_ENV_VAR, DEFAULT_DB_PATH

__all__ = [
    "BACKUP_DIR_ENV_VAR",
    "DEFAULT_BACKUP_DIR",
    "DEFAULT_KEEP_DAYS",
    "FILE_PREFIX",
    "FILE_SUFFIX",
    "KEEP_DAYS_ENV_VAR",
    "TIMESTAMP_FORMAT",
    "BackupError",
    "main",
    "make_backup",
    "prune_backups",
]

BACKUP_DIR_ENV_VAR: Final = "ESGBOT_BACKUP_DIR"
"""Where backups are written. Optional; see :data:`DEFAULT_BACKUP_DIR`."""

KEEP_DAYS_ENV_VAR: Final = "ESGBOT_BACKUP_KEEP_DAYS"
"""How many days of backups to keep. Optional; see :data:`DEFAULT_KEEP_DAYS`."""

DEFAULT_BACKUP_DIR: Final = "backups"
"""Where backups go when ``ESGBOT_BACKUP_DIR`` is unset: next to wherever the
backup was run from, which suits a laptop. The systemd unit sets it properly."""

DEFAULT_KEEP_DAYS: Final = 14
"""Days of backups kept when ``ESGBOT_BACKUP_KEEP_DAYS`` is unset. ``0`` there,
or ``--keep-days 0``, means nothing is ever deleted."""

FILE_PREFIX: Final = "esgbot-"
"""Every backup's name starts with this."""

FILE_SUFFIX: Final = ".sqlite3"
"""And ends with this."""

TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"
"""UTC, and sortable, so the names sort into chronological order."""

_PARTIAL_SUFFIX: Final = ".partial"
"""What a copy is called until it has passed its integrity check."""

_BUSY_TIMEOUT_MS: Final = 30_000
"""How long the backup waits for the bot's writes to let go. Longer than the
bot's own timeout, because a backup would rather wait than be skipped."""

# Named rather than taken from ``__name__``, which is ``__main__`` under
# ``python -m esgbot.backup`` and would leave the journal saying so.
_logger = get_logger("esgbot.backup")


class BackupError(RuntimeError):
    """The backup could not be taken, or could not be trusted once taken.

    ``str(error)`` is a complete sentence naming what to fix, so :func:`main` can
    log it and stop with a non-zero status for the timer to report.
    """


def make_backup(*, database: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    """Copy ``database`` into ``backup_dir`` and return the file that was written.

    The copy is consistent even though the bot is writing to the database at the
    time: it holds every transaction that had committed when the copy began, and
    no part of one that had not. It is checked with ``PRAGMA integrity_check``
    before it is given its final name.

    Args:
        database: the live database file. It is only ever read.
        backup_dir: where the copy goes. Created if it is not there.
        now: the timestamp for the file's name. Defaults to now, in UTC.

    Raises:
        BackupError: if the database is not there, cannot be read, or the copy
            does not pass its integrity check. Nothing is left in ``backup_dir``
            in any of those cases.
    """
    if not database.is_file():
        raise BackupError(
            f"there is no database file at {database}. Check {DB_PATH_ENV_VAR} or pass "
            f"--database with the path the bot is using."
        )
    moment = (datetime.now(UTC) if now is None else now).astimezone(UTC)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError(f"cannot write backups to {backup_dir}: {error}") from error
    target = _free_path(backup_dir, moment)
    partial = target.with_name(target.name + _PARTIAL_SUFFIX)
    try:
        _copy_database(database, partial)
        _check_integrity(partial)
    except BaseException:
        _remove(partial)
        raise
    partial.replace(target)
    _logger.info("backed up %s to %s (%d bytes)", database, target, target.stat().st_size)
    return target


def prune_backups(
    *, backup_dir: Path, keep_days: int, now: datetime | None = None
) -> tuple[Path, ...]:
    """Delete backups older than ``keep_days`` and return the ones deleted.

    Only files whose name this module would have written are considered, and the
    age comes from the timestamp in the name rather than from the file's mtime,
    so a file that was copied or touched is still pruned on its real age and
    anything else in the directory is left alone.

    ``keep_days=0`` deletes nothing, which is how an operator who keeps backups
    somewhere else turns pruning off.

    A ``.partial`` left behind by a run that was killed between the copy and the
    rename is swept the same way. One that is still being written carries today's
    timestamp, so it is never old enough to be taken out from under itself.

    Raises:
        BackupError: if ``keep_days`` is negative, or a file cannot be deleted.
    """
    if keep_days < 0:
        raise BackupError(f"keep-days cannot be negative, and {keep_days} is")
    if keep_days == 0:
        return ()
    moment = (datetime.now(UTC) if now is None else now).astimezone(UTC)
    cutoff = moment - timedelta(days=keep_days)
    found = backup_dir.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}")
    abandoned = backup_dir.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}{_PARTIAL_SUFFIX}")
    removed: list[Path] = []
    for path in sorted([*found, *abandoned]):
        stamp = _timestamp_of(path)
        if stamp is None or stamp >= cutoff or not path.is_file():
            continue
        try:
            _remove(path)
        except OSError as error:
            raise BackupError(f"cannot delete the old backup {path}: {error}") from error
        removed.append(path)
        _logger.info("removed backup %s, older than %d days", path, keep_days)
    return tuple(removed)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    """Take one backup and prune the old ones. Returns the process's exit status.

    The copy is made before the retention is so much as looked at, so a mistyped
    ``ESGBOT_BACKUP_KEEP_DAYS`` costs a night's pruning and never a backup. Either
    failure is a non-zero status, which is what the timer reports.
    """
    setup_logging()
    source = os.environ if env is None else env
    arguments = _parser(source).parse_args(argv)
    backup_dir = Path(arguments.backup_dir)
    try:
        make_backup(database=Path(arguments.database), backup_dir=backup_dir)
        keep_days = _keep_days(source) if arguments.keep_days is None else int(arguments.keep_days)
        prune_backups(backup_dir=backup_dir, keep_days=keep_days)
    except BackupError as error:
        _logger.error("%s", error)
        return 1
    return 0


def _parser(source: Mapping[str, str]) -> argparse.ArgumentParser:
    """The command line, with the environment supplying the defaults."""
    parser = argparse.ArgumentParser(
        prog="python -m esgbot.backup",
        description="Copy the bot's SQLite database while the bot is running.",
    )
    parser.add_argument(
        "--database",
        default=source.get(DB_PATH_ENV_VAR, "").strip() or DEFAULT_DB_PATH,
        help=f"the live database file (default: ${DB_PATH_ENV_VAR}, or {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--backup-dir",
        default=source.get(BACKUP_DIR_ENV_VAR, "").strip() or DEFAULT_BACKUP_DIR,
        help=f"where copies go (default: ${BACKUP_DIR_ENV_VAR}, or {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=None,
        help=(
            f"days of backups to keep, 0 to keep every one "
            f"(default: ${KEEP_DAYS_ENV_VAR}, or {DEFAULT_KEEP_DAYS})"
        ),
    )
    return parser


def _keep_days(source: Mapping[str, str]) -> int:
    """The retention from the environment.

    Raises:
        BackupError: if the variable is set to something that is not a count of
            days, because silently keeping everything -- or nothing -- is worse.
    """
    raw = source.get(KEEP_DAYS_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_KEEP_DAYS
    try:
        days = int(raw)
    except ValueError:
        raise BackupError(
            f"{KEEP_DAYS_ENV_VAR} should be a whole number of days, and it is {raw!r}"
        ) from None
    if days < 0:
        raise BackupError(f"{KEEP_DAYS_ENV_VAR} cannot be negative, and it is {raw!r}")
    return days


def _copy_database(database: Path, target: Path) -> None:
    """Run the online backup from ``database`` into a fresh ``target`` file."""
    _remove(target)
    try:
        source = _open(database)
        try:
            destination = _open(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except sqlite3.Error as error:
        raise BackupError(f"cannot copy {database}: {error}") from error


def _check_integrity(path: Path) -> None:
    """Read the copy back and make sure SQLite calls it sound.

    Raises:
        BackupError: if it will not open, or integrity_check says anything but
            ``ok``.
    """
    try:
        connection = _open(path)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError(f"the copy at {path} will not read back: {error}") from error
    problems = [str(row[0]) for row in rows]
    if problems != ["ok"]:
        raise BackupError(f"the copy at {path} failed its integrity check: {'; '.join(problems)}")


def _open(path: Path) -> sqlite3.Connection:
    """A connection that waits a while for a lock instead of failing on one."""
    connection = sqlite3.connect(path, isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000)
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return connection


def _free_path(backup_dir: Path, moment: datetime) -> Path:
    """The name for this backup, counting upwards if that second is taken."""
    stem = f"{FILE_PREFIX}{moment.strftime(TIMESTAMP_FORMAT)}"
    candidate = backup_dir / f"{stem}{FILE_SUFFIX}"
    counter = 2
    while candidate.exists():
        candidate = backup_dir / f"{stem}-{counter}{FILE_SUFFIX}"
        counter += 1
    return candidate


def _timestamp_of(path: Path) -> datetime | None:
    """When a backup's name says it was taken, or ``None`` if it does not say.

    ``None`` is what everything else in the directory gets, which is what keeps
    pruning from touching a file this module did not write. A ``.partial`` is read
    as the backup it was going to be.
    """
    name = path.name.removesuffix(_PARTIAL_SUFFIX)
    if not name.startswith(FILE_PREFIX) or not name.endswith(FILE_SUFFIX):
        return None
    middle = name[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
    try:
        return datetime.strptime(middle.split("-", 1)[0], TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _remove(path: Path) -> None:
    """Delete a half-written copy, and the journal files it may have left."""
    for name in (path.name, f"{path.name}-wal", f"{path.name}-shm", f"{path.name}-journal"):
        path.with_name(name).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())

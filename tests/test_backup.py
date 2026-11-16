"""Backing up the database: a copy that can be trusted, and old ones cleared out.

The claim worth pinning is the one the whole module exists for. The bot keeps the
database in WAL mode, so a committed flip sits in the ``-wal`` file until a
checkpoint folds it in, and the copy has to be taken through SQLite rather than
through the filesystem. Two tests here hold that line: one takes a backup of a
database being written to as fast as a loop can write to it and shows the copy is
whole, and one shows what a plain ``cp`` of the same file would have handed over
instead.

The rest is the care around the copy -- the integrity check, the ``.partial``
name that keeps a failed run from looking like a backup, and pruning that only
ever touches files it recognises.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from esgbot import backup
from esgbot.backup import BackupError
from esgbot.storage import Database, schema
from tests.storage_helpers import Line, make_flip

NOW = datetime(2026, 11, 15, 3, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _leave_logging_to_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``main`` from configuring logging for the whole session.

    ``setup_logging`` takes pytest's handler off the root logger and leaves the
    root level where the environment says, which is how one test here used to
    make a test in another file see log records it had not expected. What is under
    test is what the backup does, not how the process sets logging up, which
    ``test_logging_setup.py`` covers.
    """
    monkeypatch.setattr("esgbot.backup.setup_logging", lambda *_, **__: None)


def write_wal_database(path: Path, rows: int = 50) -> sqlite3.Connection:
    """A WAL database with paired rows, left open so the WAL is not checkpointed.

    Every transaction puts the same number in both tables, so a copy showing
    different counts caught one of them half-done. Autocheckpointing is off, which
    is what keeps the rows in the ``-wal`` file where a plain ``cp`` of the
    database cannot see them -- the same place the bot's last flips sit between
    checkpoints, just reliably so.

    The caller closes the connection. It is usable from another thread, because
    the test that writes while a backup runs does the writing in one.
    """
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE ledger (n INTEGER)")
    connection.execute("CREATE TABLE mirror (n INTEGER)")
    for n in range(1, rows + 1):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO ledger VALUES (?)", (n,))
        connection.execute("INSERT INTO mirror VALUES (?)", (n,))
        connection.execute("COMMIT")
    return connection


def counts(path: Path) -> tuple[int, int]:
    """How many rows each table of a copy has."""
    connection = sqlite3.connect(path)
    try:
        ledger = connection.execute("SELECT count(*) FROM ledger").fetchone()[0]
        mirror = connection.execute("SELECT count(*) FROM mirror").fetchone()[0]
    finally:
        connection.close()
    return int(ledger), int(mirror)


def integrity(path: Path) -> list[str]:
    """What ``PRAGMA integrity_check`` says about a copy."""
    connection = sqlite3.connect(path)
    try:
        return [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()


def ledger_rows(path: Path) -> int | None:
    """How many rows a copy has, or ``None`` if it has not even got the table.

    Both are ways of having lost the writes, which is what a copy taken with the
    filesystem rather than with SQLite looks like.
    """
    try:
        return counts(path)[0]
    except sqlite3.Error:
        return None


# ------------------------------------------------- the copy, and why not cp


def test_the_copy_of_a_database_being_written_to_is_whole(tmp_path: Path) -> None:
    """A backup taken mid-write holds committed transactions and no partial one."""
    database = tmp_path / "esgbot.sqlite3"
    connection = write_wal_database(database)
    stop = threading.Event()
    committed = 0

    def hammer() -> None:
        nonlocal committed
        n = 1_000
        while not stop.is_set():
            n += 1
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO ledger VALUES (?)", (n,))
            connection.execute("INSERT INTO mirror VALUES (?)", (n,))
            connection.execute("COMMIT")
            committed += 1

    writer = threading.Thread(target=hammer)
    writer.start()
    try:
        copies = [
            backup.make_backup(database=database, backup_dir=tmp_path / "backups") for _ in range(3)
        ]
    finally:
        stop.set()
        writer.join()
        connection.close()

    assert committed > 0, "the writer never got going, so nothing was proved"
    for copy in copies:
        ledger, mirror = counts(copy)
        assert integrity(copy) == ["ok"]
        # The halves of every transaction are both there or both absent.
        assert ledger == mirror
        assert ledger >= 50


def test_a_plain_copy_of_the_same_database_would_have_lost_the_rows(tmp_path: Path) -> None:
    """What ``cp`` hands over while the WAL is holding the committed rows."""
    database = tmp_path / "esgbot.sqlite3"
    connection = write_wal_database(database)
    try:
        taken = backup.make_backup(database=database, backup_dir=tmp_path / "backups")
        plain = tmp_path / "plain-cp.sqlite3"
        plain.write_bytes(database.read_bytes())
    finally:
        connection.close()

    assert counts(taken) == (50, 50)
    assert ledger_rows(plain) in (None, 0), "the plain copy somehow kept the rows"


def test_a_restored_copy_reads_back_as_the_flips_the_bot_recorded(tmp_path: Path) -> None:
    """The copy is a database the bot itself opens, with the same history in it."""
    database = tmp_path / "esgbot.sqlite3"
    group = -1001

    async def record_then_restore() -> None:
        store = await Database.open(database)
        try:
            number = await store.record_flip(
                make_flip(
                    group,
                    [Line(11, "Alice", 100), Line(22, "Bob", -100)],
                    seed_byte=7,
                )
            )
            original = await store.fetch_flip(group_id=group, flip_number=number)
            assert original is not None
            # Taken while the bot's own connection is still open, as it would be.
            copy = backup.make_backup(database=database, backup_dir=tmp_path / "backups")
        finally:
            await store.close()

        restored = await Database.open(copy)
        try:
            assert await restored.fetch_flip(group_id=group, flip_number=number) == original
        finally:
            await restored.close()

        connection = sqlite3.connect(copy)
        try:
            assert schema.schema_version(connection) == schema.SCHEMA_VERSION
        finally:
            connection.close()

    asyncio.run(record_then_restore())


# --------------------------------------------------------------- the name


def test_the_name_says_when_the_backup_was_taken(tmp_path: Path) -> None:
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=1).close()

    taken = backup.make_backup(database=database, backup_dir=tmp_path / "backups", now=NOW)

    assert taken.name == "esgbot-20261115T033000Z.sqlite3"
    assert taken.parent == tmp_path / "backups"


def test_two_backups_in_the_same_second_do_not_overwrite_each_other(tmp_path: Path) -> None:
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=1).close()

    first = backup.make_backup(database=database, backup_dir=tmp_path / "backups", now=NOW)
    second = backup.make_backup(database=database, backup_dir=tmp_path / "backups", now=NOW)

    assert first != second
    assert first.exists()
    assert second.exists()
    assert second.name == "esgbot-20261115T033000Z-2.sqlite3"


def test_the_backup_directory_is_made_if_it_is_not_there(tmp_path: Path) -> None:
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=1).close()

    taken = backup.make_backup(database=database, backup_dir=tmp_path / "a" / "b", now=NOW)

    assert taken.is_file()


# ------------------------------------------------------ when it cannot be done


def test_a_database_that_is_not_there_is_said_so_rather_than_created(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.sqlite3"

    with pytest.raises(BackupError, match="no database file"):
        backup.make_backup(database=missing, backup_dir=tmp_path / "backups")

    assert not missing.exists(), "a backup must never bring the database into being"
    assert not (tmp_path / "backups").exists()


def test_a_file_that_is_not_a_database_leaves_nothing_behind(tmp_path: Path) -> None:
    """The failed copy does not keep the name a real backup would have."""
    rubbish = tmp_path / "esgbot.sqlite3"
    rubbish.write_bytes(b"this is not a database" * 100)
    backups = tmp_path / "backups"

    with pytest.raises(BackupError, match=r"cannot copy|will not read back"):
        backup.make_backup(database=rubbish, backup_dir=backups, now=NOW)

    assert list(backups.iterdir()) == []


def test_a_directory_in_place_of_the_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no database file"):
        backup.make_backup(database=tmp_path, backup_dir=tmp_path / "backups")


# --------------------------------------------------------------- retention


def dated(backup_dir: Path, days_old: int, *, now: datetime = NOW) -> Path:
    """A file named as a backup taken ``days_old`` days before ``now``."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now - timedelta(days=days_old)).strftime(backup.TIMESTAMP_FORMAT)
    path = backup_dir / f"{backup.FILE_PREFIX}{stamp}{backup.FILE_SUFFIX}"
    path.write_bytes(b"")
    return path


def test_backups_older_than_the_retention_go_and_newer_ones_stay(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    old = [dated(backups, 15), dated(backups, 40)]
    kept = [dated(backups, 0), dated(backups, 1), dated(backups, 13)]

    removed = backup.prune_backups(backup_dir=backups, keep_days=14, now=NOW)

    assert sorted(removed) == sorted(old)
    assert all(not path.exists() for path in old)
    assert all(path.exists() for path in kept)


def test_the_backup_just_taken_is_never_the_one_pruned(tmp_path: Path) -> None:
    """Keeping a single day still leaves today's copy, which is the whole point."""
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=1).close()
    backups = tmp_path / "backups"
    dated(backups, 3)

    taken = backup.make_backup(database=database, backup_dir=backups, now=NOW)
    removed = backup.prune_backups(backup_dir=backups, keep_days=1, now=NOW)

    assert taken.exists()
    assert taken not in removed
    assert len(removed) == 1


def test_keeping_zero_days_means_keeping_everything(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    ancient = dated(backups, 900)

    assert backup.prune_backups(backup_dir=backups, keep_days=0, now=NOW) == ()
    assert ancient.exists()


def test_nothing_the_backup_did_not_write_is_ever_deleted(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    old = dated(backups, 99)
    strangers = [
        backups / "esgbot.sqlite3",
        backups / "esgbot-not-a-timestamp.sqlite3",
        backups / "esgbot-20261115T033000Z.sqlite3.gpg",
        backups / "notes.txt",
    ]
    for stranger in strangers:
        stranger.write_text("keep me")
    directory = backups / "esgbot-20200101T000000Z.sqlite3"
    directory.mkdir()

    removed = backup.prune_backups(backup_dir=backups, keep_days=1, now=NOW)

    assert removed == (old,)
    assert all(stranger.exists() for stranger in strangers)
    assert directory.is_dir()


def test_a_counted_name_is_pruned_on_the_date_it_carries(tmp_path: Path) -> None:
    """``esgbot-<stamp>-2.sqlite3`` is as old as ``<stamp>`` says."""
    backups = tmp_path / "backups"
    stamp = (NOW - timedelta(days=30)).strftime(backup.TIMESTAMP_FORMAT)
    second = backups / f"esgbot-{stamp}-2.sqlite3"
    backups.mkdir()
    second.write_bytes(b"")

    assert backup.prune_backups(backup_dir=backups, keep_days=14, now=NOW) == (second,)


def test_a_partial_left_by_a_killed_run_is_swept_up_too(tmp_path: Path) -> None:
    """Nothing else ever cleans one of those, so pruning has to."""
    backups = tmp_path / "backups"
    old = dated(backups, 30)
    stale = backups / f"{old.name}.partial"
    stale.write_bytes(b"half a copy")
    (backups / f"{old.name}.partial-wal").write_bytes(b"and its journal")
    today = backups / f"{dated(backups, 0).name}.partial"
    today.write_bytes(b"a copy being written right now")

    removed = backup.prune_backups(backup_dir=backups, keep_days=14, now=NOW)

    assert set(removed) == {old, stale}
    assert not (backups / f"{old.name}.partial-wal").exists()
    assert today.exists(), "a copy in flight carries today's date and must survive"


def test_a_missing_backup_directory_is_nothing_to_prune(tmp_path: Path) -> None:
    assert backup.prune_backups(backup_dir=tmp_path / "gone", keep_days=14, now=NOW) == ()


def test_a_negative_retention_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="cannot be negative"):
        backup.prune_backups(backup_dir=tmp_path, keep_days=-1, now=NOW)


# ------------------------------------------------------------- the command


def test_the_command_backs_up_where_the_environment_says(tmp_path: Path) -> None:
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=3).close()
    backups = tmp_path / "backups"
    old = dated(backups, 99)

    status = backup.main(
        [],
        env={
            "ESGBOT_DB_PATH": str(database),
            "ESGBOT_BACKUP_DIR": str(backups),
            "ESGBOT_BACKUP_KEEP_DAYS": "14",
        },
    )

    assert status == 0
    (taken,) = [path for path in backups.iterdir() if path != old]
    assert counts(taken) == (3, 3)
    assert not old.exists()


def test_the_command_line_wins_over_the_environment(tmp_path: Path) -> None:
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=1).close()
    chosen = tmp_path / "chosen"
    old = dated(chosen, 99)

    status = backup.main(
        ["--database", str(database), "--backup-dir", str(chosen), "--keep-days", "0"],
        env={"ESGBOT_DB_PATH": "/nowhere/esgbot.sqlite3", "ESGBOT_BACKUP_DIR": "/nowhere"},
    )

    assert status == 0
    assert old.exists(), "--keep-days 0 keeps every backup"
    assert len(list(chosen.iterdir())) == 2


def test_the_command_reports_a_database_it_cannot_find(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    status = backup.main([], env={"ESGBOT_DB_PATH": str(tmp_path / "nowhere.sqlite3")})

    assert status == 1
    assert "no database file" in caplog.text


def test_a_retention_that_is_not_a_number_of_days_still_costs_no_backup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "esgbot.sqlite3"
    write_wal_database(database, rows=1).close()
    backups = tmp_path / "backups"

    status = backup.main(
        [],
        env={
            "ESGBOT_DB_PATH": str(database),
            "ESGBOT_BACKUP_DIR": str(backups),
            "ESGBOT_BACKUP_KEEP_DAYS": "a fortnight",
        },
    )

    assert status == 1
    assert "ESGBOT_BACKUP_KEEP_DAYS" in caplog.text
    assert len(list(backups.iterdir())) == 1, "the copy is taken before retention is read"

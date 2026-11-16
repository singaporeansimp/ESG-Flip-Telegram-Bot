from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from esgbot.game import fairness
from esgbot.game.rules import Y_DEFAULT, FlipRequest, Settings
from esgbot.storage import (
    DEFAULT_LEADERBOARD_LIMIT,
    SCHEMA_VERSION,
    ActiveFlip,
    CorruptRecordError,
    Database,
    FinishedFlip,
    FlipState,
    GroupMigration,
    PlayerStats,
    SchemaTooNewError,
    StorageError,
    normalize_username,
    schema,
)
from esgbot.storage.schema import _MIGRATION_1
from tests.storage_helpers import EPOCH, Line, count_rows, make_flip, sync_connection

GROUP_A = -1001
GROUP_B = -1002

ALICE, BOB, CARA, EVE = 11, 22, 33, 44


def seed_of(n: int) -> bytes:
    return bytes([n]) * fairness.SEED_SIZE


def active(
    token: str,
    group_id: int = GROUP_A,
    *,
    created_at: datetime = EPOCH,
    seed: bytes | None = None,
    state: FlipState = FlipState.LOBBY,
    topic_id: int | None = None,
    message_id: int = 500,
) -> ActiveFlip:
    return ActiveFlip(
        token=token,
        group_id=group_id,
        topic_id=topic_id,
        message_id=message_id,
        state=state,
        seed=seed,
        created_at=created_at,
    )


async def record_two_groups(db: Database) -> None:
    """Group A: Alice scoops once and wins twice; Bob and Cara never win (Bob chops).
    Group B: overlapping players with different results."""
    await db.record_flip(
        make_flip(
            GROUP_A,
            [
                Line(ALICE, "Alice", 100, "alice", Fraction(2)),
                Line(BOB, "Bob", -50, "bob", Fraction(1, 2)),
                Line(CARA, "Cara", -50, None, Fraction(1, 2)),
            ],
            seed_byte=1,
        )
    )
    await db.record_flip(
        make_flip(
            GROUP_A,
            [
                Line(ALICE, "Alice", 100, "alice", Fraction(3), scooped=True),
                Line(BOB, "Bob", -50, "bob", Fraction(0)),
                Line(CARA, "Cara", -50, None, Fraction(0)),
            ],
            seed_byte=2,
        )
    )
    # A chop: both players get their wager back, so they played and did not win.
    await db.record_flip(
        make_flip(
            GROUP_A,
            [
                Line(BOB, "Bob", 0, "bob", Fraction(3, 2)),
                Line(CARA, "Cara", 0, None, Fraction(3, 2)),
            ],
            seed_byte=3,
        )
    )
    await db.record_flip(
        make_flip(
            GROUP_B,
            [
                Line(BOB, "Bobby", 100, "bobby", Fraction(2)),
                Line(ALICE, "Alice", -50, "alice", Fraction(1, 2)),
                Line(EVE, "Eve", -50, None, Fraction(1, 2)),
            ],
            seed_byte=4,
        )
    )


def by_id(rows: tuple[PlayerStats, ...]) -> dict[int, PlayerStats]:
    return {row.user_id: row for row in rows}


# --- Acceptance 1: separate, correct per-group figures -------------------------


def test_groups_have_separate_leaderboards_and_stats(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await record_two_groups(db)

            board_a = await db.leaderboard(GROUP_A)
            assert [row.user_id for row in board_a] == [ALICE, BOB, CARA]
            alice, bob, cara = board_a
            assert (alice.net_dollars, alice.flips_played, alice.wins, alice.scoops) == (
                200,
                2,
                2,
                1,
            )
            # Bob: negative net, played three (one a $0 chop), never won.
            assert (bob.net_dollars, bob.flips_played, bob.wins, bob.scoops) == (-100, 3, 0, 0)
            assert (cara.net_dollars, cara.flips_played, cara.wins, cara.scoops) == (-100, 3, 0, 0)
            assert bob.win_rate == 0.0
            assert alice.win_rate == 1.0
            assert alice.display_name == "Alice"
            assert alice.username == "alice"
            assert cara.username is None

            board_b = await db.leaderboard(GROUP_B)
            assert [row.user_id for row in board_b] == [BOB, ALICE, EVE]
            assert [row.net_dollars for row in board_b] == [100, -50, -50]
            assert [row.flips_played for row in board_b] == [1, 1, 1]
            assert [row.wins for row in board_b] == [1, 0, 0]
            # Bob is "Bobby" in group B but "Bob" in group A: names do not leak.
            assert board_b[0].display_name == "Bobby"
            assert board_b[0].username == "bobby"

            # player_stats agrees with the leaderboard, group by group.
            for group_id, board in ((GROUP_A, board_a), (GROUP_B, board_b)):
                for row in board:
                    assert await db.player_stats(group_id=group_id, user_id=row.user_id) == row

            # Eve exists only in B; Cara only in A.
            assert await db.player_stats(group_id=GROUP_A, user_id=EVE) is None
            assert await db.player_stats(group_id=GROUP_B, user_id=CARA) is None
            assert await db.leaderboard(-999) == ()

    asyncio.run(scenario())


def test_chop_winner_netting_zero_played_but_did_not_win(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [
                        Line(ALICE, "Alice", 0, None, Fraction(3, 2)),
                        Line(BOB, "Bob", 0, None, Fraction(3, 2)),
                    ],
                    seed_byte=1,
                )
            )
            stats = await db.player_stats(group_id=GROUP_A, user_id=ALICE)
            assert stats == PlayerStats(ALICE, "Alice", None, 0, 1, 0, 0)

    asyncio.run(scenario())


def test_stats_with_only_losses_have_negative_net(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "Alice", 150), Line(BOB, "Bob", -150)],
                    seed_byte=1,
                )
            )
            stats = await db.player_stats(group_id=GROUP_A, user_id=BOB)
            assert stats is not None
            assert stats.net_dollars == -150
            assert stats.wins == 0

    asyncio.run(scenario())


def test_leaderboard_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await record_two_groups(db)
            assert [r.user_id for r in await db.leaderboard(GROUP_A, limit=2)] == [ALICE, BOB]
            assert len(await db.leaderboard(GROUP_A, limit=None)) == 3
            assert len(await db.leaderboard(GROUP_A, limit=50)) == 3
            assert await db.leaderboard(GROUP_A, limit=0) == ()
            with pytest.raises(ValueError, match="negative"):
                await db.leaderboard(GROUP_A, limit=-1)

    asyncio.run(scenario())


def test_default_leaderboard_limit_is_twenty(tmp_path: Path) -> None:
    assert DEFAULT_LEADERBOARD_LIMIT == 20

    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            for n in range(25):
                await db.record_flip(
                    make_flip(
                        GROUP_A,
                        [Line(1000 + n, f"p{n:02d}", 10), Line(2000 + n, f"q{n:02d}", -10)],
                        seed_byte=n + 1,
                    )
                )
            assert len(await db.leaderboard(GROUP_A)) == 20
            assert len(await db.leaderboard(GROUP_A, limit=None)) == 50

    asyncio.run(scenario())


# --- Acceptance 2: ranking ties ------------------------------------------------


def test_net_tie_broken_by_wins(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            # Both end on net +50. Alice has one win, Bob has two.
            await db.record_flip(
                make_flip(GROUP_A, [Line(ALICE, "Alice", 100), Line(BOB, "Bob", 25)], seed_byte=1)
            )
            await db.record_flip(
                make_flip(GROUP_A, [Line(ALICE, "Alice", -50), Line(BOB, "Bob", 25)], seed_byte=2)
            )
            board = await db.leaderboard(GROUP_A)
            assert [(r.user_id, r.net_dollars, r.wins) for r in board] == [
                (BOB, 50, 2),
                (ALICE, 50, 1),
            ]

    asyncio.run(scenario())


def test_net_and_wins_tie_broken_by_name_case_insensitively(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            # "zed" has the lower user id and "abe" the higher, and "Zed" sorts before
            # "abe" byte-for-byte, so only a case-insensitive name comparison gives abe first.
            await db.record_flip(
                make_flip(GROUP_A, [Line(1, "Zed", 10), Line(2, "abe", 10)], seed_byte=1)
            )
            board = await db.leaderboard(GROUP_A)
            assert [r.display_name for r in board] == ["abe", "Zed"]

    asyncio.run(scenario())


def test_identical_names_and_stats_fall_back_to_user_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(
                make_flip(GROUP_A, [Line(9, "Sam", 10), Line(3, "Sam", 10)], seed_byte=1)
            )
            assert [r.user_id for r in await db.leaderboard(GROUP_A)] == [3, 9]

    asyncio.run(scenario())


def test_displayed_name_comes_from_highest_flip_number(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            # The second flip is recorded later but carries an earlier wall-clock time:
            # "most recent" is the flip number, not the timestamp.
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "Old Name", 10, "oldhandle"), Line(BOB, "Bob", -10)],
                    seed_byte=1,
                    started_at=EPOCH + timedelta(days=5),
                )
            )
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "New Name", 10, "newhandle"), Line(BOB, "Bob", -10)],
                    seed_byte=2,
                    started_at=EPOCH,
                )
            )
            stats = await db.player_stats(group_id=GROUP_A, user_id=ALICE)
            assert stats is not None
            assert (stats.display_name, stats.username, stats.net_dollars) == (
                "New Name",
                "newhandle",
                20,
            )
            board = await db.leaderboard(GROUP_A)
            assert board[0].display_name == "New Name"

    asyncio.run(scenario())


# --- Acceptance 3: username lookup ---------------------------------------------


def test_username_lookup_returns_most_recent_holder(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "Alice", 100, "annie"), Line(BOB, "Bob", -100)],
                    seed_byte=1,
                )
            )
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(CARA, "Cara", 20, "annie"), Line(BOB, "Bob", -20)],
                    seed_byte=2,
                )
            )
            holder = await db.player_stats_by_username(group_id=GROUP_A, username="annie")
            assert holder is not None
            assert holder.user_id == CARA
            assert holder.net_dollars == 20

            # The earlier holder is still reachable by id.
            earlier = await db.player_stats(group_id=GROUP_A, user_id=ALICE)
            assert earlier is not None
            assert earlier.net_dollars == 100

    asyncio.run(scenario())


def test_username_lookup_is_case_insensitive_and_ignores_at_sign(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "Alice", 5, "@annie"), Line(BOB, "Bob", -5)],
                    seed_byte=1,
                )
            )
            for spelling in ("annie", "@annie", "@AnNiE", "ANNIE", "  @Annie "):
                found = await db.player_stats_by_username(group_id=GROUP_A, username=spelling)
                assert found is not None, spelling
                assert found.user_id == ALICE
                assert found.username == "annie"  # stored without the @

    asyncio.run(scenario())


def test_username_lookup_unknown_empty_or_other_group_is_none(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "Alice", 5, "annie"), Line(BOB, "Bob", -5, None)],
                    seed_byte=1,
                )
            )
            for name in ("nobody", "", "@", "   ", "@  "):
                assert await db.player_stats_by_username(group_id=GROUP_A, username=name) is None
            # Right name, wrong group.
            assert await db.player_stats_by_username(group_id=GROUP_B, username="annie") is None

    asyncio.run(scenario())


# --- Acceptance 4: atomic recording, exact read-back ---------------------------


def test_stored_deal_reads_back_identical(tmp_path: Path) -> None:
    flip = make_flip(
        GROUP_A,
        [
            Line(ALICE, "Alice", 100, "alice", Fraction(7, 3), scooped=False),
            Line(BOB, "Bob", -75, None, Fraction(1, 3)),
            Line(CARA, "Cara", -25, "cara", Fraction(2, 3)),
        ],
        seed_byte=9,
        started_at=datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=timezone(timedelta(hours=8))),
    )

    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            number = await db.record_flip(flip)
            assert number == 1
            stored = await db.fetch_flip(group_id=GROUP_A, flip_number=number)
            assert stored is not None
            assert stored.flip_number == 1
            assert stored.flip == flip
            assert stored.flip.deal == flip.deal
            assert stored.flip.seed == flip.seed
            assert stored.flip.commitment == flip.commitment
            assert stored.flip.settings == flip.settings
            assert [p.points for p in stored.flip.players] == [p.points for p in flip.players]
            assert Fraction(1, 3) in [p.points for p in stored.flip.players]
            assert stored.flip.started_at == flip.started_at
            assert stored.flip.finished_at == flip.finished_at
            assert stored.flip.started_at.utcoffset() == timedelta(0)

    asyncio.run(scenario())


def test_scooped_flag_round_trips(tmp_path: Path) -> None:
    flip = make_flip(
        GROUP_A,
        [
            Line(ALICE, "Alice", 100, None, Fraction(3), scooped=True),
            Line(BOB, "Bob", -100, None, Fraction(0)),
        ],
        seed_byte=1,
    )

    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            number = await db.record_flip(flip)
            stored = await db.fetch_flip(group_id=GROUP_A, flip_number=number)
            assert stored is not None
            assert sorted(p.scooped for p in stored.flip.players) == [False, True]
            assert stored.flip == flip

    asyncio.run(scenario())


def test_flip_numbers_increase_and_fetch_is_scoped_to_group(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await record_two_groups(db)
            assert await db.fetch_flip(group_id=GROUP_A, flip_number=1) is not None
            assert await db.fetch_flip(group_id=GROUP_A, flip_number=3) is not None
            # Flip 4 was recorded in group B only.
            assert await db.fetch_flip(group_id=GROUP_A, flip_number=4) is None
            fetched = await db.fetch_flip(group_id=GROUP_B, flip_number=4)
            assert fetched is not None
            assert fetched.flip_number == 4
            assert await db.fetch_flip(group_id=GROUP_B, flip_number=1) is None
            assert await db.fetch_flip(group_id=GROUP_A, flip_number=999) is None

    asyncio.run(scenario())


def test_failure_part_way_through_recording_leaves_nothing(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    good = make_flip(GROUP_A, [Line(ALICE, "Alice", 50), Line(BOB, "Bob", -50)], seed_byte=1)
    # The flips row goes in first; the second player's value cannot be stored by sqlite,
    # so the failure comes during the player inserts.
    bad = make_flip(
        GROUP_A,
        [Line(ALICE, "Alice", 10), Line(BOB, "Bob", 2**63)],
        seed_byte=2,
    )
    after = make_flip(GROUP_A, [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)], seed_byte=3)

    async def scenario() -> None:
        async with await Database.open(path) as db:
            assert await db.record_flip(good) == 1
            board_before = await db.leaderboard(GROUP_A)
            assert count_rows(path, "flips") == 1
            assert count_rows(path, "flip_players") == 2

            with pytest.raises(OverflowError):
                await db.record_flip(bad)

            assert count_rows(path, "flips") == 1
            assert count_rows(path, "flip_players") == 2
            assert await db.leaderboard(GROUP_A) == board_before
            assert await db.fetch_flip(group_id=GROUP_A, flip_number=2) is None

            # The failed attempt consumed no flip number, and the database still works.
            assert await db.record_flip(after) == 2
            stored = await db.fetch_flip(group_id=GROUP_A, flip_number=2)
            assert stored is not None
            assert stored.flip == after

    asyncio.run(scenario())


def test_recording_with_a_token_retires_the_active_row_with_the_flip(tmp_path: Path) -> None:
    flip = make_flip(GROUP_A, [Line(ALICE, "Alice", 50), Line(BOB, "Bob", -50)], seed_byte=1)

    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("tok"))
            await db.add_active_flip(active("other", message_id=501))

            number = await db.record_flip(flip, token="tok")

            assert [row.token for row in await db.list_active_flips()] == ["other"]
            stored = await db.fetch_flip(group_id=GROUP_A, flip_number=number)
            assert stored is not None
            assert stored.flip == flip

    asyncio.run(scenario())


def test_recording_with_the_only_active_row_leaves_none_in_progress(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("tok"))
            await db.record_flip(
                make_flip(GROUP_A, [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)], seed_byte=1),
                token="tok",
            )
            assert await db.list_active_flips() == ()

    asyncio.run(scenario())


def test_recording_without_a_token_leaves_the_active_row_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("tok"))
            await db.record_flip(
                make_flip(GROUP_A, [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)], seed_byte=1)
            )
            assert [row.token for row in await db.list_active_flips()] == ["tok"]

    asyncio.run(scenario())


def test_recording_with_a_token_that_has_no_row_is_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("tok"))
            number = await db.record_flip(
                make_flip(GROUP_A, [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)], seed_byte=1),
                token="missing",
            )
            assert number == 1
            assert [row.token for row in await db.list_active_flips()] == ["tok"]

    asyncio.run(scenario())


def test_a_failed_record_leaves_the_active_row_in_place(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    bad = make_flip(
        GROUP_A,
        [Line(ALICE, "Alice", 10), Line(BOB, "Bob", 2**63)],
        seed_byte=2,
    )

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.add_active_flip(active("tok"))

            with pytest.raises(OverflowError):
                await db.record_flip(bad, token="tok")

            # The transaction rolled back as one: no flip, and the row still says
            # the flip is in progress.
            assert count_rows(path, "flips") == 0
            assert [row.token for row in await db.list_active_flips()] == ["tok"]

    asyncio.run(scenario())


def test_a_fetched_flip_carries_the_button_seat_and_player_count_of_its_deal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            flip = make_flip(
                GROUP_A,
                [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)],
                seed_byte=1,
            )
            number = await db.record_flip(flip)
            stored = await db.fetch_flip(group_id=GROUP_A, flip_number=number)
            assert stored is not None
            assert stored.flip.button_seat == flip.deal.button_seat
            assert stored.flip.player_count == len(flip.deal.hands) == 2

    asyncio.run(scenario())


# --- Cancellation and the connection lock --------------------------------------


def test_a_cancelled_call_keeps_the_connection_until_its_worker_has_finished() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()

        def work(connection: sqlite3.Connection) -> str:
            connection.execute("BEGIN IMMEDIATE")
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait()
            finally:
                connection.execute("ROLLBACK")
                loop.call_soon_threadsafe(finished.set)
            return "done"

        db = await Database.open(":memory:")
        try:
            blocked = asyncio.ensure_future(db._run(work))
            await asyncio.wait_for(entered.wait(), timeout=5)

            blocked.cancel()
            await asyncio.sleep(0.05)
            assert not blocked.done()
            assert not finished.is_set()

            follower = asyncio.ensure_future(db.fetch_flip(group_id=GROUP_A, flip_number=1))
            await asyncio.sleep(0.05)
            assert not follower.done()

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(blocked, timeout=5)
            assert finished.is_set()
            assert await asyncio.wait_for(follower, timeout=5) is None

            flip = make_flip(GROUP_A, [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)], seed_byte=1)
            number = await asyncio.wait_for(db.record_flip(flip), timeout=5)
            stored = await asyncio.wait_for(
                db.fetch_flip(group_id=GROUP_A, flip_number=number), timeout=5
            )
            assert stored is not None
            assert stored.flip == flip
        finally:
            release.set()
            await asyncio.wait_for(db.close(), timeout=5)

    asyncio.run(scenario())


# --- Acceptance 5: active flips survive reopen; migration ----------------------


def test_active_flips_survive_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    first = active("tok-1", created_at=EPOCH, topic_id=77, message_id=10)
    second = active(
        "tok-2",
        GROUP_B,
        created_at=EPOCH + timedelta(minutes=1),
        seed=seed_of(5),
        state=FlipState.RUNNING,
        message_id=20,
    )

    async def scenario() -> None:
        db = await Database.open(path)
        # Added out of age order; listing is oldest first regardless.
        await db.add_active_flip(second)
        await db.add_active_flip(first)
        await db.close()

        async with await Database.open(path) as reopened:
            assert await reopened.list_active_flips() == (first, second)

    asyncio.run(scenario())


def test_updates_to_active_flips_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.add_active_flip(active("tok", message_id=1))
            assert await db.update_active_flip("tok", message_id=2)
            assert await db.update_active_flip("tok", state=FlipState.RUNNING, seed=seed_of(8))
        async with await Database.open(path) as db:
            (row,) = await db.list_active_flips()
            assert row == active("tok", message_id=2, state=FlipState.RUNNING, seed=seed_of(8))

    asyncio.run(scenario())


def test_update_active_flip_none_leaves_fields_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            original = active("tok", message_id=3, seed=seed_of(2), state=FlipState.RUNNING)
            await db.add_active_flip(original)
            assert await db.update_active_flip("tok", message_id=4)
            (row,) = await db.list_active_flips()
            assert row.message_id == 4
            assert row.state is FlipState.RUNNING
            assert row.seed == seed_of(2)

    asyncio.run(scenario())


def test_update_active_flip_moves_one_rows_group_id_by_token(tmp_path: Path) -> None:
    """How a live flip's row follows the flip when its group is given a new id."""

    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("mine", GROUP_A, message_id=7))
            await db.add_active_flip(
                active("other", GROUP_A, created_at=EPOCH + timedelta(1), message_id=8)
            )

            assert await db.update_active_flip("mine", group_id=-1009999)

            rows = {row.token: row for row in await db.list_active_flips()}
            assert rows["mine"].group_id == -1009999
            assert rows["other"].group_id == GROUP_A  # left alone
            assert rows["mine"].message_id == 7
            assert rows["mine"].state is FlipState.LOBBY

    asyncio.run(scenario())


def test_update_active_flip_unknown_token_and_empty_update(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            assert await db.update_active_flip("ghost", message_id=1) is False
            await db.add_active_flip(active("tok"))
            with pytest.raises(ValueError, match="at least one"):
                await db.update_active_flip("tok")

    asyncio.run(scenario())


def test_remove_active_flip(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("tok"))
            assert await db.remove_active_flip("tok") is True
            assert await db.remove_active_flip("tok") is False
            assert await db.list_active_flips() == ()

    asyncio.run(scenario())


def test_duplicate_active_token_is_an_integrity_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.add_active_flip(active("tok"))
            with pytest.raises(sqlite3.IntegrityError):
                await db.add_active_flip(active("tok"))

    asyncio.run(scenario())


def test_migrate_group_id_moves_history_and_active_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await record_two_groups(db)
            await db.add_active_flip(active("mine", GROUP_A))
            await db.add_active_flip(active("other", GROUP_B, created_at=EPOCH + timedelta(1)))
            before_a = await db.leaderboard(GROUP_A)
            before_b = await db.leaderboard(GROUP_B)

            new_id = -1009999
            result = await db.migrate_group_id(old_group_id=GROUP_A, new_group_id=new_id)
            assert result == GroupMigration(flips=3, active_flips=1)
            assert result.rows == 4

            assert await db.leaderboard(GROUP_A) == ()
            assert await db.leaderboard(new_id) == before_a
            assert await db.leaderboard(GROUP_B) == before_b  # untouched
            assert await db.fetch_flip(group_id=GROUP_A, flip_number=1) is None
            moved = await db.fetch_flip(group_id=new_id, flip_number=1)
            assert moved is not None
            assert moved.flip.group_id == new_id
            assert (
                await db.player_stats_by_username(group_id=new_id, username="alice")
            ) == before_a[0]

            groups = {row.token: row.group_id for row in await db.list_active_flips()}
            assert groups == {"mine": new_id, "other": GROUP_B}

    asyncio.run(scenario())


def test_migrate_group_id_same_id_and_empty_group_are_no_ops(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await record_two_groups(db)
            same = await db.migrate_group_id(old_group_id=GROUP_A, new_group_id=GROUP_A)
            assert same == GroupMigration(0, 0)
            assert same.rows == 0
            nothing = await db.migrate_group_id(old_group_id=-555, new_group_id=-666)
            assert nothing == GroupMigration(0, 0)
            assert len(await db.leaderboard(GROUP_A)) == 3

    asyncio.run(scenario())


def test_migrated_rows_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await record_two_groups(db)
            await db.add_active_flip(active("tok", GROUP_A))
            await db.migrate_group_id(old_group_id=GROUP_A, new_group_id=-42)
        async with await Database.open(path) as db:
            assert len(await db.leaderboard(-42)) == 3
            (row,) = await db.list_active_flips()
            assert row.group_id == -42

    asyncio.run(scenario())


# --- Acceptance 6: never-played players ----------------------------------------


def test_stats_for_a_player_who_never_played_are_none(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            # Empty database, then a group with flips but without this player.
            assert await db.player_stats(group_id=GROUP_A, user_id=12345) is None
            assert await db.player_stats_by_username(group_id=GROUP_A, username="x") is None
            assert await db.leaderboard(GROUP_A) == ()
            await record_two_groups(db)
            assert await db.player_stats(group_id=GROUP_A, user_id=12345) is None

    asyncio.run(scenario())


# --- Corrupt data and schema ---------------------------------------------------


def tamper(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sync_connection(path)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(statement, parameters)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE flips SET deal = 'not json'",
        "UPDATE flips SET deal = '{}'",
        "UPDATE flips SET seed = '" + "zz" * 32 + "'",
        "UPDATE flips SET started_at = 'yesterday-ish'",
        "UPDATE flips SET started_at = '2026-01-01T00:00:00'",
        "UPDATE flip_players SET points = 'lots'",
        "UPDATE flip_players SET points = '1/0'",
        "UPDATE flip_players SET points = '-1'",
        "UPDATE flip_players SET net_dollars = 'lots'",
        # Not a whole number, so the column's INTEGER affinity leaves it a float.
        "UPDATE flip_players SET net_dollars = 1.5",
        # One seat only: anything truthy in every seat is caught as two scoopers
        # instead, and what is in question here is the one damaged flag.
        "UPDATE flip_players SET scooped = 'yes' WHERE seat = 0",
        "UPDATE flip_players SET scooped = 2 WHERE seat = 0",
        "DELETE FROM flip_players WHERE seat = 0",
        # A fraction sqlite's INTEGER affinity keeps as a REAL; int() would have read
        # each of these back as a plausible whole number.
        "UPDATE flips SET wager = 50.9",
        "UPDATE flips SET x = 5.9",
        "UPDATE flips SET y = 2.9",
        "UPDATE flips SET z = 3.9",
        "UPDATE flips SET player_count = 2.5",
        "UPDATE flips SET button_seat = 0.9",
        "UPDATE flip_players SET seat = 0.9 WHERE seat = 0",
        "UPDATE flip_players SET user_id = 11.5 WHERE user_id = 11",
        # The flip row's own figures have to agree with its deal.
        "UPDATE flips SET button_seat = 1 - button_seat",
        "UPDATE flips SET player_count = 3",
    ],
)
def test_corrupt_flip_row_raises_corrupt_record_error(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            number = await db.record_flip(
                make_flip(GROUP_A, [Line(ALICE, "Alice", 5), Line(BOB, "Bob", -5)], seed_byte=1)
            )
            tamper(path, statement)
            with pytest.raises(CorruptRecordError) as caught:
                await db.fetch_flip(group_id=GROUP_A, flip_number=number)
            assert isinstance(caught.value, StorageError)
            assert caught.value.record == f"flip {number}"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE active_flips SET state = 'sideways'",
        "UPDATE active_flips SET seed = '" + "zz" * 32 + "'",
        "UPDATE active_flips SET created_at = 'never'",
        "UPDATE active_flips SET created_at = '2026-01-01T00:00:00'",
        "UPDATE active_flips SET group_id = -1001.5",
        "UPDATE active_flips SET message_id = 500.5",
        "UPDATE active_flips SET topic_id = 7.5",
    ],
)
def test_corrupt_active_row_raises_corrupt_record_error(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.add_active_flip(active("tok", seed=seed_of(1)))
            tamper(path, statement)
            with pytest.raises(CorruptRecordError) as caught:
                await db.list_active_flips()
            assert "tok" in caught.value.record

    asyncio.run(scenario())


async def _read_stats(db: Database, read: str) -> object:
    """One of the three stats reads, aimed at Alice's group."""
    if read == "leaderboard":
        return await db.leaderboard(group_id=GROUP_A)
    if read == "player_stats":
        return await db.player_stats(group_id=GROUP_A, user_id=ALICE)
    return await db.player_stats_by_username(group_id=GROUP_A, username="alice")


async def _record_alice_and_bob(db: Database) -> None:
    await db.record_flip(
        make_flip(
            GROUP_A,
            [Line(ALICE, "Alice", 5, username="alice"), Line(BOB, "Bob", -5, username="bob")],
            seed_byte=1,
        )
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE flip_players SET scooped = 3",
        "UPDATE flip_players SET scooped = -1",
    ],
)
@pytest.mark.parametrize("read", ["leaderboard", "player_stats", "player_stats_by_username"])
def test_impossible_player_totals_raise_corrupt_record_error_from_every_stats_read(
    tmp_path: Path, statement: str, read: str
) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await _record_alice_and_bob(db)
            tamper(path, statement)
            with pytest.raises(CorruptRecordError) as caught:
                await _read_stats(db, read)
            assert isinstance(caught.value, StorageError)
            # The leaderboard reads both damaged rows and may trip on either first.
            assert caught.value.record in {
                f"stats for player {ALICE} in group {GROUP_A}",
                f"stats for player {BOB} in group {GROUP_A}",
            }
            assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE flip_players SET net_dollars = 'lots'",
        # Not a whole number, so the column's INTEGER affinity leaves it a float.
        "UPDATE flip_players SET net_dollars = 1.5",
    ],
    ids=["text", "fraction"],
)
@pytest.mark.parametrize("read", ["leaderboard", "player_stats", "player_stats_by_username"])
def test_a_damaged_net_dollars_raises_corrupt_record_error_from_every_stats_read(
    tmp_path: Path, statement: str, read: str
) -> None:
    """Money that is not a whole number cannot be summed into anything meaningful.

    sqlite would add text up as zero and a fraction as a fraction, so without a
    check of its own every stats read would report a figure that looks like money.
    """
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await _record_alice_and_bob(db)
            tamper(path, statement)
            with pytest.raises(CorruptRecordError) as caught:
                await _read_stats(db, read)
            assert isinstance(caught.value, StorageError)
            # The leaderboard reads both damaged rows and may trip on either first.
            assert caught.value.record in {
                f"stats for player {ALICE} in group {GROUP_A}",
                f"stats for player {BOB} in group {GROUP_A}",
            }
            assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE flip_players SET scooped = 'yes'",
        # Neither 0 nor 1, so the column's INTEGER affinity leaves it a float.
        "UPDATE flip_players SET scooped = 0.5",
    ],
    ids=["text", "fraction"],
)
@pytest.mark.parametrize("read", ["leaderboard", "player_stats", "player_stats_by_username"])
def test_a_damaged_scoop_flag_raises_corrupt_record_error_from_every_stats_read(
    tmp_path: Path, statement: str, read: str
) -> None:
    """A flag that is neither 0 nor 1 counts as no scoop at all when summed.

    Which is a figure nothing can object to, so the rows are counted rather than
    left to the totals.
    """
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await _record_alice_and_bob(db)
            tamper(path, statement)
            with pytest.raises(CorruptRecordError) as caught:
                await _read_stats(db, read)
            assert isinstance(caught.value, StorageError)
            # The leaderboard reads both damaged rows and may trip on either first.
            assert caught.value.record in {
                f"stats for player {ALICE} in group {GROUP_A}",
                f"stats for player {BOB} in group {GROUP_A}",
            }
            assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


def test_a_scoop_flag_in_range_only_by_chance_still_raises(tmp_path: Path) -> None:
    """A 2 where a 1 belongs can still add up to a believable number of scoops.

    Alice scooped one of her two flips, so a 2 on that row totals two scoops in
    two flips: nothing the totals on their own could object to.
    """
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await record_two_groups(db)
            tamper(
                path,
                "UPDATE flip_players SET scooped = 2 WHERE user_id = ? AND scooped = 1",
                (ALICE,),
            )
            with pytest.raises(CorruptRecordError) as caught:
                await db.player_stats(group_id=GROUP_A, user_id=ALICE)
            assert caught.value.record == f"stats for player {ALICE} in group {GROUP_A}"
            assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


@pytest.mark.parametrize("read", ["leaderboard", "player_stats_by_username"])
def test_a_damaged_user_id_raises_corrupt_record_error_from_the_reads_that_find_it(
    tmp_path: Path, read: str
) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await _record_alice_and_bob(db)
            # One row only: the unique (flip_number, user_id) index still applies.
            tamper(path, "UPDATE flip_players SET user_id = 'abc' WHERE user_id = ?", (ALICE,))
            with pytest.raises(CorruptRecordError) as caught:
                await _read_stats(db, read)
            assert isinstance(caught.value, StorageError)
            assert caught.value.record == f"stats for player 'abc' in group {GROUP_A}"
            assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


@pytest.mark.parametrize("read", ["leaderboard", "player_stats_by_username"])
def test_a_fractional_user_id_raises_corrupt_record_error_from_the_reads_that_find_it(
    tmp_path: Path, read: str
) -> None:
    """A user id of 11.5 must not be read back as player 11."""
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await _record_alice_and_bob(db)
            tamper(path, "UPDATE flip_players SET user_id = 11.5 WHERE user_id = ?", (ALICE,))
            with pytest.raises(CorruptRecordError) as caught:
                await _read_stats(db, read)
            assert isinstance(caught.value, StorageError)
            assert caught.value.record == f"stats for player 11.5 in group {GROUP_A}"
            assert isinstance(caught.value.__cause__, ValueError)

    asyncio.run(scenario())


def test_corrupt_error_keeps_the_underlying_cause() -> None:
    error = CorruptRecordError("flip 3", "bad seed")
    assert error.record == "flip 3"
    assert error.reason == "bad seed"
    assert "flip 3" in str(error)


def test_new_database_is_at_current_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            assert db.path == str(path)

    asyncio.run(scenario())
    connection = sync_connection(path)
    try:
        assert schema.schema_version(connection) == SCHEMA_VERSION == 2
        assert schema.apply_migrations(connection) == SCHEMA_VERSION  # idempotent
    finally:
        connection.close()


def test_schema_too_new_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    connection = sync_connection(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    async def scenario() -> None:
        with pytest.raises(SchemaTooNewError) as caught:
            await Database.open(path)
        assert caught.value.found == SCHEMA_VERSION + 1
        assert caught.value.supported == SCHEMA_VERSION
        assert isinstance(caught.value, StorageError)

    asyncio.run(scenario())


def test_calls_after_close_fail(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await Database.open(tmp_path / "bot.db")
        await db.close()
        await db.close()  # harmless
        with pytest.raises(sqlite3.ProgrammingError):
            await db.leaderboard(GROUP_A)

    asyncio.run(scenario())


# --- Small pieces --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("@", None),
        ("@ ", None),
        ("annie", "annie"),
        ("@annie", "annie"),
        ("  @Annie  ", "Annie"),
        ("@@annie", "@annie"),
    ],
)
def test_normalize_username(raw: str | None, expected: str | None) -> None:
    assert normalize_username(raw) == expected


def test_win_rate_and_migration_rows() -> None:
    assert PlayerStats(1, "A", None, 0, 0, 0, 0).win_rate == 0.0
    assert PlayerStats(1, "A", None, 5, 4, 1, 0).win_rate == 0.25
    assert GroupMigration(2, 3).rows == 5


def test_stored_values_are_in_normalised_and_utc_form(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(
                make_flip(
                    GROUP_A,
                    [Line(ALICE, "Alice", 5, "@Annie"), Line(BOB, "Bob", -5, "  ")],
                    seed_byte=1,
                    started_at=datetime(2026, 1, 1, 20, 0, tzinfo=timezone(timedelta(hours=8))),
                )
            )

    asyncio.run(scenario())
    connection = sync_connection(path)
    try:
        usernames = {
            row["user_id"]: row["username"]
            for row in connection.execute("SELECT user_id, username FROM flip_players")
        }
        assert usernames == {ALICE: "Annie", BOB: None}
        started = connection.execute("SELECT started_at FROM flips").fetchone()[0]
        assert datetime.fromisoformat(started) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert started.endswith("+00:00")
    finally:
        connection.close()


# --- The last flip's settings, for /again ---------------------------------------

PAIR = [Line(ALICE, "Alice", 50), Line(BOB, "Bob", -50)]
TRIO = [Line(ALICE, "Alice", 50), Line(BOB, "Bob", -25), Line(CARA, "Cara", -25)]


def request_flip(
    group_id: int,
    settings: Settings,
    *,
    seed_byte: int,
    y_defaulted: bool = False,
    z_defaulted: bool = False,
    lines: list[Line] | None = None,
    started_at: datetime = EPOCH,
) -> FinishedFlip:
    """A genuine flip whose settings were (or were not) filled in by the bot."""
    flip = make_flip(
        group_id,
        PAIR if lines is None else lines,
        seed_byte=seed_byte,
        settings=settings,
        started_at=started_at,
    )
    return replace(flip, y_defaulted=y_defaulted, z_defaulted=z_defaulted)


def last_row(path: Path, group_id: int) -> tuple[object, ...] | None:
    connection = sync_connection(path)
    try:
        row = connection.execute(
            "SELECT wager, x, y, z, y_defaulted, z_defaulted, updated_at "
            "FROM group_last_flip WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        return None if row is None else tuple(row)
    finally:
        connection.close()


def v1_database(path: Path) -> sqlite3.Connection:
    """A file exactly as version 1 left it, ready for raw inserts."""
    connection = schema.connect(path)
    for statement in _MIGRATION_1:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")
    return connection


V1_SEED = "ab" * fairness.SEED_SIZE
V1_COMMITMENT = "cd" * 32


def v1_flip(
    connection: sqlite3.Connection,
    group_id: int,
    settings: tuple[int, int, int, int],
    finished_at: str,
    players: tuple[int, ...] = (ALICE, BOB),
) -> int:
    wager, x, y, z = settings
    cursor = connection.execute(
        "INSERT INTO flips (group_id, wager, x, y, z, player_count, button_seat, seed, "
        "commitment, deal, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, '{}', ?, ?)",
        (
            group_id,
            wager,
            x,
            y,
            z,
            len(players),
            V1_SEED,
            V1_COMMITMENT,
            "2026-01-01T00:00:00+00:00",
            finished_at,
        ),
    )
    number = cursor.lastrowid
    assert number is not None
    for seat, user_id in enumerate(players):
        connection.execute(
            "INSERT INTO flip_players VALUES (?, ?, ?, NULL, ?, '1', 0, 0)",
            (number, user_id, f"user{user_id}", seat),
        )
    return number


def dump(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sync_connection(path)
    try:
        return {
            "flips": [
                tuple(r)
                for r in connection.execute(
                    "SELECT flip_number, group_id, wager, x, y, z, player_count, button_seat, "
                    "seed, commitment, deal, started_at, finished_at FROM flips ORDER BY 1"
                )
            ],
            "players": [
                tuple(r) for r in connection.execute("SELECT * FROM flip_players ORDER BY 1, 4")
            ],
        }
    finally:
        connection.close()


def test_a_version_one_file_is_stepped_up_to_version_two(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = v1_database(path)
    v1_flip(connection, GROUP_A, (50, 5, 2, 3), "2026-01-01T10:00:00+00:00")
    v1_flip(connection, GROUP_A, (20, 7, 6, 9), "2026-01-02T10:00:00+00:00", (ALICE, BOB, CARA))
    v1_flip(connection, GROUP_B, (10, 5, 1, 1), "2026-01-03T10:00:00+00:00")
    v1_flip(connection, GROUP_B, (30, 6, 7, 2), "2026-01-01T09:00:00+00:00")
    connection.close()
    before = dump(path)

    async def scenario() -> None:
        async with await Database.open(path) as db:
            assert await db.last_flip_request(GROUP_A) == FlipRequest(20, 7, 6, 9)
            # The newest flip number wins, not the latest finished_at.
            assert await db.last_flip_request(GROUP_B) == FlipRequest(30, 6, 7, 2)
            assert await db.last_flip_request(-5) is None

    asyncio.run(scenario())

    connection = sync_connection(path)
    try:
        assert schema.schema_version(connection) == 2
        assert connection.execute("SELECT COUNT(*) FROM group_last_flip").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM flips WHERE y_defaulted <> 0 OR z_defaulted <> 0"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    assert dump(path) == before
    assert last_row(path, GROUP_A) == (20, 7, 6, 9, 0, 0, "2026-01-02T10:00:00+00:00")
    assert last_row(path, GROUP_B) == (30, 6, 7, 2, 0, 0, "2026-01-01T09:00:00+00:00")


def test_the_upgraded_flips_read_back_as_typed_in_full(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = v1_database(path)
    number = v1_flip(connection, GROUP_A, (50, 5, 2, 3), "2026-01-01T10:00:00+00:00")
    connection.close()
    connection = schema.connect(path)
    try:
        schema.apply_migrations(connection)
        row = connection.execute(
            "SELECT y_defaulted, z_defaulted FROM flips WHERE flip_number = ?", (number,)
        ).fetchone()
        assert tuple(row) == (0, 0)
    finally:
        connection.close()


def test_a_version_one_file_without_flips_gets_an_empty_last_flip_table(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    v1_database(path).close()
    connection = schema.connect(path)
    try:
        assert schema.apply_migrations(connection) == 2
        assert connection.execute("SELECT COUNT(*) FROM group_last_flip").fetchone()[0] == 0
    finally:
        connection.close()


def test_the_upgrade_is_idempotent_and_a_current_file_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = v1_database(path)
    v1_flip(connection, GROUP_A, (50, 5, 2, 3), "2026-01-01T10:00:00+00:00")
    connection.close()
    connection = schema.connect(path)
    try:
        assert schema.apply_migrations(connection) == 2
        # Someone changes the row after the upgrade; a second run must not rebuild it.
        connection.execute("UPDATE group_last_flip SET wager = 99")
        master = connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        assert schema.apply_migrations(connection) == 2
        assert schema.schema_version(connection) == 2
        assert connection.execute("SELECT wager FROM group_last_flip").fetchone()[0] == 99
        assert connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == master
        assert connection.execute("SELECT COUNT(*) FROM flips").fetchone()[0] == 1
    finally:
        connection.close()


def test_a_fresh_file_and_an_upgraded_file_have_the_same_schema(tmp_path: Path) -> None:
    fresh = schema.connect(tmp_path / "fresh.db")
    upgraded = v1_database(tmp_path / "old.db")
    try:
        assert schema.apply_migrations(fresh) == 2
        assert schema.apply_migrations(upgraded) == 2
        query = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        assert [tuple(r) for r in fresh.execute(query)] == [
            tuple(r) for r in upgraded.execute(query)
        ]
        for table in ("flips", "group_last_flip"):
            columns = [tuple(r) for r in fresh.execute(f"PRAGMA table_info({table})")]
            assert columns == [tuple(r) for r in upgraded.execute(f"PRAGMA table_info({table})")]
        assert "y_defaulted" in {r["name"] for r in fresh.execute("PRAGMA table_info(flips)")}
    finally:
        fresh.close()
        upgraded.close()


@pytest.mark.parametrize(
    ("y_defaulted", "z_defaulted"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_the_defaulted_flags_round_trip(
    tmp_path: Path, y_defaulted: bool, z_defaulted: bool
) -> None:
    settings = Settings(wager=50, x=5, y=Y_DEFAULT if y_defaulted else 3, z=3 if z_defaulted else 7)
    flip = request_flip(
        GROUP_A,
        settings,
        seed_byte=1,
        y_defaulted=y_defaulted,
        z_defaulted=z_defaulted,
        lines=TRIO,
    )

    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            number = await db.record_flip(flip)
            stored = await db.fetch_flip(group_id=GROUP_A, flip_number=number)
            assert stored is not None
            assert stored.flip == flip
            assert stored.flip.y_defaulted is y_defaulted
            assert stored.flip.z_defaulted is z_defaulted
            assert stored.flip.settings == settings
            expected = FlipRequest(50, 5, None if y_defaulted else 3, None if z_defaulted else 7)
            assert stored.flip.request == expected
            assert await db.last_flip_request(GROUP_A) == expected

    asyncio.run(scenario())

    connection = sync_connection(tmp_path / "bot.db")
    try:
        row = connection.execute("SELECT y, z, y_defaulted, z_defaulted FROM flips").fetchone()
        assert tuple(row) == (settings.y, settings.z, int(y_defaulted), int(z_defaulted))
    finally:
        connection.close()


def test_a_group_that_never_finished_a_flip_has_no_last_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            assert await db.last_flip_request(GROUP_A) is None
            await db.record_flip(request_flip(GROUP_B, SETTINGS_ONE, seed_byte=1))
            assert await db.last_flip_request(GROUP_A) is None

    asyncio.run(scenario())


SETTINGS_ONE = Settings(wager=50, x=5, y=2, z=3)
SETTINGS_TWO = Settings(wager=10, x=6, y=1, z=8)


def test_the_last_request_is_the_newest_flip_even_when_settings_change(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1))
            assert await db.last_flip_request(GROUP_A) == FlipRequest(50, 5, 2, 3)
            await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=2))
            await db.record_flip(
                request_flip(GROUP_A, SETTINGS_TWO, seed_byte=3, started_at=EPOCH + timedelta(1))
            )
            assert await db.last_flip_request(GROUP_A) == FlipRequest(10, 6, 1, 8)
            # Back to a defaulted one: the request forgets the figures.
            await db.record_flip(
                request_flip(
                    GROUP_A,
                    Settings(wager=10, x=6, y=Y_DEFAULT, z=2),
                    seed_byte=4,
                    y_defaulted=True,
                    z_defaulted=True,
                )
            )
            assert await db.last_flip_request(GROUP_A) == FlipRequest(10, 6)

    asyncio.run(scenario())
    assert count_rows(tmp_path / "bot.db", "group_last_flip") == 1


def test_last_requests_are_kept_per_group(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1))
            await db.record_flip(request_flip(GROUP_B, SETTINGS_TWO, seed_byte=2))
            row_b = last_row(path, GROUP_B)
            await db.record_flip(
                request_flip(
                    GROUP_A,
                    Settings(wager=70, x=6, y=Y_DEFAULT, z=2),
                    seed_byte=3,
                    y_defaulted=True,
                )
            )
            assert await db.last_flip_request(GROUP_A) == FlipRequest(70, 6, None, 2)
            assert await db.last_flip_request(GROUP_B) == FlipRequest(10, 6, 1, 8)
            assert last_row(path, GROUP_B) == row_b

    asyncio.run(scenario())
    assert count_rows(path, "group_last_flip") == 2


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE group_last_flip SET y_defaulted = 'yes'",
        "UPDATE group_last_flip SET z_defaulted = 7",
        "UPDATE group_last_flip SET z_defaulted = 1.5",
        "UPDATE group_last_flip SET wager = 'lots'",
        "UPDATE group_last_flip SET x = 0",
    ],
)
def test_a_damaged_last_flip_row_raises_corrupt_record_error(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1))
        tamper(path, statement)
        async with await Database.open(path) as db:
            with pytest.raises(CorruptRecordError) as caught:
                await db.last_flip_request(GROUP_A)
            assert caught.value.record == f"the last flip settings of group {GROUP_A}"
            assert await db.last_flip_request(GROUP_B) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("column", ["y_defaulted", "z_defaulted"])
def test_a_damaged_flag_on_a_flip_raises_corrupt_record_error(tmp_path: Path, column: str) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            number = await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1))
        tamper(path, f"UPDATE flips SET {column} = 7")
        async with await Database.open(path) as db:
            with pytest.raises(CorruptRecordError):
                await db.fetch_flip(group_id=GROUP_A, flip_number=number)

    asyncio.run(scenario())


def test_a_failed_record_leaves_the_last_flip_row_as_it_was(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    good = request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1, started_at=EPOCH)
    bad = request_flip(
        GROUP_A,
        SETTINGS_TWO,
        seed_byte=2,
        lines=[Line(ALICE, "Alice", 10), Line(BOB, "Bob", 2**63)],
        started_at=EPOCH + timedelta(days=1),
    )

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(good)
            row = last_row(path, GROUP_A)
            assert row is not None

            with pytest.raises(OverflowError):
                await db.record_flip(bad)

            assert last_row(path, GROUP_A) == row
            assert await db.last_flip_request(GROUP_A) == FlipRequest(50, 5, 2, 3)
            assert count_rows(path, "flips") == 1

    asyncio.run(scenario())


def test_a_failed_first_record_leaves_no_last_flip_row(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    bad = request_flip(
        GROUP_A,
        SETTINGS_ONE,
        seed_byte=2,
        lines=[Line(ALICE, "Alice", 10), Line(BOB, "Bob", 2**63)],
    )

    async def scenario() -> None:
        async with await Database.open(path) as db:
            with pytest.raises(OverflowError):
                await db.record_flip(bad)
            assert await db.last_flip_request(GROUP_A) is None

    asyncio.run(scenario())
    assert count_rows(path, "group_last_flip") == 0


def test_only_recording_a_flip_writes_the_last_flip_row(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            # A flip that is opened, then cancelled or voided, is never recorded.
            await db.add_active_flip(active("tok", GROUP_A))
            await db.update_active_flip("tok", state=FlipState.RUNNING)
            await db.remove_active_flip("tok")
            assert await db.last_flip_request(GROUP_A) is None
            assert count_rows(path, "group_last_flip") == 0

            await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1))
            row = last_row(path, GROUP_A)
            await db.add_active_flip(active("tok2", GROUP_A))
            await db.remove_active_flip("tok2")
            assert last_row(path, GROUP_A) == row

    asyncio.run(scenario())


def test_migrate_group_id_moves_the_last_flip_row(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    new_id = -1009999

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(request_flip(GROUP_A, SETTINGS_ONE, seed_byte=1))
            await db.record_flip(request_flip(GROUP_B, SETTINGS_TWO, seed_byte=2))
            row = last_row(path, GROUP_A)

            result = await db.migrate_group_id(old_group_id=GROUP_A, new_group_id=new_id)
            assert result == GroupMigration(flips=1, active_flips=0)

            assert await db.last_flip_request(GROUP_A) is None
            assert await db.last_flip_request(new_id) == FlipRequest(50, 5, 2, 3)
            assert await db.last_flip_request(GROUP_B) == FlipRequest(10, 6, 1, 8)
            assert last_row(path, new_id) == row

    asyncio.run(scenario())
    assert count_rows(path, "group_last_flip") == 2


def test_migrate_group_id_with_no_last_flip_row_or_the_same_id_changes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.db"

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(request_flip(GROUP_B, SETTINGS_TWO, seed_byte=2))
            nothing = await db.migrate_group_id(old_group_id=GROUP_A, new_group_id=-77)
            assert nothing == GroupMigration(0, 0)
            assert await db.last_flip_request(-77) is None
            assert await db.last_flip_request(GROUP_A) is None

            same = await db.migrate_group_id(old_group_id=GROUP_B, new_group_id=GROUP_B)
            assert same == GroupMigration(0, 0)
            assert await db.last_flip_request(GROUP_B) == FlipRequest(10, 6, 1, 8)

    asyncio.run(scenario())
    assert count_rows(path, "group_last_flip") == 1


@pytest.mark.parametrize(
    ("old_offset", "new_offset", "kept_old"),
    [(2, 1, True), (1, 2, False), (1, 1, False)],
    ids=["old-newer", "new-newer", "tie-keeps-new"],
)
def test_migrate_group_id_keeps_the_newer_of_two_last_flip_rows(
    tmp_path: Path, old_offset: int, new_offset: int, kept_old: bool
) -> None:
    path = tmp_path / "bot.db"
    new_id = -2002

    async def scenario() -> None:
        async with await Database.open(path) as db:
            await db.record_flip(
                request_flip(
                    GROUP_A, SETTINGS_ONE, seed_byte=1, started_at=EPOCH + timedelta(old_offset)
                )
            )
            await db.record_flip(
                request_flip(
                    new_id, SETTINGS_TWO, seed_byte=2, started_at=EPOCH + timedelta(new_offset)
                )
            )
            result = await db.migrate_group_id(old_group_id=GROUP_A, new_group_id=new_id)
            assert result == GroupMigration(flips=1, active_flips=0)

            assert await db.last_flip_request(GROUP_A) is None
            expected = FlipRequest(50, 5, 2, 3) if kept_old else FlipRequest(10, 6, 1, 8)
            assert await db.last_flip_request(new_id) == expected

    asyncio.run(scenario())
    assert count_rows(path, "group_last_flip") == 1


def test_the_async_database_exposes_the_last_flip_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with await Database.open(tmp_path / "bot.db") as db:
            assert await db.last_flip_request(GROUP_A) is None
            await db.record_flip(
                request_flip(
                    GROUP_A, Settings(wager=5, x=5, y=Y_DEFAULT, z=2), seed_byte=1, y_defaulted=True
                )
            )
            assert await db.last_flip_request(GROUP_A) == FlipRequest(5, 5, None, 2)

    asyncio.run(scenario())

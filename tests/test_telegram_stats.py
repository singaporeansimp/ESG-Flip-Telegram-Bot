"""What ``/leaderboard`` and ``/stats`` say when a group asks for its figures.

Every test records real finished flips into the harness's in-memory database,
feeds the command through the real dispatcher, and reads what the fake Telegram
was asked to send. So the ranking, the win rule and the username lookup are the
SQL the bot ships, and what is pinned here is what a group would see: the order
of the rows, the four figures on each, who a command is about (the asker, a
``@name``, or the person replied to), the empty and error replies, and the output
rules -- escaped names, clipped names, one message inside Telegram's limit, and
the reply landing in the topic it was asked in.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from fractions import Fraction

import pytest
from aiogram.types import Message

from esgbot.storage import StorageError
from esgbot.telegram import render
from esgbot.telegram.keyboards import Press, pack
from tests.engine_helpers import GROUP
from tests.storage_helpers import Line, make_flip
from tests.telegram_helpers import (
    STARTER,
    TOPIC,
    BotHarness,
    bot_harness,
    button_press,
    group_message,
    topic_message,
    user,
)

_OTHER_GROUP = -1002222222222
_PAT = 12

_ROW = re.compile(r"^\d+\. ")


async def _record(world: BotHarness, lines: list[Line], *, seed: int, group_id: int = GROUP) -> int:
    """Put one finished flip into the history and return its number."""
    return await world.store.record_flip(make_flip(group_id, lines, seed_byte=seed))


async def _ask(world: BotHarness, text: str, *, sender_id: int = STARTER) -> str:
    """Send a command from a group member and return the one reply it got."""
    sends_before = len(world.client.sends)
    await world.feed(
        message=group_message(text, sender=user(sender_id, name="Sam", username="sam"))
    )
    assert len(world.client.sends) == sends_before + 1
    return world.client.sends[-1].text


def _rows(text: str) -> list[str]:
    """The numbered rows of a leaderboard reply."""
    return [line for line in text.splitlines() if _ROW.match(line)]


def _ranking(text: str) -> list[str]:
    """The names on a leaderboard reply, best first."""
    return [row.split(". ", 1)[1].split(" — ")[0] for row in _rows(text)]


def _reply_to(text: str, *, sender_id: int | None, name: str = "Pat") -> Message:
    """A command sent as a reply to a message from someone else, or from nobody."""
    original = group_message("hi", sender=user(sender_id or 0, name=name, username=None))
    if sender_id is None:
        original = original.model_copy(update={"from_user": None})
    return group_message(text).model_copy(update={"reply_to_message": original})


async def _tie_break_history(world: BotHarness) -> None:
    """Seven players in which each tie-break in turn is the one that decides.

    Top leads on net. Many, Few tie on net and Many has more wins. The two bravos
    and Charlie tie on both, and are told apart by name without regard to case and
    then by user id. Loser has only losses.
    """
    await _record(
        world,
        [
            Line(1, "Top", 300),
            Line(2, "Many", 100),
            Line(3, "Few", 100),
            Line(4, "Charlie", 50),
            Line(6, "bravo", 50),
            Line(5, "bravo", 50),
            Line(7, "Loser", -200),
        ],
        seed=1,
    )
    await _record(world, [Line(2, "Many", 100), Line(3, "Few", 0), Line(7, "Loser", 0)], seed=2)
    await _record(world, [Line(2, "Many", -100), Line(3, "Few", 0)], seed=3)


# ------------------------------------------------------------------ leaderboard


def test_leaderboard_ranks_by_net_then_wins_then_name_without_case_then_user_id() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _tie_break_history(world)

        text = await _ask(world, "/leaderboard")

        # Many beats Few on wins at the same net. "bravo" sorts before "Charlie"
        # once case is ignored (raw order would put Charlie first), and the two
        # identical names are settled by user id: 5 before 6.
        assert _ranking(text) == ["Top", "Many", "Few", "bravo", "bravo", "Charlie", "Loser"]
        ranked = await world.store.leaderboard(GROUP)
        assert [stats.user_id for stats in ranked] == [1, 2, 3, 5, 6, 4, 7]
        await world.close()

    asyncio.run(scenario())


def test_leaderboard_rows_show_net_flips_played_win_rate_and_scoops() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _tie_break_history(world)

        rows = _rows(await _ask(world, "/leaderboard"))

        assert rows[0].endswith("Top — +$300 · 1 flip · 1 win (100%) · 0 scoops")
        assert rows[1].endswith("Many — +$100 · 3 flips · 2 wins (67%) · 0 scoops")
        assert rows[2].endswith("Few — +$100 · 3 flips · 1 win (33%) · 0 scoops")
        assert rows[6].endswith("Loser — -$200 · 2 flips · 0 wins (0%) · 0 scoops")
        await world.close()

    asyncio.run(scenario())


def test_a_player_with_only_losses_is_listed_with_a_negative_net() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 50), Line(2, "Bob", -50)], seed=1)
        await _record(world, [Line(1, "Ann", 25), Line(2, "Bob", -25)], seed=2)

        rows = _rows(await _ask(world, "/leaderboard"))

        assert len(rows) == 2
        assert rows[1].endswith("Bob — -$75 · 2 flips · 0 wins (0%) · 0 scoops")
        await world.close()

    asyncio.run(scenario())


def test_a_chop_winner_who_netted_nothing_played_but_did_not_win() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 0), Line(2, "Bob", 0)], seed=1)

        only_chops = await _ask(world, "/stats", sender_id=1)
        assert "$0 · 1 flip · 0 wins (0%) · 0 scoops" in only_chops

        await _record(world, [Line(1, "Ann", 100), Line(2, "Bob", -100)], seed=2)
        mixed = await _ask(world, "/stats", sender_id=1)
        assert "+$100 · 2 flips · 1 win (50%) · 0 scoops" in mixed
        await world.close()

    asyncio.run(scenario())


def test_scoops_add_up_across_flips() -> None:
    async def scenario() -> None:
        world = bot_harness()
        scoop = Fraction(3)
        for seed in (1, 2):
            await _record(
                world,
                [Line(1, "Ann", 100, points=scoop, scooped=True), Line(2, "Bob", -100)],
                seed=seed,
            )
        await _record(world, [Line(1, "Ann", -50), Line(2, "Bob", 50)], seed=3)

        rows = _rows(await _ask(world, "/leaderboard"))

        assert rows[0].endswith("Ann — +$150 · 3 flips · 2 wins (67%) · 2 scoops")
        assert rows[1].endswith("Bob — -$150 · 3 flips · 1 win (33%) · 0 scoops")
        await world.close()

    asyncio.run(scenario())


def test_the_leaderboard_shows_the_twenty_best_players_and_drops_the_rest() -> None:
    async def scenario() -> None:
        world = bot_harness()
        players = [Line(100 + n, f"P{n:02d}", 10 * n) for n in range(1, 23)]
        for seed, start in enumerate(range(0, 22, 8), start=1):
            await _record(world, players[start : start + 8], seed=seed)

        text = await _ask(world, "/leaderboard")

        assert _ranking(text) == [f"P{n:02d}" for n in range(22, 2, -1)]
        assert "P02" not in text
        assert "P01" not in text
        await world.close()

    asyncio.run(scenario())


def test_the_name_shown_is_the_one_from_the_players_latest_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Old Name", 50), Line(2, "Bob", -50)], seed=1)
        await _record(world, [Line(1, "New Name", 50), Line(2, "Bob", -50)], seed=2)

        text = await _ask(world, "/leaderboard")

        assert _ranking(text) == ["New Name", "Bob"]
        assert "Old Name" not in text
        await world.close()

    asyncio.run(scenario())


def test_groups_never_mix_in_the_ranking_or_in_a_players_figures() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 100), Line(2, "Bob", -100)], seed=1)
        await _record(
            world,
            [Line(1, "Ann", -500), Line(2, "Bob", 400), Line(3, "Eve", 100)],
            seed=2,
            group_id=_OTHER_GROUP,
        )

        board = await _ask(world, "/leaderboard")
        stats = await _ask(world, "/stats", sender_id=1)
        elsewhere = await _ask(world, "/stats @eve")

        assert _ranking(board) == ["Ann", "Bob"]
        assert "Eve" not in board
        assert "+$100 · 1 flip · 1 win (100%)" in stats
        assert "I have no player by that name" not in stats
        assert "no player by that name in this group" in elsewhere
        await world.close()

    asyncio.run(scenario())


def test_a_group_with_no_finished_flips_is_told_so_and_pointed_at_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()

        text = await _ask(world, "/leaderboard")

        assert text == render.NO_FINISHED_FLIPS
        assert "No flips have finished" in text
        assert "/flip" in text
        await world.close()

    asyncio.run(scenario())


def test_a_cancelled_lobby_leaves_nothing_on_the_leaderboard_or_in_stats() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await world.flip_command()
        flip = world.registry.flip_for_group(GROUP)
        assert flip is not None
        await world.feed(
            callback_query=button_press(pack(Press.CANCEL, flip.token), press_id=world.press_id())
        )
        assert world.registry.flip_for_group(GROUP) is None

        board = await _ask(world, "/leaderboard")
        stats = await _ask(world, "/stats")

        assert board == render.NO_FINISHED_FLIPS
        assert stats == render.NO_FLIPS_OF_YOUR_OWN
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------------ stats


def test_stats_with_no_argument_shows_the_askers_own_figures() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(STARTER, "Sam", 200), Line(_PAT, "Pat", -200)], seed=1)
        await _record(world, [Line(STARTER, "Sam", -50), Line(_PAT, "Pat", 50)], seed=2)

        text = await _ask(world, "/stats")

        assert "<b>Sam</b>" in text
        assert "+$150 · 2 flips · 1 win (50%) · 0 scoops" in text
        assert "Pat" not in text
        await world.close()

    asyncio.run(scenario())


def test_stats_from_someone_with_no_history_says_so_and_suggests_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 50), Line(2, "Bob", -50)], seed=1)

        text = await _ask(world, "/stats")

        assert text == render.NO_FLIPS_OF_YOUR_OWN
        assert "not finished a flip" in text
        assert "/flip" in text
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("/stats @sam_ace", id="with-at-sign"),
        pytest.param("/stats sam_ace", id="without-at-sign"),
        pytest.param("/stats @SAM_ACE", id="upper-case"),
        pytest.param("/stats @Sam_Ace", id="same-case-as-stored"),
    ],
)
def test_stats_by_name_finds_the_player_by_stored_username_however_it_is_typed(
    command: str,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(
            world,
            [Line(1, "Ann", 70, username="Sam_Ace"), Line(2, "Bob", -70, username="bob")],
            seed=1,
        )

        text = await _ask(world, command, sender_id=2)

        assert "<b>Ann</b>" in text
        assert "+$70 · 1 flip · 1 win (100%) · 0 scoops" in text
        await world.close()

    asyncio.run(scenario())


def test_stats_by_an_unknown_name_says_there_is_no_such_player_in_this_group() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 50, username="ann"), Line(2, "Bob", -50)], seed=1)

        text = await _ask(world, "/stats @ghost")

        assert "no player by that name in this group" in text
        assert "ghost" in text
        await world.close()

    asyncio.run(scenario())


def test_stats_by_name_returns_whoever_used_the_username_most_recently() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 10, username="shared"), Line(3, "Cy", -10)], seed=1)
        await _record(world, [Line(2, "Bob", 20, username="shared"), Line(3, "Cy", -20)], seed=2)

        after_bob = await _ask(world, "/stats @shared")

        await _record(world, [Line(1, "Ann", 30, username="shared"), Line(3, "Cy", -30)], seed=3)
        after_ann = await _ask(world, "/stats @shared")

        assert "<b>Bob</b>" in after_bob
        assert "+$20 · 1 flip" in after_bob
        assert "<b>Ann</b>" in after_ann
        assert "+$40 · 2 flips" in after_ann
        await world.close()

    asyncio.run(scenario())


def test_stats_as_a_reply_shows_that_persons_figures_even_with_no_username() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(_PAT, "Pat", 80, username=None), Line(1, "Ann", -80)], seed=1)

        await world.feed(message=_reply_to("/stats", sender_id=_PAT))

        text = world.client.sends[-1].text
        assert "<b>Pat</b>" in text
        assert "+$80 · 1 flip · 1 win (100%) · 0 scoops" in text
        await world.close()

    asyncio.run(scenario())


def test_stats_as_a_reply_to_someone_with_no_history_names_them_and_suggests_flip() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 50), Line(2, "Bob", -50)], seed=1)

        await world.feed(message=_reply_to("/stats", sender_id=_PAT, name="Pat"))

        assert world.client.sends[-1].text == render.render_no_stats_for("Pat")
        assert "Pat has not finished a flip" in world.client.sends[-1].text
        assert "/flip" in world.client.sends[-1].text
        await world.close()

    asyncio.run(scenario())


def test_stats_as_a_reply_to_a_message_with_no_sender_gets_the_usage_line() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(STARTER, "Sam", 50), Line(2, "Bob", -50)], seed=1)

        await world.feed(message=_reply_to("/stats", sender_id=None))

        assert world.client.sends[-1].text == render.STATS_USAGE
        await world.close()

    asyncio.run(scenario())


def test_a_name_in_the_command_wins_over_the_message_replied_to() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(
            world,
            [Line(1, "Ann", 60, username="ann"), Line(_PAT, "Pat", -60, username=None)],
            seed=1,
        )

        await world.feed(message=_reply_to("/stats @ann", sender_id=_PAT))

        text = world.client.sends[-1].text
        assert "<b>Ann</b>" in text
        assert "Pat" not in text
        await world.close()

    asyncio.run(scenario())


def test_stats_with_more_than_one_argument_gets_the_usage_line_and_no_lookup() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 50, username="ann"), Line(2, "Bob", -50)], seed=1)
        # A lookup would hit this and answer with the trouble line instead.
        world.store.stats_error = sqlite3.OperationalError("disk I/O error")

        text = await _ask(world, "/stats @ann @bob")

        assert text == render.STATS_USAGE
        await world.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------- output rules


def test_names_are_escaped_so_markup_in_a_display_name_never_becomes_a_tag() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(
            world, [Line(1, "<b>Sam", 50, username="tagged"), Line(2, "R&D", -50)], seed=1
        )

        board = await _ask(world, "/leaderboard")
        stats = await _ask(world, "/stats @tagged")

        for text in (board, stats):
            assert "&lt;b&gt;Sam" in text
            assert "<b>Sam" not in text
        assert "R&amp;D" in board
        await world.close()

    asyncio.run(scenario())


def test_a_very_long_name_is_clipped_to_the_name_limit() -> None:
    async def scenario() -> None:
        world = bot_harness()
        long_name = "L" * 200
        await _record(world, [Line(1, long_name, 50, username="long"), Line(2, "Bob", -50)], seed=1)

        board = await _ask(world, "/leaderboard")
        stats = await _ask(world, "/stats @long")

        clipped = "L" * (render.NAME_LIMIT - 1) + "…"
        assert clipped in board
        assert clipped in stats
        assert "L" * render.NAME_LIMIT not in board
        assert "L" * render.NAME_LIMIT not in stats
        await world.close()

    asyncio.run(scenario())


def test_a_full_leaderboard_of_long_names_stays_inside_the_message_limit() -> None:
    async def scenario() -> None:
        world = bot_harness()
        players = [Line(100 + n, f"{n:02d}" + "x" * 98, 10 * n) for n in range(1, 23)]
        for seed, start in enumerate(range(0, 22, 8), start=1):
            await _record(world, players[start : start + 8], seed=seed)

        text = await _ask(world, "/leaderboard")

        assert len(_rows(text)) == 20
        assert len(text) <= render.MESSAGE_LIMIT
        await world.close()

    asyncio.run(scenario())


def test_a_full_leaderboard_of_names_made_of_escapable_characters_stays_inside_the_limit() -> None:
    async def scenario() -> None:
        world = bot_harness()
        # Every "<" grows to four characters when escaped, so this is the worst case.
        players = [Line(100 + n, f"{n:02d}" + "<" * 98, 10 * n) for n in range(1, 23)]
        for seed, start in enumerate(range(0, 22, 8), start=1):
            await _record(world, players[start : start + 8], seed=seed)

        text = await _ask(world, "/leaderboard")

        assert len(text) <= render.MESSAGE_LIMIT
        assert "<<" not in text
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("/leaderboard", id="leaderboard"),
        pytest.param("/stats", id="stats"),
        pytest.param("/stats @sam", id="stats-by-name"),
    ],
)
def test_both_commands_refuse_in_a_private_chat_without_reading_anything(command: str) -> None:
    async def scenario() -> None:
        world = bot_harness()
        # Reading anything at all would answer with the trouble line instead.
        world.store.stats_error = sqlite3.OperationalError("disk I/O error")

        await world.feed(message=group_message(command, chat_type="private"))

        assert [call.text for call in world.client.sends] == [render.NO_STATS_IN_PRIVATE]
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(sqlite3.OperationalError("disk I/O error"), id="sqlite-error"),
        pytest.param(StorageError("the database is locked"), id="storage-error"),
    ],
)
@pytest.mark.parametrize(
    "command",
    [
        pytest.param("/leaderboard", id="leaderboard"),
        pytest.param("/stats", id="stats"),
        pytest.param("/stats @sam", id="stats-by-name"),
    ],
)
def test_a_failing_database_gets_the_trouble_line_and_an_error_in_the_log(
    command: str, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.stats_error = error

        with caplog.at_level(logging.ERROR):
            await world.feed(message=group_message(command))

        assert [call.text for call in world.client.sends] == [render.STATS_TROUBLE]
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert errors
        assert str(error) in caplog.text
        assert str(GROUP) in caplog.text
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tampering",
    [
        pytest.param("UPDATE flip_players SET scooped = 3", id="impossible-scoops"),
        pytest.param("UPDATE flip_players SET net_dollars = 'lots'", id="damaged-net"),
    ],
)
def test_a_damaged_stats_row_gets_the_trouble_line_and_an_error_in_the_log(
    tampering: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(world, [Line(1, "Ann", 50), Line(2, "Bob", -50)], seed=1)
        world.store.connection.execute("PRAGMA ignore_check_constraints = ON")
        world.store.connection.execute(tampering)

        with caplog.at_level(logging.ERROR):
            await world.feed(message=group_message("/leaderboard"))

        assert [call.text for call in world.client.sends] == [render.STATS_TROUBLE]
        errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert errors
        assert str(GROUP) in caplog.text
        # The damage itself is logged, rather than some other database trouble.
        assert "unreadable" in caplog.text
        await world.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("/leaderboard", id="leaderboard"),
        pytest.param("/stats", id="stats"),
        pytest.param("/stats @ann", id="stats-by-name"),
        pytest.param("/stats @nobody", id="unknown-name"),
    ],
)
def test_the_reply_goes_back_into_the_topic_the_command_was_asked_in(command: str) -> None:
    async def scenario() -> None:
        world = bot_harness()
        await _record(
            world, [Line(STARTER, "Sam", 50, username="ann"), Line(2, "Bob", -50)], seed=1
        )

        await world.feed(message=topic_message(command, topic_id=TOPIC))
        await world.feed(message=group_message(command))

        in_topic, in_general = world.client.sends[-2:]
        assert in_topic.thread_id == TOPIC
        assert in_topic.chat_id == GROUP
        assert in_general.thread_id is None
        assert in_topic.text == in_general.text
        await world.close()

    asyncio.run(scenario())

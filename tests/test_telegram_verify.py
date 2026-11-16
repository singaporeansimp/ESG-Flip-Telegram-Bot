"""What ``/verify`` does with a finished flip, through Telegram and on its own.

The Telegram half feeds real updates through the real dispatcher against a real
in-memory database, so a flip number is one sqlite handed out and a tampered row
is a row somebody really edited. Each kind of damage has one definite failure
part, because the deal is recomputed from the stored seed and the *recomputed*
deal is what gets settled: a changed seed fails the commitment and the deal, a
changed deal fails the deal alone, a changed result fails that result alone.

The second half calls :func:`esgbot.verify.verify_flip` directly on prepared
flips, where going through a chat would only get in the way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest

from esgbot.engine import Flip
from esgbot.game import fairness
from esgbot.game.cards import DECK_SIZE, cards_from_indexes
from esgbot.game.deal import Deal, PlayerHand, deal_cards
from esgbot.game.rules import Settings
from esgbot.game.settlement import settle_deal
from esgbot.storage import FinishedFlip, FlipPlayer, StorageError, StoredFlip
from esgbot.telegram import render
from esgbot.telegram.keyboards import Press, pack
from esgbot.verify import PART_ORDER, Verification, VerifyPart, verify_flip
from tests.engine_helpers import GROUP, SETTINGS, seed_where, use_seed
from tests.storage_helpers import EPOCH
from tests.telegram_helpers import (
    STARTER,
    TOPIC,
    BotHarness,
    bot_harness,
    button_press,
    group_message,
    topic_message,
)

_JOINER = 12
_SEED = bytes(range(32))
_OTHER_GROUP = -1005555
_OTHER_SEED_HEX = "ab" * 32


# ------------------------------------------------------------------ helpers


async def _play_out(world: BotHarness, flip: Flip, *, limit: int = 40) -> None:
    """Advance the engine's clock until the flip is over."""
    for _ in range(limit):
        if flip.closed:
            return
        assert await world.clock.advance_to_next() != 0.0 or flip.closed
    raise AssertionError(f"flip {flip.token} did not finish in {limit} steps")


async def _finished_flip(
    world: BotHarness,
    monkeypatch: pytest.MonkeyPatch,
    seed: bytes = _SEED,
    command: str = "/flip 50 5 2 3",
) -> int:
    """Play a two-player flip on this seed to its end and return its flip number."""
    use_seed(monkeypatch, seed)
    await world.flip_command(command)
    flip = world.registry.flip_for_group(world.group_id)
    assert flip is not None
    await world.feed(
        callback_query=button_press(
            pack(Press.JOIN, flip.token), user_id=_JOINER, press_id="join", name="Pat"
        )
    )
    await world.feed(
        callback_query=button_press(pack(Press.START, flip.token), user_id=STARTER, press_id="go")
    )
    await _play_out(world, flip)
    row = world.store.connection.execute("SELECT MAX(flip_number) FROM flips").fetchone()
    assert row[0] is not None
    return int(row[0])


def _tamper(world: BotHarness, statement: str, parameters: tuple[object, ...] = ()) -> None:
    """Edit the stored flip the way a hand on the database would, checks off."""
    world.store.connection.execute("PRAGMA ignore_check_constraints = ON")
    world.store.connection.execute(statement, parameters)


def _stored_deal(world: BotHarness, number: int) -> dict[str, Any]:
    """The stored deal as plain JSON data, to be edited and written back."""
    row = world.store.connection.execute(
        "SELECT deal FROM flips WHERE flip_number = ?", (number,)
    ).fetchone()
    loaded: dict[str, Any] = json.loads(row[0])
    return loaded


def _store_deal(world: BotHarness, number: int, deal: object) -> None:
    _tamper(
        world,
        "UPDATE flips SET deal = ? WHERE flip_number = ?",
        (json.dumps(deal, separators=(",", ":")), number),
    )


async def _verify(world: BotHarness, argument: str = "") -> str:
    """Send ``/verify`` in the group and return the one reply it got."""
    before = len(world.client.sends)
    await world.feed(message=group_message(f"/verify {argument}".strip()))
    assert len(world.client.sends) == before + 1, world.client.calls
    return world.client.sends[-1].text


def _names_only(text: str, *parts: VerifyPart) -> None:
    """The failure line lists these parts and none of the others."""
    assert "does not verify" in text
    wording = {
        VerifyPart.COMMITMENT: "the commitment",
        VerifyPart.DEAL: "the deal",
        VerifyPart.POINTS: "the points",
        VerifyPart.NETS: "the net results",
        VerifyPart.SCOOP: "the scoop",
    }
    for part, words in wording.items():
        failure_line = next(
            line for line in text.splitlines() if line.startswith("These do not match")
        )
        assert (words in failure_line) == (part in parts), (part, failure_line)


# ----------------------------------------------------- the pass, through Telegram


def test_verify_passes_for_a_flip_recorded_by_a_real_engine_run_and_shows_the_commitment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        text = await _verify(world, str(number))
        assert f"Flip #{number} verifies" in text
        assert fairness.commitment(_SEED) in text
        assert "does not verify" not in text
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------- a flip recorded with defaults


def _flags(world: BotHarness, number: int) -> tuple[int, int]:
    row = world.store.connection.execute(
        "SELECT y_defaulted, z_defaulted FROM flips WHERE flip_number = ?", (number,)
    ).fetchone()
    return int(row[0]), int(row[1])


@pytest.mark.parametrize(
    ("command", "flags"),
    [
        pytest.param("/flip 50 5", (1, 1), id="y-and-z-defaulted"),
        pytest.param("/flip 50 5 2", (0, 1), id="z-defaulted"),
    ],
)
def test_verify_passes_for_a_flip_played_with_defaults(
    monkeypatch: pytest.MonkeyPatch, command: str, flags: tuple[int, int]
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch, command=command)
        assert _flags(world, number) == flags
        text = await _verify(world, str(number))
        assert f"Flip #{number} verifies" in text
        assert fairness.commitment(_SEED) in text
        assert "does not verify" not in text
        await world.close()

    asyncio.run(scenario())


def test_verify_does_not_care_whether_the_defaults_were_typed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch, command="/flip 50 5")
        assert _flags(world, number) == (1, 1)
        # Y is 21 and Z is the two players, so "typed in full" is a consistent row.
        _tamper(world, "UPDATE flips SET y_defaulted = 0, z_defaulted = 0")
        assert _flags(world, number) == (0, 0)
        text = await _verify(world, str(number))
        assert f"Flip #{number} verifies" in text
        assert "does not verify" not in text
        await world.close()

    asyncio.run(scenario())


def test_verify_still_fails_a_tampered_flip_that_was_played_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch, command="/flip 50 5")
        _tamper(
            world,
            "UPDATE flip_players SET net_dollars = net_dollars + 7 "
            "WHERE flip_number = ? AND seat = 0",
            (number,),
        )
        _names_only(await _verify(world, str(number)), VerifyPart.NETS)
        _tamper(world, "UPDATE flips SET seed = ?", (_OTHER_SEED_HEX,))
        text = await _verify(world, str(number))
        failure_line = next(line for line in text.splitlines() if "do not match" in line)
        assert "the commitment" in failure_line
        assert "the deal" in failure_line
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------ tampering


def test_verify_fails_naming_commitment_and_deal_when_the_stored_seed_is_altered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        _tamper(world, "UPDATE flips SET seed = ?", (_OTHER_SEED_HEX,))
        text = await _verify(world, str(number))
        failure_line = next(line for line in text.splitlines() if "do not match" in line)
        assert "the commitment" in failure_line
        assert "the deal" in failure_line
        assert fairness.commitment(_SEED) in text
        assert fairness.commitment(bytes.fromhex(_OTHER_SEED_HEX)) in text
        await world.close()

    asyncio.run(scenario())


def test_verify_fails_naming_only_the_deal_when_a_stored_card_is_swapped_for_an_undealt_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        stored = _stored_deal(world, number)
        used = set(stored["board1"]) | set(stored["board2"])
        for hand in stored["hands"]:
            used |= set(hand["hole"]) | set(hand["extra"])
        undealt = next(index for index in range(DECK_SIZE) if index not in used)
        stored["board1"][0] = undealt
        _store_deal(world, number, stored)

        text = await _verify(world, str(number))
        _names_only(text, VerifyPart.DEAL)
        await world.close()

    asyncio.run(scenario())


def test_verify_fails_naming_only_the_net_results_when_a_stored_net_is_altered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        _tamper(
            world,
            "UPDATE flip_players SET net_dollars = net_dollars + 7 "
            "WHERE flip_number = ? AND seat = 0",
            (number,),
        )
        text = await _verify(world, str(number))
        _names_only(text, VerifyPart.NETS)
        await world.close()

    asyncio.run(scenario())


def test_verify_fails_naming_only_the_points_when_a_stored_points_value_is_altered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        _tamper(
            world,
            "UPDATE flip_players SET points = '17' WHERE flip_number = ? AND seat = 0",
            (number,),
        )
        text = await _verify(world, str(number))
        _names_only(text, VerifyPart.POINTS)
        await world.close()

    asyncio.run(scenario())


def test_verify_fails_naming_only_the_scoop_when_a_stored_scoop_flag_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        seed = seed_where(lambda outcome: outcome.scoop_seat is not None, player_ids=[11, 12])
        number = await _finished_flip(world, monkeypatch, seed)
        assert (await _verify(world, str(number))).count("verifies") == 1, (
            "an untampered scoop flip must verify first"
        )
        cleared = world.store.connection.execute(
            "UPDATE flip_players SET scooped = 0 WHERE flip_number = ? AND scooped = 1", (number,)
        )
        assert cleared.rowcount == 1
        text = await _verify(world, str(number))
        _names_only(text, VerifyPart.SCOOP)
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------- a deal that will not read back


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param("truncate", id="truncated-json"),
        pytest.param("duplicate-card", id="the-same-card-twice"),
    ],
)
def test_verify_answers_that_an_unreadable_stored_deal_does_not_verify_instead_of_crashing(
    damage: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        if damage == "truncate":
            row = world.store.connection.execute(
                "SELECT deal FROM flips WHERE flip_number = ?", (number,)
            ).fetchone()
            _tamper(
                world,
                "UPDATE flips SET deal = ? WHERE flip_number = ?",
                (str(row[0])[:25], number),
            )
        else:
            stored = _stored_deal(world, number)
            stored["board1"][0] = stored["board2"][0]
            _store_deal(world, number, stored)

        with caplog.at_level(logging.WARNING):
            text = await _verify(world, str(number))  # a raise here fails the test

        assert text == render.render_unreadable_flip(number)
        assert "does not verify" in text
        assert "cannot be read back" in text
        assert f"flip {number}" in caplog.text
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------ whose flip it is


def test_verify_refuses_a_flip_number_that_belongs_to_another_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        elsewhere = await _finished_flip(world, monkeypatch)
        world.store.connection.execute("UPDATE flips SET group_id = ?", (_OTHER_GROUP,))
        assert world.group_id == GROUP
        assert await _verify(world, str(elsewhere)) == render.render_no_such_flip(elsewhere)
        await world.close()

    asyncio.run(scenario())


def test_verify_still_answers_for_the_group_that_owns_the_flip_when_another_group_asks_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        await world.feed(message=group_message(f"/verify {number}", chat_id=_OTHER_GROUP))
        assert world.client.sends[-1].text == render.render_no_such_flip(number)
        assert world.client.sends[-1].chat_id == _OTHER_GROUP
        assert f"Flip #{number} verifies" in await _verify(world, str(number))
        await world.close()

    asyncio.run(scenario())


def test_verify_says_there_is_no_such_flip_for_a_number_that_exists_nowhere() -> None:
    async def scenario() -> None:
        world = bot_harness()
        assert await _verify(world, "123") == render.render_no_such_flip(123)
        await world.close()

    asyncio.run(scenario())


def test_verify_accepts_a_flip_number_of_the_longest_allowed_length() -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = int("9" * 18)
        assert await _verify(world, str(number)) == render.render_no_such_flip(number)
        await world.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------- bad commands


@pytest.mark.parametrize(
    "argument",
    [
        pytest.param("", id="missing"),
        pytest.param("x", id="not-a-number"),
        pytest.param("4.0", id="decimal"),
        pytest.param("-1", id="negative"),
        pytest.param("0", id="zero"),
        pytest.param("1 2", id="two-arguments"),
        pytest.param("1" * 19, id="nineteen-digits"),
    ],
)
def test_verify_answers_with_usage_and_reads_nothing_for_an_unusable_argument(
    argument: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        # Any read at all would turn this reply into STATS_TROUBLE and an error log.
        world.store.fetch_error = StorageError("the database should not have been asked")
        with caplog.at_level(logging.ERROR):
            text = await _verify(world, argument)
        assert text == render.VERIFY_USAGE
        assert caplog.text == ""
        await world.close()

    asyncio.run(scenario())


def test_verify_in_a_private_chat_refuses_with_the_group_only_notice() -> None:
    async def scenario() -> None:
        world = bot_harness()
        await world.feed(message=group_message("/verify 1", chat_id=42, chat_type="private"))
        assert world.client.sends[-1].text == render.NO_STATS_IN_PRIVATE
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------ a failing database


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(sqlite3.OperationalError("disk I/O error"), id="sqlite-error"),
        pytest.param(StorageError("the database is locked"), id="storage-error"),
    ],
)
def test_verify_replies_with_the_trouble_notice_and_logs_an_error_when_the_database_fails(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        world.store.fetch_error = error
        with caplog.at_level(logging.ERROR):
            text = await _verify(world, "1")
        assert text == render.STATS_TROUBLE
        assert str(error) in caplog.text
        assert any(record.levelno == logging.ERROR for record in caplog.records)
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------- the topic


def test_verify_replies_inside_the_forum_topic_it_was_asked_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        await world.feed(message=topic_message(f"/verify {number}", topic_id=TOPIC))
        reply = world.client.sends[-1]
        assert f"Flip #{number} verifies" in reply.text
        assert reply.thread_id == TOPIC
        await world.feed(message=topic_message("/verify 999", topic_id=TOPIC))
        assert world.client.sends[-1].thread_id == TOPIC
        await world.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------- the log


def test_a_failed_verification_is_logged_at_warning_naming_the_flip_and_the_parts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        _tamper(
            world,
            "UPDATE flip_players SET net_dollars = net_dollars + 1 "
            "WHERE flip_number = ? AND seat = 0",
            (number,),
        )
        with caplog.at_level(logging.WARNING):
            await _verify(world, str(number))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert f"flip {number}" in message
        assert str(GROUP) in message
        assert VerifyPart.NETS in message
        await world.close()

    asyncio.run(scenario())


def test_a_passing_verification_logs_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        world = bot_harness()
        number = await _finished_flip(world, monkeypatch)
        with caplog.at_level(logging.WARNING):
            await _verify(world, str(number))
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        await world.close()

    asyncio.run(scenario())


# ------------------------------------------------- verify_flip on prepared flips


_PLAYERS = (11, 12, 13)
_NO_SCOOP_SEED = seed_where(lambda outcome: outcome.scoop_seat is None, player_ids=_PLAYERS)


def _correct_flip(seed: bytes = _NO_SCOOP_SEED, settings: Settings = SETTINGS) -> FinishedFlip:
    """A finished flip whose stored players are exactly what settlement produces."""
    deal = deal_cards(seed=seed, player_ids=_PLAYERS, x=settings.x, y=settings.y)
    outcome = settle_deal(deal, settings)
    players = tuple(
        FlipPlayer(
            user_id=hand.player_id,
            display_name=f"Player {hand.player_id}",
            username=None,
            seat=seat,
            points=result.points,
            net_dollars=result.net_dollars,
            scooped=result.scooped,
        )
        for seat, (hand, result) in enumerate(zip(deal.hands, outcome.seats, strict=True))
    )
    return FinishedFlip(
        group_id=GROUP,
        settings=settings,
        seed=seed,
        commitment=fairness.commitment(seed),
        deal=deal,
        players=players,
        started_at=EPOCH,
        finished_at=EPOCH,
    )


def _with_players(flip: FinishedFlip, players: tuple[FlipPlayer, ...]) -> StoredFlip:
    return StoredFlip(
        flip_number=3,
        flip=FinishedFlip(
            group_id=flip.group_id,
            settings=flip.settings,
            seed=flip.seed,
            commitment=flip.commitment,
            deal=flip.deal,
            players=players,
            started_at=flip.started_at,
            finished_at=flip.finished_at,
        ),
    )


def test_verify_flip_passes_a_flip_whose_stored_players_are_exactly_what_settlement_produces() -> (
    None
):
    flip = _correct_flip()
    report = verify_flip(StoredFlip(flip_number=3, flip=flip))
    assert report.passed
    assert report.differences == ()
    assert report.flip_number == 3
    assert report.commitment == report.recomputed_commitment == fairness.commitment(flip.seed)


def test_verify_flip_reports_a_scoop_flip_that_is_stored_correctly_as_passing() -> None:
    seed = seed_where(lambda outcome: outcome.scoop_seat is not None, player_ids=_PLAYERS)
    flip = _correct_flip(seed)
    assert any(player.scooped for player in flip.players)
    assert verify_flip(StoredFlip(flip_number=1, flip=flip)).passed


def test_verify_flip_lists_differences_in_part_order_with_no_repeats_across_many_wrong_seats() -> (
    None
):
    flip = _correct_flip()
    first, second, third = flip.players
    players = (
        replace(first, scooped=True, net_dollars=first.net_dollars + 1),
        replace(second, points=second.points + 5),
        replace(third, net_dollars=third.net_dollars + 3, points=third.points + 5),
    )

    report = verify_flip(_with_players(flip, players))

    assert report.differences == (VerifyPart.POINTS, VerifyPart.NETS, VerifyPart.SCOOP)
    assert list(report.differences) == [part for part in PART_ORDER if report.differs(part)]
    assert len(set(report.differences)) == len(report.differences)


def test_verify_flip_reports_each_part_once_when_every_seat_is_wrong_in_the_same_way() -> None:
    flip = _correct_flip()
    players = tuple(
        FlipPlayer(
            user_id=player.user_id,
            display_name=player.display_name,
            username=player.username,
            seat=player.seat,
            points=player.points + Fraction(1, 2),
            net_dollars=player.net_dollars,
            scooped=player.scooped,
        )
        for player in flip.players
    )
    assert verify_flip(_with_players(flip, players)).differences == (VerifyPart.POINTS,)


def test_verify_flip_reports_a_commitment_that_is_not_the_hash_of_the_seed() -> None:
    flip = _correct_flip()
    wrong = fairness.commitment(_SEED)
    stored = StoredFlip(
        flip_number=2,
        flip=FinishedFlip(
            group_id=flip.group_id,
            settings=flip.settings,
            seed=flip.seed,
            commitment=wrong,
            deal=flip.deal,
            players=flip.players,
            started_at=flip.started_at,
            finished_at=flip.finished_at,
        ),
    )
    report = verify_flip(stored)
    assert report.differences == (VerifyPart.COMMITMENT,)
    assert report.commitment == wrong
    assert report.recomputed_commitment == fairness.commitment(flip.seed)


def test_verify_flip_reports_exactly_the_deal_for_a_flip_that_cannot_be_dealt() -> None:
    settings = Settings(wager=50, x=5, y=0, z=1)
    # A player list no deal can be made from: two seats recording the same
    # player. The stored deal holds it and the settings are legal, so nothing
    # short of re-dealing it notices -- which is what this is about.
    with pytest.raises(ValueError, match="unique"):
        deal_cards(seed=_SEED, player_ids=[11, 11], x=settings.x, y=settings.y)

    cards = iter(cards_from_indexes(list(range(20))))
    hands = tuple(
        PlayerHand(
            seat=seat,
            player_id=11,
            hole=tuple(next(cards) for _ in range(5)),
            extra=(),
        )
        for seat in range(2)
    )
    deal = Deal(
        x=5,
        y=0,
        button_seat=0,
        hands=hands,
        board1=tuple(next(cards) for _ in range(5)),
        board2=tuple(next(cards) for _ in range(5)),
    )
    flip = FinishedFlip(
        group_id=GROUP,
        settings=settings,
        seed=_SEED,
        commitment=fairness.commitment(_SEED),
        deal=deal,
        players=tuple(
            FlipPlayer(
                user_id=hand.player_id,
                display_name=f"Player {hand.player_id}",
                username=None,
                seat=hand.seat,
                points=Fraction(1),
                net_dollars=0,
                scooped=False,
            )
            for hand in hands
        ),
        started_at=EPOCH,
        finished_at=EPOCH,
    )

    report = verify_flip(StoredFlip(flip_number=1, flip=flip))

    assert report.differences == (VerifyPart.DEAL,)


def test_verification_refuses_a_flip_number_below_one() -> None:
    with pytest.raises(ValueError, match="flip number"):
        Verification(
            flip_number=0, commitment="a" * 64, recomputed_commitment="a" * 64, differences=()
        )


@pytest.mark.parametrize(
    "differences",
    [
        pytest.param((VerifyPart.DEAL, VerifyPart.DEAL), id="repeated"),
        pytest.param((VerifyPart.NETS, VerifyPart.DEAL), id="out-of-order"),
        pytest.param((VerifyPart.SCOOP, VerifyPart.COMMITMENT), id="reversed-ends"),
    ],
)
def test_verification_refuses_differences_that_repeat_or_are_out_of_order(
    differences: tuple[VerifyPart, ...],
) -> None:
    with pytest.raises(ValueError, match="distinct and in"):
        Verification(
            flip_number=1,
            commitment="a" * 64,
            recomputed_commitment="a" * 64,
            differences=differences,
        )


def test_verification_accepts_every_part_in_part_order() -> None:
    report = Verification(
        flip_number=1,
        commitment="a" * 64,
        recomputed_commitment="b" * 64,
        differences=PART_ORDER,
    )
    assert not report.passed
    assert all(report.differs(part) for part in VerifyPart)

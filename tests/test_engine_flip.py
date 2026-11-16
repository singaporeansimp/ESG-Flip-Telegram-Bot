"""A flip from lobby to result, and the interfaces the engine is wired to.

The timing rules are in ``test_engine_timing.py``, the unhappy paths in
``test_engine_lifecycle.py``, the races in ``test_engine_concurrency.py`` and
what a view may carry in ``test_engine_views.py``.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from esgbot.engine import (
    Clock,
    CommitmentView,
    FlipPhase,
    FlipStore,
    Gateway,
    LobbyStatus,
    LobbyView,
    Notice,
    PeekAnswer,
    ResultView,
    ShowdownStep,
    ShowdownView,
    SystemClock,
    TableStage,
    TableView,
    VoidReason,
    VoidView,
    settle_in_thread,
)
from esgbot.engine import flip as flip_module
from esgbot.game import fairness
from esgbot.game.deal import deal_cards
from esgbot.game.rules import Settings
from esgbot.game.settlement import FlipOutcome, settle_deal
from esgbot.storage import ActiveFlip, Database, FlipState
from tests.engine_helpers import FakeClock, FakeGateway, FakeStore, harness, player


def test_lobby_posts_one_message_with_the_starter_in_it() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.registry.create_flip(
                group_id=world.group_id, starter=player(11), settings=world.settings
            )
            assert flip.phase is FlipPhase.LOBBY
            assert flip.players == (player(11),)
            assert world.registry.flip_for_group(world.group_id) is flip
            assert world.registry.flip_for_token(flip.token) is flip

            (posted,) = world.gateway.posts
            assert posted.message_id == flip.message_id
            view = posted.view
            assert isinstance(view, LobbyView)
            assert view.status is LobbyStatus.OPEN
            assert view.starter == player(11)
            assert view.players == (player(11),)

            row = world.store.active[flip.token]
            assert row.group_id == world.group_id
            assert row.message_id == flip.message_id
            assert row.state is FlipState.LOBBY
            assert row.seed is None

    asyncio.run(scenario())


def test_start_puts_the_seed_and_the_running_state_on_the_flips_row() -> None:
    """The row is what a restart reads, so the seed it reveals has to be there.

    It is also the seed the result reveals and the one the flip was recorded
    with, so all three are checked against each other.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            assert world.store.active[flip.token].seed is None

            await world.start(flip)
            row = world.store.active[flip.token]
            assert row.state is FlipState.RUNNING
            assert row.seed is not None
            assert len(row.seed) == fairness.SEED_SIZE
            commitment = next(
                view for view in world.gateway.views if isinstance(view, CommitmentView)
            )
            assert commitment.commitment == fairness.commitment(row.seed)

            await world.finish(flip)
            result = world.gateway.posts[-1].view
            assert isinstance(result, ResultView)
            assert result.seed == row.seed
            assert world.store.recorded[0].seed == row.seed

    asyncio.run(scenario())


def test_full_flip_runs_from_lobby_to_a_recorded_result() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            assert flip.player_count == 3

            assert await flip.start(flip.starter.user_id, press_id=world.press()) is Notice.STARTED
            await world.finish(flip)

            assert flip.phase is FlipPhase.FINISHED
            assert flip.flip_number == 42
            assert flip.token not in world.store.active
            assert world.registry.flip_for_token(flip.token) is None

            assert [type(view).__name__ for view in world.gateway.views] == [
                "LobbyView",  # posted when the flip was created
                "LobbyView",  # edited as each player joined
                "LobbyView",
                "CommitmentView",  # its own message, never edited
                "TableView",  # the lobby message becomes the table
                "TableView",  # Board 1
                "TableView",  # Board 2
                "TableView",  # the extra cards, and the peek window opens
                "ShowdownView",  # every hand
                "ShowdownView",  # Board 1's winner
                "ShowdownView",  # Board 2's winner
                "ShowdownView",  # the hand-strength board's winner
                "ShowdownView",  # the points
                "ResultView",  # its own message, at the bottom of the chat
            ]

            commitment = next(
                view for view in world.gateway.views if isinstance(view, CommitmentView)
            )
            result = world.gateway.posts[-1].view
            assert isinstance(result, ResultView)
            assert result.flip_number == 42
            assert result.recorded
            assert commitment.commitment == fairness.commitment(result.seed)

            # The table is one message, edited; the commitment and result are their own.
            assert {edit.message_id for edit in world.gateway.edits} == {flip.message_id}
            assert len({post.message_id for post in world.gateway.posts}) == 3

            (recorded,) = world.store.recorded
            assert recorded.group_id == world.group_id
            assert recorded.settings == world.settings
            assert recorded.commitment == commitment.commitment
            assert recorded.seed == result.seed
            assert recorded.deal == deal_cards(
                seed=result.seed,
                player_ids=[seated.user_id for seated in flip.players],
                x=world.settings.x,
                y=world.settings.y,
            )
            assert [line.user_id for line in recorded.players] == [11, 12, 13]
            assert sum(line.net_dollars for line in recorded.players) == 0
            assert recorded.finished_at > recorded.started_at

            assert [
                view.step for view in world.gateway.views if isinstance(view, ShowdownView)
            ] == list(ShowdownStep)
            final = world.gateway.edits[-1].view
            assert isinstance(final, ShowdownView)
            assert final.step is ShowdownStep.POINTS
            assert {hand.player.user_id: hand.points for hand in final.hands} == {
                line.user_id: line.points for line in recorded.players
            }
            assert {hand.player.user_id: hand.net_dollars for hand in result.hands} == {
                line.user_id: line.net_dollars for line in recorded.players
            }

    asyncio.run(scenario())


def test_peek_shows_only_that_players_cards_and_grows_with_the_extra_cards() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            early = await flip.peek(11, press_id=world.press())
            assert isinstance(early, PeekAnswer)
            assert len(early.hole) == world.settings.x
            assert early.extra == ()
            assert await flip.peek(99, press_id=world.press()) is Notice.NOT_A_PLAYER

            await world.clock.advance(2.0)  # Board 2, then the extra cards
            table = world.gateway.edits[-1].view
            assert isinstance(table, TableView)
            assert table.stage is TableStage.PEEKING
            assert table.extra_count == world.settings.y

            late = await flip.peek(11, press_id=world.press())
            other = await flip.peek(12, press_id=world.press())
            assert isinstance(late, PeekAnswer)
            assert isinstance(other, PeekAnswer)
            assert late.hole == early.hole
            assert len(late.extra) == table.extra_count
            assert set(late.cards).isdisjoint(other.cards)

            await world.finish(flip)
            assert await flip.peek(11, press_id=world.press()) is Notice.EXPIRED

    asyncio.run(scenario())


def test_the_default_settler_runs_the_showdown_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran_on: list[int] = []

    def spy(deal: object, settings: object) -> FlipOutcome:
        ran_on.append(threading.get_ident())
        assert isinstance(deal, type(fixture_deal))
        assert isinstance(settings, Settings)
        return settle_deal(fixture_deal, fixture_settings)

    fixture_settings = Settings(wager=10, x=5, y=0, z=1)
    fixture_deal = deal_cards(
        seed=fairness.generate_seed(), player_ids=[1, 2], x=fixture_settings.x, y=fixture_settings.y
    )
    monkeypatch.setattr(flip_module, "settle_deal", spy)

    async def scenario() -> None:
        outcome = await settle_in_thread(fixture_deal, fixture_settings)
        assert outcome.player_count == 2

    asyncio.run(scenario())
    assert len(ran_on) == 1
    assert ran_on[0] != threading.get_ident()


def test_a_void_notice_can_be_built_from_a_leftover_row_alone() -> None:
    """The sweep after a restart has only the row, so the notice must need only that.

    It knows the flip's token, its message and its seed, and nothing about the
    settings or who was playing, so those are what a notice may leave out.
    """
    seed = fairness.generate_seed()
    row = ActiveFlip(
        token="Ab3dEf7h",
        group_id=-1001,
        topic_id=None,
        message_id=500,
        state=FlipState.RUNNING,
        seed=seed,
        created_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
    )
    notice = VoidView(token=row.token, reason=VoidReason.RESTART, seed=row.seed)
    assert notice.started
    assert notice.seed == seed
    assert notice.settings is None
    assert notice.players == ()
    assert not VoidView(token=row.token, reason=VoidReason.RESTART).started


def test_the_real_parts_and_the_fakes_both_fit_the_engine_interfaces() -> None:
    clock = FakeClock()
    assert isinstance(clock, Clock)
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeGateway(clock), Gateway)
    assert isinstance(FakeStore(), FlipStore)
    # Database is checked by the type checker below; building one needs a loop.
    assert issubclass(Database, object)


def _database_is_a_flip_store(database: Database) -> FlipStore:
    """A type-checker-only claim: the real database fits what the engine needs."""
    return database

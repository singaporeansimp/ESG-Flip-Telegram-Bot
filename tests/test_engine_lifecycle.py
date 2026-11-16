"""The lobby timers, the watchdog, the player cap and every way a flip can fail.

A full flip is in ``test_engine_flip.py`` and the pacing of a healthy round in
``test_engine_timing.py``. Here the clock is only ever advanced to the second a
deadline falls on, so each rule is checked to the second.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Sequence
from dataclasses import replace

import pytest

from esgbot.engine import (
    CommitmentView,
    Flip,
    FlipAlreadyOpenError,
    FlipPhase,
    LobbyStatus,
    LobbyView,
    MessageTarget,
    Notice,
    PermanentFailureError,
    ResultView,
    ShowdownView,
    TableView,
    VoidReason,
    VoidView,
    timing,
)
from esgbot.engine.flip import _FlipOverError
from esgbot.game import fairness
from esgbot.game.deal import Deal, extra_rounds_for
from esgbot.game.rules import FlipRequest, Settings
from esgbot.game.settlement import FlipOutcome
from esgbot.storage import FinishedFlip, StorageError
from tests.engine_helpers import (
    Edited,
    Event,
    FakeClock,
    FakeGateway,
    FakeStore,
    Harness,
    HoldsAnAnswer,
    HoldsAView,
    Posted,
    harness,
    harness_over,
    outcome_for,
    player,
    players,
    seed_where,
    settle_inline,
    use_seed,
)

_OTHER_GROUP = -2002
_MIGRATED_GROUP = -1003003
_TOPIC = 7


def _phase(flip: Flip) -> FlipPhase:
    """The phase read afresh, so the type checker does not assume it cannot change."""
    return flip.phase


def _messages(events: Sequence[Event]) -> list[Posted | Edited]:
    """The posts and edits among some of the gateway's events, in order."""
    return [event for event in events if isinstance(event, (Posted, Edited))]


class _HoldsTheRecord(FakeStore):
    """A store whose record of the flip waits, as a worker thread inside sqlite does.

    The point is that the write cannot be called back once it has begun: the
    engine runs it where a cancelled round cannot stop it, and the real
    :class:`~esgbot.storage.Database` holds its connection until the thread is
    out of it, so this stands in for a write that is going to commit whatever
    the engine does next.
    """

    def __init__(self, *, fails: bool = False) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.record_gate = asyncio.Event()
        if fails:
            self.record_error = sqlite3.OperationalError("disk full at commit")

    async def record_flip(self, flip: FinishedFlip, *, token: str | None = None) -> int:
        self.entered.set()
        await self.record_gate.wait()
        return await super().record_flip(flip, token=token)


def _assert_voided_unpublished(world: Harness, flip: Flip, reason: VoidReason) -> None:
    """The flip was voided before its round published anything, and said so as a lobby."""
    assert _phase(flip) is FlipPhase.VOIDED
    assert world.gateway.views_of(CommitmentView) == ()
    assert world.gateway.views_of(TableView) == ()
    last = world.gateway.edits[-1]
    assert isinstance(last.view, LobbyView)
    assert last.view.status is LobbyStatus.VOIDED
    void = world.gateway.posts[-1].view
    assert isinstance(void, VoidView)
    assert void.reason is reason
    assert void.started
    assert void.seed is not None
    assert len(void.seed) == fairness.SEED_SIZE
    assert void.commitment == fairness.commitment(void.seed)
    assert world.store.recorded == []
    assert flip.token not in world.store.active
    assert world.registry.flips == ()


def test_the_timeouts_are_ten_and_thirty_minutes() -> None:
    assert timing.LOBBY_IDLE_SECONDS == 10 * 60
    assert timing.LOBBY_MAX_AGE_SECONDS == 30 * 60
    assert timing.FLIP_WATCHDOG_SECONDS == 30 * 60


def test_an_idle_lobby_cancels_itself_after_ten_minutes() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            opened = world.clock.monotonic()

            await world.clock.advance(timing.LOBBY_IDLE_SECONDS - 1)
            assert _phase(flip) is FlipPhase.LOBBY
            await world.clock.advance(1)

            assert _phase(flip) is FlipPhase.CANCELLED
            assert flip.closed
            assert world.registry.flip_for_group(world.group_id) is None
            assert flip.token not in world.store.active
            assert world.store.recorded == []

            last = world.gateway.edits[-1]
            assert isinstance(last.view, LobbyView)
            assert last.view.status is LobbyStatus.TIMED_OUT
            assert last.message_id == flip.message_id
            assert last.at == opened + timing.LOBBY_IDLE_SECONDS

            assert await flip.join(player(30), press_id="late") is Notice.EXPIRED
            assert world.gateway.answers[-1].answer is Notice.EXPIRED

    asyncio.run(scenario())


def test_a_join_pushes_the_idle_deadline_out() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            await world.clock.advance(timing.LOBBY_IDLE_SECONDS - 60)
            assert await flip.join(player(12), press_id=world.press()) is Notice.JOINED

            await world.clock.advance(timing.LOBBY_IDLE_SECONDS - 1)
            assert _phase(flip) is FlipPhase.LOBBY
            await world.clock.advance(1)
            assert _phase(flip) is FlipPhase.CANCELLED

    asyncio.run(scenario())


def test_a_leave_pushes_the_idle_deadline_out() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.clock.advance(timing.LOBBY_IDLE_SECONDS - 60)
            assert await flip.leave(12, press_id=world.press()) is Notice.LEFT

            await world.clock.advance(timing.LOBBY_IDLE_SECONDS - 1)
            assert _phase(flip) is FlipPhase.LOBBY
            await world.clock.advance(1)
            assert _phase(flip) is FlipPhase.CANCELLED

    asyncio.run(scenario())


def test_a_lobby_busy_every_nine_minutes_survives_the_idle_timeout() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            opened = world.clock.monotonic()
            for user_id in (12, 13):
                await world.clock.advance(540)
                assert await flip.join(player(user_id), press_id=world.press()) is Notice.JOINED

            # Past the idle limit counted from the start, so only the joins kept it alive.
            assert world.clock.monotonic() - opened == 1080 > timing.LOBBY_IDLE_SECONDS
            assert _phase(flip) is FlipPhase.LOBBY
            assert flip.player_count == 3
            assert not any(
                isinstance(view, LobbyView) and view.status is LobbyStatus.TIMED_OUT
                for view in world.gateway.views
            )

    asyncio.run(scenario())


def test_a_busy_lobby_still_times_out_at_thirty_minutes() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            opened = world.clock.monotonic()
            # Alternating join and leave every 500 seconds keeps the idle clock fresh.
            for turn in range(3):
                await world.clock.advance(500)
                if turn % 2 == 0:
                    assert await flip.join(player(12), press_id=world.press()) is Notice.JOINED
                else:
                    assert await flip.leave(12, press_id=world.press()) is Notice.LEFT

            await world.clock.advance(timing.LOBBY_MAX_AGE_SECONDS - 1500 - 1)
            assert _phase(flip) is FlipPhase.LOBBY
            await world.clock.advance(1)

            assert _phase(flip) is FlipPhase.CANCELLED
            assert flip.token not in world.store.active
            assert world.store.recorded == []
            last = world.gateway.edits[-1]
            assert isinstance(last.view, LobbyView)
            assert last.view.status is LobbyStatus.TIMED_OUT
            assert last.at == opened + timing.LOBBY_MAX_AGE_SECONDS

    asyncio.run(scenario())


def test_the_starter_cancelling_closes_the_lobby_and_records_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)

            assert await flip.cancel(11, press_id=world.press()) is Notice.CANCELLED

            assert _phase(flip) is FlipPhase.CANCELLED
            assert flip.closed
            assert world.registry.flip_for_group(world.group_id) is None
            assert world.registry.flip_for_token(flip.token) is None
            assert flip.token not in world.store.active
            assert world.store.recorded == []
            last = world.gateway.edits[-1]
            assert isinstance(last.view, LobbyView)
            assert last.view.status is LobbyStatus.CANCELLED
            assert world.clock.sleeping == 0

    asyncio.run(scenario())


def test_the_watchdog_voids_a_round_stuck_for_half_an_hour() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.gateway.hold()
            await world.start(flip)
            assert _phase(flip) is FlipPhase.DEALING
            assert world.gateway.waiting == 1

            await world.clock.advance(timing.FLIP_WATCHDOG_SECONDS - 1)
            assert _phase(flip) is FlipPhase.DEALING
            assert world.gateway.waiting == 1
            assert flip.token in world.store.active
            await world.clock.advance(1)
            assert _phase(flip) is FlipPhase.VOIDED
            world.gateway.release()
            await flip.wait_closed()

            assert flip.token not in world.store.active
            assert world.store.recorded == []
            assert world.registry.flip_for_group(world.group_id) is None

            table = world.gateway.edits[-1].view
            assert isinstance(table, TableView)
            assert table.voided

            void = world.gateway.posts[-1].view
            assert isinstance(void, VoidView)
            assert void.reason is VoidReason.TIMED_OUT
            assert void.started
            assert void.seed is not None
            assert len(void.seed) == fairness.SEED_SIZE
            assert void.commitment == fairness.commitment(void.seed)
            assert [seated.user_id for seated in void.players] == [11, 12, 13]

    asyncio.run(scenario())


def test_the_ninth_player_is_refused_when_eight_fit() -> None:
    async def scenario() -> None:
        world = harness()
        assert world.settings.max_players == 8
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            for joiner in players(7, first_id=12):
                assert await flip.join(joiner, press_id=world.press()) is Notice.JOINED
            assert flip.player_count == 8
            edits = len(world.gateway.edits)

            assert await flip.join(player(30), press_id=world.press()) is Notice.LOBBY_FULL

            assert flip.player_count == 8
            assert player(30) not in flip.players
            assert len(world.gateway.edits) == edits
            assert world.gateway.answers[-1].answer is Notice.LOBBY_FULL

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError("disk I/O error"), StorageError("broken")]
)
def test_a_storage_failure_at_the_end_still_posts_the_result(error: Exception) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.store.record_error = error
            await world.start(flip)
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            assert flip.flip_number is None
            assert world.store.recorded == []
            assert flip.token not in world.store.active

            result = world.gateway.posts[-1].view
            assert isinstance(result, ResultView)
            assert result.flip_number is None
            assert not result.recorded

    asyncio.run(scenario())


def test_a_permanent_gateway_failure_voids_the_flip_without_a_word() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.gateway.fail_edits = True
            await world.start(flip)
            await flip.wait_closed()

            assert _phase(flip) is FlipPhase.VOIDED
            assert flip.token not in world.store.active
            assert world.store.recorded == []
            assert world.registry.flip_for_group(world.group_id) is None
            assert world.gateway.views_of(VoidView) == ()
            # The commitment goes out before the first table edit, which is what fails.
            assert [type(post.view) for post in world.gateway.posts] == [
                LobbyView,
                CommitmentView,
            ]

    asyncio.run(scenario())


def test_a_gateway_that_cannot_post_voids_the_flip_without_a_notice() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.gateway.fail_posts = True
            await world.start(flip)
            await flip.wait_closed()

            assert _phase(flip) is FlipPhase.VOIDED
            assert flip.token not in world.store.active
            assert world.store.recorded == []
            assert world.registry.flip_for_group(world.group_id) is None
            assert world.gateway.views_of(VoidView) == ()
            assert world.gateway.views_of(TableView) == ()
            assert [type(post.view) for post in world.gateway.posts] == [LobbyView]

    asyncio.run(scenario())


def test_a_seed_that_cannot_be_saved_voids_the_round_before_anything_is_published() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.store.update_error = sqlite3.OperationalError("disk I/O error")
            await world.start(flip)
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            _assert_voided_unpublished(world, flip, VoidReason.ERROR)

    asyncio.run(scenario())


def test_a_flip_whose_row_is_gone_cannot_store_its_seed_and_is_voided_unpublished() -> None:
    """``update_active_flip`` reporting no row is as bad as it raising."""

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.store.active.clear()
            await world.start(flip)
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            _assert_voided_unpublished(world, flip, VoidReason.ERROR)

    asyncio.run(scenario())


def test_a_round_voided_for_its_seed_leaves_no_row_and_no_commitment_for_a_restart() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            world.store.update_error = sqlite3.OperationalError("disk I/O error")
            await world.start(flip)
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            # Nothing for the next start to announce, and no promise outstanding.
            assert world.store.active == {}
            assert world.gateway.views_of(CommitmentView) == ()

    asyncio.run(scenario())


def test_a_void_stops_the_round_before_it_can_record_anything() -> None:
    """A flip that has been called off never charges anybody.

    The dangerous shape: a Join's lobby edit is still in the gateway, holding
    this flip's output, when the starter presses Start. The round takes the
    freed output lock behind the void that edit's failure triggers -- so unless
    voiding stops the round first, and waits for it, the round deals, shows down
    and commits money for a flip the group has been told was void.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAView(
            clock,
            # The lobby a third player joining draws, and only that one: the
            # two before it are how the lobby is set up.
            chosen=lambda view: (
                isinstance(view, LobbyView)
                and view.status is LobbyStatus.OPEN
                and view.player_count == 3
            ),
        )
        settled = 0

        async def counting_settle(deal: Deal, settings: Settings) -> FlipOutcome:
            nonlocal settled
            settled += 1
            return await settle_inline(deal, settings)

        world = harness_over(gateway, clock=clock, settle=counting_settle)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            joining = asyncio.create_task(flip.join(player(13), press_id=world.press()))
            await asyncio.wait_for(gateway.reached.wait(), timeout=1)

            # Start while that edit still holds the output lock: the round's
            # first message queues behind it.
            await world.start(flip)
            assert _phase(flip) is FlipPhase.DEALING

            # The edit now fails for good, which is what voids the flip.
            gateway.fail_edits = True
            gateway.go.set()
            assert await asyncio.wait_for(joining, timeout=1) is Notice.JOINED
            await asyncio.wait_for(flip.wait_closed(), timeout=1)
            # Whatever the round had left to do, it has no chance to do it.
            await world.clock.advance(timing.FLIP_WATCHDOG_SECONDS)

            assert _phase(flip) is FlipPhase.VOIDED
            # The round was stopped, not merely stopped from doing damage: it
            # never got as far as working a result out.
            assert settled == 0
            assert world.store.recorded == []
            assert flip.flip_number is None
            assert gateway.views_of(ResultView) == ()
            assert gateway.views_of(ShowdownView) == ()
            assert flip.token not in world.store.active
            assert world.registry.flip_for_group(world.group_id) is None

    asyncio.run(scenario())


def test_a_round_overtaken_by_a_decision_records_nothing_and_shows_nothing() -> None:
    """The backstop under :meth:`Flip.void`, which is deliberately out of reach.

    Voiding stops the round and waits for it before it decides anything, so no
    round step can run after a decision -- which is exactly why this reaches
    inside for the guards rather than through a race. They are what stops a
    future rearrangement from quietly putting the window back: a step that finds
    the flip over must refuse, not carry on into the showdown or the money.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.deal(flip)
            await flip.void(VoidReason.TIMED_OUT)
            assert _phase(flip) is FlipPhase.VOIDED
            shown = len(world.gateway.events)

            with pytest.raises(_FlipOverError):
                await flip._showdown()
            with pytest.raises(_FlipOverError):
                await flip._commit(world.clock.now())

            assert _phase(flip) is FlipPhase.VOIDED
            assert world.store.recorded == []
            assert flip.flip_number is None
            assert world.gateway.events[shown:] == []

    asyncio.run(scenario())


def test_a_round_that_voids_its_own_failure_does_not_wait_for_itself() -> None:
    """Voiding from inside the round must not cancel or await the calling task.

    The round voids its own failures -- a gateway that has given up, a seed that
    would not store -- and stopping the round is part of deciding a void, so the
    one task it must never stop is the one doing the deciding.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.gateway.fail_posts = True
            await world.start(flip)

            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            assert _phase(flip) is FlipPhase.VOIDED
            assert world.store.recorded == []
            assert flip.token not in world.store.active
            assert world.registry.flip_for_group(world.group_id) is None

    asyncio.run(scenario())


def test_recording_a_flip_retires_its_row_in_the_same_write() -> None:
    """With the row gone by the record, a failing ``remove_active_flip`` is never reached."""

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.store.remove_error = sqlite3.OperationalError("disk I/O error")
            await world.start(flip)
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            assert flip.flip_number == 42
            assert len(world.store.recorded) == 1
            assert flip.token not in world.store.active
            result = world.gateway.posts[-1].view
            assert isinstance(result, ResultView)
            assert result.flip_number == 42
            assert result.recorded

    asyncio.run(scenario())


def test_a_flip_that_cannot_be_recorded_keeps_its_row_when_it_cannot_be_cleared_either() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.store.record_error = sqlite3.OperationalError("disk I/O error")
            world.store.remove_error = sqlite3.OperationalError("disk I/O error")
            await world.start(flip)
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            assert world.store.recorded == []
            # Nothing was recorded, so the row left behind is voided by the next start.
            assert flip.token in world.store.active
            result = world.gateway.posts[-1].view
            assert isinstance(result, ResultView)
            assert result.flip_number is None
            assert not result.recorded

    asyncio.run(scenario())


def test_a_watchdog_firing_during_a_hanging_result_post_does_not_void_a_recorded_flip() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAView(clock, chosen=lambda view: isinstance(view, ResultView))
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            started = clock.monotonic()
            await world.start(flip)
            for _ in range(40):
                if world.store.recorded:
                    break
                await clock.advance_to_next()
            await clock.quiesce()

            assert gateway.reached.is_set()
            assert len(world.store.recorded) == 1
            assert flip.token not in world.store.active
            assert _phase(flip) is FlipPhase.FINISHED

            await clock.advance(started + timing.FLIP_WATCHDOG_SECONDS - clock.monotonic())

            assert _phase(flip) is FlipPhase.FINISHED
            assert gateway.views_of(VoidView) == ()
            assert len(world.store.recorded) == 1
            await asyncio.wait_for(flip.wait_closed(), timeout=1)
            assert world.registry.flip_for_group(world.group_id) is None

    asyncio.run(scenario())


def test_a_record_that_commits_while_the_watchdog_stops_the_round_is_still_told() -> None:
    """The write decides, even when the round is cancelled with it in flight.

    The watchdog cancels the round while the flip is being recorded, and the
    record commits anyway. Storage then has the flip as finished, so the one
    thing that must not happen is a notice telling the group it was voided --
    and the result is not lost either: the watchdog waited the write out, so it
    knows the number and owes the group the message the round never got back to.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = FakeGateway(clock)
        store = _HoldsTheRecord()
        world = harness_over(gateway, clock=clock, store=store)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            started = clock.monotonic()
            await world.start(flip)
            for _ in range(40):
                if store.entered.is_set():
                    break
                await clock.advance_to_next()
            assert store.entered.is_set()

            # Half an hour after Start, with the write still in the store.
            await clock.advance(started + timing.FLIP_WATCHDOG_SECONDS - clock.monotonic())
            assert _phase(flip) is FlipPhase.SHOWDOWN
            assert store.recorded == []

            store.record_gate.set()
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            assert _phase(flip) is FlipPhase.FINISHED
            assert len(store.recorded) == 1
            assert flip.token not in store.active
            assert gateway.views_of(VoidView) == ()
            # The result goes out once, with the number the write handed back, so
            # the group can read it and ``/verify`` can answer for it.
            (result,) = gateway.views_of(ResultView)
            assert isinstance(result, ResultView)
            assert result.flip_number == 42
            assert result.recorded
            assert result.seed == store.recorded[0].seed
            assert world.registry.flip_for_group(world.group_id) is None

    asyncio.run(scenario())


def test_a_round_that_has_said_everything_is_left_to_finish() -> None:
    """Closing the flip stops its timers, not the round that is seeing it out.

    By the time the announcement is going, the round has nothing left to do but
    wait for it, and cancelling it there would end a healthy flip on a cancelled
    task. Reaching for the task is the only way to tell, since the difference is
    exactly that nothing goes wrong.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            run = flip._run_task
            assert run is not None
            assert run.done()
            assert not run.cancelled()
            assert run.exception() is None
            timer = flip._timer_task
            assert timer is not None
            assert timer.cancelled()

    asyncio.run(scenario())


def test_a_second_void_joins_the_announcement_instead_of_cutting_it_short() -> None:
    """Nobody may cancel the teller, however the telling was taken on.

    The watchdog waits out a cancelled commit and the flip's announcement starts
    with the write; while that message is still in the gateway a second caller
    voids the flip. It must join the announcement, not close over it: closing
    used to stop the task that was mid-send and leave a flip that was recorded,
    not voided, having said nothing at all.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAView(clock, chosen=lambda view: isinstance(view, ResultView))
        store = _HoldsTheRecord()
        world = harness_over(gateway, clock=clock, store=store)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            started = clock.monotonic()
            await world.start(flip)
            for _ in range(40):
                if store.entered.is_set():
                    break
                await clock.advance_to_next()
            assert store.entered.is_set()

            # The watchdog fires and waits out the commit, which then succeeds.
            await clock.advance(started + timing.FLIP_WATCHDOG_SECONDS - clock.monotonic())
            store.record_gate.set()
            await asyncio.wait_for(gateway.reached.wait(), timeout=1)
            assert _phase(flip) is FlipPhase.FINISHED
            assert gateway.views_of(ResultView) == ()

            # A second ending caller arrives while that message is in flight.
            second = asyncio.create_task(flip.void(VoidReason.ERROR))
            await world.clock.quiesce()
            gateway.go.set()
            await asyncio.wait_for(second, timeout=1)
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            assert _phase(flip) is FlipPhase.FINISHED
            (result,) = gateway.views_of(ResultView)
            assert isinstance(result, ResultView)
            assert result.flip_number == 42
            assert gateway.views_of(VoidView) == ()
            assert len(world.store.recorded) == 1
            assert flip.token not in world.store.active

    asyncio.run(scenario())


def test_shutting_down_during_a_commit_still_tells_the_group_its_result() -> None:
    """The write decides the flip, so the write owns saying so.

    Stopping the bot cancels the round and the watchdog, and the commit it was
    waiting on is protected and lands anyway. Nothing that was watching is left
    -- and the flip has no row either, because recording it retired that -- so
    the announcement has to belong to the decision itself. This is the abrupt
    :meth:`FlipRegistry.close`, which waits for nothing at all.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = FakeGateway(clock)
        store = _HoldsTheRecord()
        world = harness_over(gateway, clock=clock, store=store)
        flip = await world.open_lobby(joiners=2)
        await world.start(flip)
        for _ in range(40):
            if store.entered.is_set():
                break
            await clock.advance_to_next()
        assert store.entered.is_set()

        world.registry.close()
        store.record_gate.set()
        await asyncio.wait_for(flip.wait_closed(), timeout=1)

        assert _phase(flip) is FlipPhase.FINISHED
        assert len(store.recorded) == 1
        assert flip.token not in store.active
        (result,) = gateway.views_of(ResultView)
        assert isinstance(result, ResultView)
        assert result.flip_number == 42
        assert result.seed == store.recorded[0].seed
        assert gateway.views_of(VoidView) == ()

    asyncio.run(scenario())


def test_shutting_down_waits_for_a_commit_and_tells_the_group_before_it_returns() -> None:
    """What the bot does, as opposed to dropping everything: it waits, briefly.

    The result has to be sent before the Telegram session closes, and a commit
    in flight is the one thing that decides whether there is a result at all, so
    stopping waits for it and for the message that follows.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = FakeGateway(clock)
        store = _HoldsTheRecord()
        world = harness_over(gateway, clock=clock, store=store)
        flip = await world.open_lobby(joiners=2)
        await world.start(flip)
        for _ in range(40):
            if store.entered.is_set():
                break
            await clock.advance_to_next()
        assert store.entered.is_set()

        stopping = asyncio.create_task(world.registry.shut_down())
        await asyncio.sleep(0)
        assert not stopping.done()
        store.record_gate.set()
        await asyncio.wait_for(stopping, timeout=2)

        # Everything was said by the time stopping returned.
        assert _phase(flip) is FlipPhase.FINISHED
        assert len(world.store.recorded) == 1
        (result,) = gateway.views_of(ResultView)
        assert isinstance(result, ResultView)
        assert result.flip_number == 42
        assert len(world.registry) == 0

    asyncio.run(scenario())


def test_shutting_down_does_not_wait_for_a_commit_that_never_comes_back() -> None:
    """A stop that cannot finish is worse than a result that arrives nowhere."""

    async def scenario() -> None:
        clock = FakeClock()
        gateway = FakeGateway(clock)
        store = _HoldsTheRecord()
        world = harness_over(gateway, clock=clock, store=store)
        flip = await world.open_lobby(joiners=2)
        await world.start(flip)
        for _ in range(40):
            if store.entered.is_set():
                break
            await clock.advance_to_next()
        assert store.entered.is_set()

        try:
            await asyncio.wait_for(world.registry.shut_down(within=0.05), timeout=2)

            assert len(world.registry) == 0
            assert gateway.views_of(ResultView) == ()
            assert gateway.views_of(VoidView) == ()
        finally:
            store.record_gate.set()
            await world.clock.quiesce()

    asyncio.run(scenario())


def test_voiding_a_flip_that_has_already_finished_says_nothing_twice() -> None:
    """A late void is inert, and in particular does not tell the result again.

    One outcome, told once: a flip that is already finished and has had its
    result posted gets no void notice and no second result, however late the
    caller is -- a watchdog that was slow to be cancelled, say.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)
            assert _phase(flip) is FlipPhase.FINISHED
            posted = len(world.gateway.posts)

            await flip.void(VoidReason.TIMED_OUT)

            assert _phase(flip) is FlipPhase.FINISHED
            assert len(world.gateway.posts) == posted
            assert len(world.gateway.views_of(ResultView)) == 1
            assert world.gateway.views_of(VoidView) == ()
            assert len(world.store.recorded) == 1
            assert flip.flip_number == 42

    asyncio.run(scenario())


def test_a_commit_that_failed_while_the_watchdog_stopped_the_round_is_still_told() -> None:
    """The other half of a drained commit: the write failed, and that is news too.

    Nothing was recorded, so the flip has no number and still has its row -- the
    only durable copy of the seed. The group gets the result all the same,
    saying the flip was not recorded, and the seed goes out with it; only then
    is the row let go.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = FakeGateway(clock)
        store = _HoldsTheRecord(fails=True)
        world = harness_over(gateway, clock=clock, store=store)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            started = clock.monotonic()
            await world.start(flip)
            for _ in range(40):
                if store.entered.is_set():
                    break
                await clock.advance_to_next()
            assert store.entered.is_set()

            await clock.advance(started + timing.FLIP_WATCHDOG_SECONDS - clock.monotonic())
            store.record_gate.set()
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            assert _phase(flip) is FlipPhase.FINISHED
            assert store.recorded == []
            assert flip.flip_number is None
            assert gateway.views_of(VoidView) == ()
            (result,) = gateway.views_of(ResultView)
            assert isinstance(result, ResultView)
            assert result.flip_number is None
            assert not result.recorded
            # The seed was revealed, so the row it was being kept on can go.
            assert result.seed is not None
            assert flip.token not in store.active

    asyncio.run(scenario())


def test_a_result_that_could_not_be_posted_leaves_the_row_for_the_next_start() -> None:
    """The seed outlives a result nobody could be told.

    A flip that was not recorded keeps its row until the seed has gone out. When
    the message cannot be delivered either, the row stays behind on purpose: the
    next start voids it and reveals the seed, which is the last place that deal
    can be checked from.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            world.store.record_error = sqlite3.OperationalError("disk I/O error")
            await world.start(flip)
            await world.clock.advance(timing.BOARD_GAP_SECONDS)
            world.gateway.fail_posts = True
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            assert world.store.recorded == []
            assert world.gateway.views_of(ResultView) == ()
            row = world.store.active[flip.token]
            assert row.seed is not None

    asyncio.run(scenario())


def test_a_result_post_that_never_returns_is_bounded_by_its_own_deadline() -> None:
    """A committed flip is never voided, and never holds its group for ever.

    The announcement has a deadline of its own, which is the only thing that
    stops it: a group whose result Telegram will not take waits two minutes, not
    the half hour the watchdog would have taken, and not for ever. The message
    is the one thing lost -- the flip stays finished and recorded.
    """

    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAView(clock, chosen=lambda view: isinstance(view, ResultView))
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            for _ in range(40):
                if gateway.reached.is_set():
                    break
                await clock.advance_to_next()
            assert gateway.reached.is_set()
            assert _phase(flip) is FlipPhase.FINISHED

            # Just short of the deadline the flip is still trying, and still
            # holding its group.
            await clock.advance(timing.TELLING_SECONDS - 1)
            assert world.registry.flip_for_group(world.group_id) is flip
            await clock.advance(1)
            await asyncio.wait_for(flip.wait_closed(), timeout=1)

            assert _phase(flip) is FlipPhase.FINISHED
            assert flip.flip_number == 42
            assert len(world.store.recorded) == 1
            assert gateway.views_of(VoidView) == ()
            # Nothing went out, and nothing posted it twice either.
            assert gateway.views_of(ResultView) == ()
            assert world.registry.flip_for_group(world.group_id) is None

    asyncio.run(scenario())


def test_the_watchdog_runs_from_start_even_when_the_start_press_is_still_unanswered() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            press = world.press()
            gateway.slow_press = press
            start = asyncio.create_task(flip.start(flip.starter.user_id, press_id=press))
            await gateway.reached.wait()

            await clock.advance(timing.FLIP_WATCHDOG_SECONDS - 1)
            assert _phase(flip) is FlipPhase.DEALING
            await clock.advance(1)
            assert _phase(flip) is FlipPhase.VOIDED

            gateway.go.set()
            await start
            await asyncio.wait_for(flip.wait_closed(), timeout=1)
            _assert_voided_unpublished(world, flip, VoidReason.TIMED_OUT)

    asyncio.run(scenario())


def test_nothing_is_published_until_the_start_press_has_been_answered() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            press = world.press()
            gateway.slow_press = press
            start = asyncio.create_task(flip.start(flip.starter.user_id, press_id=press))
            await gateway.reached.wait()
            await clock.quiesce()

            assert gateway.views_of(CommitmentView) == ()
            assert gateway.views_of(TableView) == ()

            gateway.go.set()
            await start
            await world.finish(flip)

            assert len(gateway.views_of(CommitmentView)) == 1
            assert _phase(flip) is FlipPhase.FINISHED

    asyncio.run(scenario())


def test_a_start_handler_cancelled_during_its_answer_still_runs_the_round() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            press = world.press()
            gateway.slow_press = press
            start = asyncio.create_task(flip.start(flip.starter.user_id, press_id=press))
            await gateway.reached.wait()

            start.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await start
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            assert len(world.store.recorded) == 1

    asyncio.run(scenario())


def test_shutting_down_during_the_start_answer_leaves_no_orphan_round() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        gateway = HoldsAnAnswer(clock)
        world = harness_over(gateway, clock=clock)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            press = world.press()
            gateway.slow_press = press
            start = asyncio.create_task(flip.start(flip.starter.user_id, press_id=press))
            await gateway.reached.wait()

            world.registry.close()
            gateway.go.set()
            await start
            await clock.quiesce()

            assert gateway.views_of(CommitmentView) == ()
            assert gateway.views_of(TableView) == ()
            assert world.registry.flips == ()
            assert clock.sleeping == 0

    asyncio.run(scenario())


def test_a_second_flip_in_the_same_group_is_refused_and_points_at_the_first() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            first = await world.open_lobby(joiners=0)

            with pytest.raises(FlipAlreadyOpenError) as refused:
                await world.registry.create_flip(
                    group_id=world.group_id, starter=player(20), settings=world.settings
                )

            assert refused.value.flip is first
            assert refused.value.group_id == world.group_id
            assert world.registry.flip_for_group(world.group_id) is first
            assert len(world.gateway.posts) == 1
            assert list(world.store.active) == [first.token]

    asyncio.run(scenario())


def test_another_group_can_open_a_flip_at_the_same_time() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            here = await world.open_lobby(joiners=0)
            there = await world.open_lobby(joiners=0, group_id=_OTHER_GROUP)

            assert here is not there
            assert world.registry.flip_for_group(world.group_id) is here
            assert world.registry.flip_for_group(_OTHER_GROUP) is there
            assert world.registry.flip_for_token(there.token) is there
            assert set(world.registry.flips) == {here, there}

    asyncio.run(scenario())


def test_a_lobby_that_cannot_be_posted_leaves_nothing_behind() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            world.gateway.fail_posts = True
            with pytest.raises(PermanentFailureError):
                await world.registry.create_flip(
                    group_id=world.group_id, starter=player(11), settings=world.settings
                )
            assert world.registry.flip_for_group(world.group_id) is None
            assert len(world.registry) == 0
            assert world.store.active == {}

            world.gateway.fail_posts = False
            flip = await world.open_lobby(joiners=0)
            assert world.registry.flip_for_group(world.group_id) is flip

    asyncio.run(scenario())


def test_a_lobby_whose_row_cannot_be_written_leaves_nothing_behind() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            world.store.add_error = sqlite3.OperationalError("disk I/O error")
            with pytest.raises(sqlite3.OperationalError):
                await world.registry.create_flip(
                    group_id=world.group_id, starter=player(11), settings=world.settings
                )
            await world.clock.quiesce()
            assert world.registry.flip_for_group(world.group_id) is None
            assert len(world.registry) == 0
            assert world.store.active == {}
            assert world.clock.sleeping == 0

            world.store.add_error = None
            flip = await world.open_lobby(joiners=0)
            assert world.registry.flip_for_group(world.group_id) is flip
            assert list(world.store.active) == [flip.token]

    asyncio.run(scenario())


def test_the_flip_follows_its_message_when_the_gateway_reposts_it() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            original = flip.message_id

            world.gateway.repost_next_edit = True
            await flip.join(player(12), press_id=world.press())

            moved = flip.message_id
            assert moved != original
            assert world.store.active[flip.token].message_id == moved

            await flip.join(player(13), press_id=world.press())
            assert world.gateway.edits[-1].message_id == moved
            assert world.store.active[flip.token].message_id == moved

    asyncio.run(scenario())


def test_closing_the_registry_stops_every_timer_and_keeps_the_rows() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            lobby = await world.open_lobby(joiners=0)
            running = await world.open_lobby(joiners=1, group_id=_OTHER_GROUP)
            await world.deal(running)
            assert world.clock.sleeping > 0
            events = len(world.gateway.events)

            world.registry.close()
            await world.clock.quiesce()

            assert world.clock.sleeping == 0
            assert world.registry.flips == ()
            assert set(world.store.active) == {lobby.token, running.token}

            await world.clock.advance(timing.FLIP_WATCHDOG_SECONDS + timing.LOBBY_MAX_AGE_SECONDS)
            assert len(world.gateway.events) == events
            assert set(world.store.active) == {lobby.token, running.token}
            assert world.store.recorded == []
            assert _phase(lobby) is FlipPhase.LOBBY
            assert _phase(running) is FlipPhase.DEALING

    asyncio.run(scenario())


def test_a_migrated_flip_keeps_its_slot_token_and_topic_under_the_new_id() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1, topic_id=_TOPIC)

            moved = await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )

            assert moved is flip
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is flip
            # The slot moved, and the old id still names the group it belonged to.
            assert world.registry.group_id_for(world.group_id) == _MIGRATED_GROUP
            assert world.registry.flip_for_group(world.group_id) is flip
            assert world.registry.flip_for_token(flip.token) is flip
            assert len(world.registry) == 1
            assert flip.group_id == _MIGRATED_GROUP
            assert flip.topic_id == _TOPIC

    asyncio.run(scenario())


def test_a_migrated_flips_row_follows_it_to_the_new_group_id() -> None:
    """The row the next start would void has to name the chat the flip is in."""

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1, topic_id=_TOPIC)
            before = world.store.active[flip.token]

            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )

            row = world.store.active[flip.token]
            assert row.group_id == _MIGRATED_GROUP
            # Only the id moved: the row still points at the same lobby message,
            # in the same topic, with the same seed and state.
            assert row == replace(before, group_id=_MIGRATED_GROUP)

    asyncio.run(scenario())


def test_a_flip_whose_row_cannot_be_moved_still_follows_its_group() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            world.store.update_error = sqlite3.OperationalError("disk I/O error")

            moved = await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )

            assert moved is flip
            assert flip.group_id == _MIGRATED_GROUP
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is flip
            assert world.store.active[flip.token].group_id == world.group_id

            world.store.update_error = None
            await world.finish(flip)
            assert _phase(flip) is FlipPhase.CANCELLED

    asyncio.run(scenario())


def test_everything_a_flip_says_after_its_group_migrates_goes_to_the_new_id_and_topic() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1, topic_id=_TOPIC)
            before = len(world.gateway.events)
            old = MessageTarget(group_id=world.group_id, topic_id=_TOPIC)
            new = MessageTarget(group_id=_MIGRATED_GROUP, topic_id=_TOPIC)

            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )
            await world.deal(flip)
            await world.finish(flip)

            earlier = _messages(world.gateway.events[:before])
            later = _messages(world.gateway.events[before:])
            assert earlier
            assert later
            assert {event.target for event in earlier} == {old}
            assert {event.target for event in later} == {new}
            assert isinstance(later[-1].view, ResultView)

    asyncio.run(scenario())


def test_the_flip_recorded_after_a_migration_carries_the_new_group_id() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)

            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )
            await world.deal(flip)
            await world.finish(flip)

            assert _phase(flip) is FlipPhase.FINISHED
            assert world.store.recorded[-1].group_id == _MIGRATED_GROUP
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is None
            assert world.registry.flips == ()

    asyncio.run(scenario())


def test_either_id_is_refused_a_second_flip_once_a_group_has_moved() -> None:
    """Both ids are one chat, which gets one flip, whichever id asks for it."""

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            moved = await world.open_lobby(joiners=0)
            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )

            for asked in (_MIGRATED_GROUP, world.group_id):
                with pytest.raises(FlipAlreadyOpenError) as refused:
                    await world.registry.create_flip(
                        group_id=asked, starter=player(20), settings=world.settings
                    )
                assert refused.value.flip is moved
                assert refused.value.group_id == _MIGRATED_GROUP

            assert world.registry.flips == (moved,)
            assert moved.group_id == _MIGRATED_GROUP
            assert len(world.registry) == 1

    asyncio.run(scenario())


def test_a_flip_opened_under_a_retired_id_opens_in_the_group_that_id_belonged_to() -> None:
    """A command read before the move and acted on after it is still that group's.

    The id it was read under is dead, so a flip keyed by it would be a second
    flip in the chat and would be talking to nobody.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )

            late = await world.registry.create_flip(
                group_id=world.group_id, starter=player(20), settings=world.settings
            )

            assert late.group_id == _MIGRATED_GROUP
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is late
            assert world.registry.flip_for_group(world.group_id) is late
            assert len(world.registry) == 1
            assert world.store.active[late.token].group_id == _MIGRATED_GROUP

    asyncio.run(scenario())


def test_migrating_to_the_same_id_or_from_a_group_with_no_flip_does_nothing() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0)
            events = len(world.gateway.events)

            same = await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=world.group_id
            )
            empty = await world.registry.migrate_group(
                old_group_id=_OTHER_GROUP, new_group_id=_MIGRATED_GROUP
            )

            assert same is None
            assert empty is None
            assert world.registry.flip_for_group(world.group_id) is flip
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is None
            assert flip.group_id == world.group_id
            assert len(world.registry) == 1
            assert len(world.gateway.events) == events

    asyncio.run(scenario())


def test_migrating_onto_a_group_that_has_a_flip_voids_the_one_that_cannot_move() -> None:
    """One chat cannot keep two flips, so the one on the retired id is given up.

    Both of them are in the chat the new id names, and the move has nowhere to
    put the one that was there before.
    """

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            stale = await world.open_lobby(joiners=0)
            kept = await world.open_lobby(joiners=0, group_id=_OTHER_GROUP)

            moved = await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_OTHER_GROUP
            )
            # Giving it up is the registry's own task, so it runs on the next
            # pass rather than inside the move: see FlipRegistry._abandon.
            await world.clock.quiesce()

            assert moved is None
            assert stale.closed
            assert _phase(stale) is FlipPhase.VOIDED
            assert stale.token not in world.store.active
            # The surviving flip keeps its place, and both ids now name its chat.
            assert world.registry.flips == (kept,)
            assert world.registry.flip_for_group(_OTHER_GROUP) is kept
            assert world.registry.flip_for_group(world.group_id) is kept
            assert kept.group_id == _OTHER_GROUP
            assert _phase(kept) is FlipPhase.LOBBY

    asyncio.run(scenario())


def test_an_edit_in_flight_when_the_group_migrates_stays_on_the_old_id() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2, topic_id=_TOPIC)
            await world.start(flip)
            world.gateway.hold()
            await world.clock.advance(timing.BOARD_GAP_SECONDS)  # Board 2 goes to edit
            assert world.gateway.waiting == 1
            held = len(world.gateway.events)

            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )
            world.gateway.release()
            await world.clock.quiesce()

            assert world.gateway.waiting == 0
            landed = world.gateway.events[held]
            assert isinstance(landed, Edited)
            assert landed.target == MessageTarget(group_id=world.group_id, topic_id=_TOPIC)

            await world.finish(flip)
            after = _messages(world.gateway.events[held + 1 :])
            assert after
            assert {event.target for event in after} == {
                MessageTarget(group_id=_MIGRATED_GROUP, topic_id=_TOPIC)
            }

    asyncio.run(scenario())


def test_a_flip_that_has_already_closed_is_not_migrated() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.deal(flip)
            await world.finish(flip)
            events = len(world.gateway.events)

            moved = await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )

            assert moved is None
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is None
            assert world.registry.flips == ()
            assert len(world.gateway.events) == events

    asyncio.run(scenario())


def test_the_lobby_timeout_after_a_migration_is_posted_in_the_new_group() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1, topic_id=_TOPIC)

            await world.registry.migrate_group(
                old_group_id=world.group_id, new_group_id=_MIGRATED_GROUP
            )
            await world.clock.advance(timing.LOBBY_IDLE_SECONDS)

            assert _phase(flip) is FlipPhase.CANCELLED
            last = world.gateway.edits[-1]
            assert isinstance(last.view, LobbyView)
            assert last.view.status is LobbyStatus.TIMED_OUT
            assert last.target == MessageTarget(group_id=_MIGRATED_GROUP, topic_id=_TOPIC)
            assert world.registry.flip_for_group(_MIGRATED_GROUP) is None

    asyncio.run(scenario())


# ---------------------------------------------------------------- defaulted settings

_OMITTED = FlipRequest(wager=50, x=5)
"""A ``/flip 50 5``: Y and Z are left for the bot to fill in."""


def _settings_of(flip: Flip) -> Settings | None:
    """The settings read afresh, so the type checker does not assume they cannot change."""
    return flip.settings


@pytest.mark.parametrize("player_count", [2, 3, 8])
def test_an_omitted_z_becomes_the_number_of_players_when_start_is_pressed(
    player_count: int,
) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=player_count - 1, settings=_OMITTED)
            assert flip.player_count == player_count
            assert flip.settings is None

            await world.start(flip)

            assert _settings_of(flip) == Settings(wager=50, x=5, y=21, z=player_count)
            assert flip.request == _OMITTED
            assert (flip.request.y, flip.request.z) == (None, None)

    asyncio.run(scenario())


def test_a_player_leaving_before_start_changes_the_resolved_z() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=3, settings=_OMITTED)
            assert flip.player_count == 4
            assert await flip.leave(13, press_id=world.press()) is Notice.LEFT
            assert flip.settings is None

            await world.start(flip)
            resolved = _settings_of(flip)
            assert resolved is not None
            assert resolved.z == 3

    asyncio.run(scenario())


def test_a_join_after_start_cannot_change_the_resolved_z() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2, settings=_OMITTED)
            await world.start(flip)
            resolved = flip.settings
            assert resolved == Settings(wager=50, x=5, y=21, z=3)

            late = await flip.join(player(40), press_id=world.press())

            assert late is not Notice.JOINED
            assert player(40) not in flip.players
            assert flip.settings == resolved
            await world.finish(flip)
            assert world.store.recorded[-1].settings == resolved
            assert len(world.store.recorded[-1].players) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("player_count", [2, 3])
def test_an_explicit_z_is_not_touched_by_the_number_of_players(player_count: int) -> None:
    async def scenario() -> None:
        given = Settings(wager=50, x=5, y=2, z=6)
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=player_count - 1, settings=given)
            await world.start(flip)

            assert flip.settings == given
            assert flip.request == FlipRequest(wager=50, x=5, y=2, z=6)

    asyncio.run(scenario())


@pytest.mark.parametrize("player_count", [2, 3, 8])
def test_an_omitted_y_deals_as_many_extra_cards_as_fit(player_count: int) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=player_count - 1, settings=_OMITTED)
            await world.start(flip)
            await world.finish(flip)

            recorded = world.store.recorded[-1]
            expected = extra_rounds_for(player_count=player_count, x=5, y=21)
            assert recorded.settings.y == 21
            assert recorded.deal.y == recorded.settings.y
            assert recorded.deal.extra_count == expected
            assert recorded.deal.extra_count < 21

    asyncio.run(scenario())


def test_the_extra_cards_of_an_omitted_y_depend_on_the_table_size() -> None:
    assert extra_rounds_for(player_count=2, x=5, y=21) == 16
    assert extra_rounds_for(player_count=8, x=5, y=21) == 0


@pytest.mark.parametrize("player_count", [2, 3])
def test_a_scoop_pays_the_wager_times_the_player_count_when_z_is_omitted(
    player_count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        ids = [11 + index for index in range(player_count)]
        explicit = Settings(wager=50, x=5, y=21, z=player_count)
        seed = seed_where(
            lambda outcome: outcome.scoop_seat is not None, player_ids=ids, settings=explicit
        )
        scooper = outcome_for(seed, ids, explicit).scoop_seat
        assert scooper is not None
        use_seed(monkeypatch, seed)

        nets: list[list[int]] = []
        requests: tuple[FlipRequest | Settings, ...] = (_OMITTED, explicit)
        for request in requests:
            world = harness()
            async with world.registry:
                flip = await world.open_lobby(joiners=player_count - 1, settings=request)
                await world.start(flip)
                await world.finish(flip)
                (recorded,) = world.store.recorded
                assert recorded.settings == explicit
                nets.append([line.net_dollars for line in recorded.players])

        owed = 50 * player_count
        expected = [
            owed * (player_count - 1) if seat == scooper else -owed for seat in range(player_count)
        ]
        assert nets == [expected, expected]
        assert sum(expected) == 0

    asyncio.run(scenario())


def test_the_recorded_flip_carries_the_resolved_settings_and_what_was_typed() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2, settings=_OMITTED)
            await world.start(flip)
            await world.finish(flip)

            recorded = world.store.recorded[-1]
            assert recorded.settings.z == 3
            assert recorded.settings.y == 21
            assert recorded.y_defaulted is True
            assert recorded.z_defaulted is True
            assert recorded.request == _OMITTED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("request_", "y_defaulted", "z_defaulted"),
    [
        (FlipRequest(wager=50, x=5, y=2, z=3), False, False),
        (FlipRequest(wager=50, x=5, y=2), False, True),
        (FlipRequest(wager=50, x=5, z=3), True, False),
    ],
)
def test_the_recorded_flags_say_which_values_were_typed(
    request_: FlipRequest, y_defaulted: bool, z_defaulted: bool
) -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2, settings=request_)
            await world.start(flip)
            await world.finish(flip)

            recorded = world.store.recorded[-1]
            assert recorded.y_defaulted is y_defaulted
            assert recorded.z_defaulted is z_defaulted
            assert recorded.request == request_

    asyncio.run(scenario())


def test_a_plain_settings_flip_records_nothing_as_defaulted() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)

            recorded = world.store.recorded[-1]
            assert recorded.settings == world.settings
            assert recorded.y_defaulted is False
            assert recorded.z_defaulted is False

    asyncio.run(scenario())


@pytest.mark.parametrize(("x", "cap"), [(5, 8), (7, 6), (21, 2)])
def test_the_player_cap_of_a_defaulted_flip_is_still_set_by_x(x: int, cap: int) -> None:
    async def scenario() -> None:
        assert cap == 42 // x
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=0, settings=FlipRequest(wager=50, x=x))
            for joiner in players(cap - 1, first_id=12):
                assert await flip.join(joiner, press_id=world.press()) is Notice.JOINED
            assert flip.player_count == cap

            assert await flip.join(player(30), press_id=world.press()) is Notice.LOBBY_FULL
            assert flip.player_count == cap
            assert player(30) not in flip.players

    asyncio.run(scenario())


def test_a_lobby_with_an_omitted_z_survives_losing_every_player() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1, settings=_OMITTED)
            assert await flip.leave(11, press_id=world.press()) is Notice.LEFT
            assert await flip.leave(12, press_id=world.press()) is Notice.LEFT
            assert flip.player_count == 0
            assert flip.settings is None

            last = world.gateway.edits[-1].view
            assert isinstance(last, LobbyView)
            assert last.players == ()
            assert last.request == _OMITTED
            assert not last.full

            assert await flip.start(11, press_id=world.press()) is Notice.NEED_MORE_PLAYERS
            assert _phase(flip) is FlipPhase.LOBBY
            assert flip.settings is None

    asyncio.run(scenario())

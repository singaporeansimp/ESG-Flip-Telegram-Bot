"""Fakes the engine tests run a whole flip on: a clock, a gateway and a store.

No network, no database and no waiting. Logical time only moves when a test moves
it, so a flip with a thirty-second peek window plays out in a millisecond and the
timing rules can be asserted to the second.

Keep it that way. Everything here yields to the event loop with
:func:`asyncio.sleep` of zero and never sleeps for real or hands work to a thread,
which is what makes :meth:`FakeClock.advance` enough to drive a flip to its next
decision. That is also why the tests settle the showdown with
:func:`settle_inline` instead of the engine's default worker thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from esgbot.engine import (
    Flip,
    FlipRegistry,
    FlipView,
    MessageTarget,
    PermanentFailureError,
    Player,
    PressAnswer,
    Settler,
    timing,
)
from esgbot.game import fairness
from esgbot.game.deal import Deal, deal_cards
from esgbot.game.rules import FlipRequest, Settings
from esgbot.game.settlement import FlipOutcome, settle_deal
from esgbot.storage import ActiveFlip, FinishedFlip, FlipState

__all__ = [
    "EPOCH",
    "GROUP",
    "SETTINGS",
    "Answered",
    "Edited",
    "Event",
    "FakeClock",
    "FakeGateway",
    "FakeStore",
    "Harness",
    "HoldsAView",
    "HoldsAnAnswer",
    "Posted",
    "harness",
    "harness_over",
    "outcome_for",
    "player",
    "players",
    "seed_where",
    "settle_inline",
    "use_seed",
]

EPOCH: Final = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
"""Where a :class:`FakeClock` starts, so stored timestamps read sensibly."""

GROUP: Final = -1001
"""The group the helpers open flips in."""

SETTINGS: Final = Settings(wager=50, x=5, y=2, z=3)
"""Five hole cards each, up to two extra, a triple scoop: eight players fit."""

_DRAIN_PASSES: Final = 64
"""Event-loop turns one :meth:`FakeClock.quiesce` gives the engine to catch up.

Each pass lets every task that is ready run until it next suspends, and the
engine only ever suspends on the clock, a lock, or one of these fakes. A flip
needs a handful between decisions, so this is room to spare rather than a
deadline to tune.
"""


async def settle_inline(deal: Deal, settings: Settings) -> FlipOutcome:
    """Settle a deal on the event loop, so a test never waits on a worker thread.

    The engine's own default, :func:`esgbot.engine.settle_in_thread`, is what the
    bot runs; one test checks it really does hand the work to another thread.
    """
    return settle_deal(deal, settings)


def outcome_for(seed: bytes, player_ids: Sequence[int], settings: Settings) -> FlipOutcome:
    """What a flip on this seed would come to, without running one."""
    return settle_deal(
        deal_cards(seed=seed, player_ids=player_ids, x=settings.x, y=settings.y), settings
    )


def seed_where(
    wanted: Callable[[FlipOutcome], bool],
    *,
    player_ids: Sequence[int],
    settings: Settings = SETTINGS,
    tries: int = 5_000,
) -> bytes:
    """The first seed whose result satisfies ``wanted``: a scoop, a chop, a full tie.

    The search walks seeds in order rather than drawing them at random, so a test
    that asks for a scoop gets the same scoop every time it runs. Pair it with
    :func:`use_seed` to make the engine deal that seed.

    Raises:
        AssertionError: if no seed in ``tries`` fits, which means the condition is
            wrong rather than rare.
    """
    for attempt in range(tries):
        seed = attempt.to_bytes(fairness.SEED_SIZE, "big")
        if wanted(outcome_for(seed, player_ids, settings)):
            return seed
    raise AssertionError(f"no seed in {tries} gave the result the test wanted")


def use_seed(monkeypatch: pytest.MonkeyPatch, seed: bytes) -> None:
    """Make the next flip deal this seed instead of a random one.

    The seed is the one thing a flip draws for itself, so a test that needs a
    particular result pins it here. Everything else about the deal follows from
    it, exactly as it does in the bot.
    """
    monkeypatch.setattr(fairness, "generate_seed", lambda: seed)


def player(user_id: int, name: str | None = None, username: str | None = None) -> Player:
    """One player, named after their id unless told otherwise."""
    return Player(
        user_id=user_id,
        display_name=name if name is not None else f"Player {user_id}",
        username=username,
    )


def players(count: int, *, first_id: int = 11) -> tuple[Player, ...]:
    """``count`` players with consecutive ids, in join order."""
    return tuple(player(first_id + index) for index in range(count))


@dataclass(slots=True)
class _Sleeper:
    deadline: float
    waiter: asyncio.Future[None]

    def wake(self) -> None:
        if not self.waiter.done():
            self.waiter.set_result(None)


class FakeClock:
    """A clock that only moves when a test moves it.

    :meth:`monotonic` and :meth:`now` keep step, so a flip's stored timestamps
    match the seconds the test advanced. :meth:`advance` wakes every sleep that
    comes due on the way, in order, letting the engine run between each one, so
    the engine sees exactly the times it would have seen in real life.
    """

    __slots__ = ("_monotonic", "_origin", "_sleepers", "_start")

    def __init__(self, *, start: datetime = EPOCH, monotonic: float = 10_000.0) -> None:
        self._start = start
        self._origin = monotonic
        self._monotonic = monotonic
        self._sleepers: list[_Sleeper] = []

    def now(self) -> datetime:
        """Wall-clock time, as far from :data:`EPOCH` as the clock has been advanced."""
        return self._start + timedelta(seconds=self._monotonic - self._origin)

    def monotonic(self) -> float:
        """The current reading. It only changes in :meth:`advance`."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Wait for the clock to be advanced this far, or return now if it is not positive."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        sleeper = _Sleeper(
            deadline=self._monotonic + seconds, waiter=asyncio.get_running_loop().create_future()
        )
        self._sleepers.append(sleeper)
        try:
            await sleeper.waiter
        finally:
            if sleeper in self._sleepers:
                self._sleepers.remove(sleeper)

    @property
    def sleeping(self) -> int:
        """How many sleeps are waiting on the clock right now."""
        return len(self._sleepers)

    @property
    def next_deadline(self) -> float | None:
        """The earliest reading anything is waiting for, or ``None`` if nothing is."""
        return min((sleeper.deadline for sleeper in self._sleepers), default=None)

    async def quiesce(self) -> None:
        """Let everything that can run, run, without moving the clock."""
        for _ in range(_DRAIN_PASSES):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Move the clock on, running each sleep that comes due at the time it does.

        Raises:
            ValueError: if asked to go backwards.
        """
        if seconds < 0:
            raise ValueError(f"the clock cannot go back, got {seconds}")
        target = self._monotonic + seconds
        await self.quiesce()
        while True:
            due = min(
                (sleeper.deadline for sleeper in self._sleepers if sleeper.deadline <= target),
                default=None,
            )
            if due is None:
                break
            self._monotonic = due
            for sleeper in list(self._sleepers):
                if sleeper.deadline <= due:
                    sleeper.wake()
            await self.quiesce()
        self._monotonic = target
        await self.quiesce()

    async def advance_to_next(self) -> float:
        """Move the clock to the next deadline, whatever it is, and return the gap.

        Returns ``0.0`` when nothing is waiting, which is how a test can tell the
        engine has stopped expecting time to pass.
        """
        await self.quiesce()
        deadline = self.next_deadline
        if deadline is None:
            return 0.0
        gap = deadline - self._monotonic
        await self.advance(gap)
        return gap


@dataclass(frozen=True, slots=True)
class Posted:
    """A message the engine posted."""

    at: float
    target: MessageTarget
    message_id: int
    view: FlipView


@dataclass(frozen=True, slots=True)
class Edited:
    """An edit the engine made, and the id it landed on."""

    at: float
    target: MessageTarget
    message_id: int
    view: FlipView


@dataclass(frozen=True, slots=True)
class Answered:
    """A button press the engine answered."""

    at: float
    press_id: str
    answer: PressAnswer


Event = Posted | Edited | Answered
"""Anything the gateway was asked to do, in order, each stamped with the clock."""


class FakeGateway:
    """A gateway that writes down what it was asked to show, when.

    Every call is stamped with the clock, so a test can check the two-second gap
    between the boards or the five-second limit on peek-count edits by reading
    :attr:`events`. The failure switches are what the engine's unhappy paths are
    tested through:

    * :meth:`hold` makes posts and edits wait, as a flood-control pause or a
      Telegram outage would, without the clock moving. :meth:`release` lets them
      through. A press is still answered while a post is held, which is the point.
    * ``fail_posts`` and ``fail_edits`` raise
      :class:`~esgbot.engine.PermanentFailureError`, which is what the real
      gateway does once a call has failed three times.
    * ``fail_answers`` makes every pop-up come back undelivered, as the real
      gateway reports one Telegram would not take. ``raise_on_answers`` raises
      :class:`~esgbot.engine.PermanentFailureError` from it instead, which the
      real gateway never does and the engine survives anyway.
    * ``repost_next_edit`` returns a fresh message id for the next edit, as the
      real gateway does when somebody has deleted the table message.
    """

    __slots__ = (
        "_gate",
        "_next_message_id",
        "clock",
        "events",
        "fail_answers",
        "fail_edits",
        "fail_posts",
        "raise_on_answers",
        "repost_next_edit",
        "waiting",
    )

    def __init__(self, clock: FakeClock, *, first_message_id: int = 500) -> None:
        self.clock = clock
        self.events: list[Event] = []
        self.fail_posts = False
        self.fail_edits = False
        self.fail_answers = False
        self.raise_on_answers = False
        self.repost_next_edit = False
        self.waiting = 0
        self._next_message_id = first_message_id
        self._gate: asyncio.Event | None = None

    # ------------------------------------------------------------------ the gate

    def hold(self) -> None:
        """Make every post and edit from now on wait, as a slow Telegram would."""
        if self._gate is None:
            self._gate = asyncio.Event()

    def release(self) -> None:
        """Let held posts and edits through, and stop holding new ones."""
        gate, self._gate = self._gate, None
        if gate is not None:
            gate.set()

    @property
    def held(self) -> bool:
        """Whether posts and edits are being held."""
        return self._gate is not None

    async def _pass_gate(self) -> None:
        gate = self._gate
        if gate is None:
            await asyncio.sleep(0)
            return
        self.waiting += 1
        try:
            await gate.wait()
        finally:
            self.waiting -= 1

    # -------------------------------------------------------------- the gateway

    async def post(self, target: MessageTarget, view: FlipView) -> int:
        await self._pass_gate()
        if self.fail_posts:
            raise PermanentFailureError(f"cannot post to {target.group_id}")
        message_id = self._next_message_id
        self._next_message_id += 1
        self.events.append(
            Posted(at=self.clock.monotonic(), target=target, message_id=message_id, view=view)
        )
        return message_id

    async def edit(self, target: MessageTarget, message_id: int, view: FlipView) -> int:
        await self._pass_gate()
        if self.fail_edits:
            raise PermanentFailureError(f"cannot edit {message_id}")
        if self.repost_next_edit:
            self.repost_next_edit = False
            message_id = self._next_message_id
            self._next_message_id += 1
        self.events.append(
            Edited(at=self.clock.monotonic(), target=target, message_id=message_id, view=view)
        )
        return message_id

    async def answer_press(self, press_id: str, answer: PressAnswer) -> bool:
        await asyncio.sleep(0)
        if self.fail_answers:
            return False
        if self.raise_on_answers:
            raise PermanentFailureError(f"cannot answer {press_id}")
        self.events.append(Answered(at=self.clock.monotonic(), press_id=press_id, answer=answer))
        return True

    # ----------------------------------------------------------------- readback

    @property
    def posts(self) -> tuple[Posted, ...]:
        """The messages that were posted, in order."""
        return tuple(event for event in self.events if isinstance(event, Posted))

    @property
    def edits(self) -> tuple[Edited, ...]:
        """The edits that were made, in order."""
        return tuple(event for event in self.events if isinstance(event, Edited))

    @property
    def answers(self) -> tuple[Answered, ...]:
        """The button presses that were answered, in order."""
        return tuple(event for event in self.events if isinstance(event, Answered))

    @property
    def views(self) -> tuple[FlipView, ...]:
        """Every view the engine asked for, posted or edited, in order."""
        return tuple(event.view for event in self.events if isinstance(event, (Posted, Edited)))

    def views_of(self, kind: type[FlipView]) -> tuple[FlipView, ...]:
        """Every view of one kind, in order."""
        return tuple(view for view in self.views if isinstance(view, kind))


class HoldsAView(FakeGateway):
    """A gateway that sits on the first post or edit of one view the test chooses.

    :meth:`FakeGateway.hold` holds everything; this holds one message, which is
    what lines a slow Telegram up against an exact moment in a round -- the
    commitment, the first table edit, a peek-count edit, the result. ``reached`` is
    set once that call is waiting, so a test can synchronise on it, and ``go``
    lets it through. A ``go`` that is never set is output that never comes back
    at all.
    """

    def __init__(self, clock: FakeClock, *, chosen: Callable[[FlipView], bool]) -> None:
        super().__init__(clock)
        self.chosen = chosen
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def _maybe_hold(self, view: FlipView) -> None:
        if self.reached.is_set() or not self.chosen(view):
            return
        self.reached.set()
        await self.go.wait()

    async def post(self, target: MessageTarget, view: FlipView) -> int:
        await self._maybe_hold(view)
        return await super().post(target, view)

    async def edit(self, target: MessageTarget, message_id: int, view: FlipView) -> int:
        await self._maybe_hold(view)
        return await super().edit(target, message_id, view)


class HoldsAnAnswer(FakeGateway):
    """A gateway that sits on the answer to one press until the test lets it go.

    Every other press is answered as usual, so one player's pop-up can be held
    while the rest of the flip carries on -- which is how a press answered after
    the flip has moved on is tested. ``reached`` is set once that answer is
    waiting and ``go`` releases it. ``slow_press`` can be set after
    construction, because a press id usually comes from the harness this gateway
    is built into.
    """

    def __init__(self, clock: FakeClock, *, slow_press: str | None = None) -> None:
        super().__init__(clock)
        self.slow_press = slow_press
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def answer_press(self, press_id: str, answer: PressAnswer) -> bool:
        if press_id == self.slow_press:
            self.reached.set()
            await self.go.wait()
        return await super().answer_press(press_id, answer)


class FakeStore:
    """A store that keeps flips in a dictionary, and fails when a test asks it to.

    Set ``record_error``, ``add_error``, ``update_error`` or ``remove_error`` to
    the exception that call should raise: a :class:`sqlite3.Error` or a
    :class:`~esgbot.storage.StorageError`, which are the two the engine catches.

    :meth:`record_flip` takes the token of the flip's in-progress row and drops
    it, because the real store does both in one transaction and the engine
    depends on that: a recorded flip never has a row left over.
    """

    __slots__ = (
        "active",
        "add_error",
        "next_number",
        "record_error",
        "recorded",
        "remove_error",
        "update_error",
    )

    def __init__(self, *, first_number: int = 42) -> None:
        self.active: dict[str, ActiveFlip] = {}
        self.recorded: list[FinishedFlip] = []
        self.next_number = first_number
        self.record_error: Exception | None = None
        self.add_error: Exception | None = None
        self.update_error: Exception | None = None
        self.remove_error: Exception | None = None

    async def record_flip(self, flip: FinishedFlip, *, token: str | None = None) -> int:
        await asyncio.sleep(0)
        if self.record_error is not None:
            raise self.record_error
        self.recorded.append(flip)
        # One transaction in the real store, so recording a flip and forgetting
        # its row either both happen or neither does.
        if token is not None:
            self.active.pop(token, None)
        number = self.next_number
        self.next_number += 1
        return number

    async def add_active_flip(self, active: ActiveFlip) -> None:
        await asyncio.sleep(0)
        if self.add_error is not None:
            raise self.add_error
        self.active[active.token] = active

    async def update_active_flip(
        self,
        token: str,
        *,
        message_id: int | None = None,
        state: FlipState | None = None,
        seed: bytes | None = None,
        group_id: int | None = None,
    ) -> bool:
        await asyncio.sleep(0)
        if self.update_error is not None:
            raise self.update_error
        current = self.active.get(token)
        if current is None:
            return False
        self.active[token] = replace(
            current,
            message_id=current.message_id if message_id is None else message_id,
            state=current.state if state is None else state,
            seed=current.seed if seed is None else seed,
            group_id=current.group_id if group_id is None else group_id,
        )
        return True

    async def remove_active_flip(self, token: str) -> bool:
        await asyncio.sleep(0)
        if self.remove_error is not None:
            raise self.remove_error
        return self.active.pop(token, None) is not None


@dataclass(slots=True)
class Harness:
    """A registry wired to the three fakes, with the odds and ends tests repeat."""

    clock: FakeClock
    gateway: FakeGateway
    store: FakeStore
    registry: FlipRegistry
    settings: Settings = SETTINGS
    group_id: int = GROUP
    presses: int = field(default=0)

    def press(self) -> str:
        """A fresh press id, so every answer can be told apart."""
        self.presses += 1
        return f"press-{self.presses}"

    async def open_lobby(
        self,
        *,
        joiners: int = 1,
        settings: Settings | FlipRequest | None = None,
        group_id: int | None = None,
        topic_id: int | None = None,
    ) -> Flip:
        """Open a lobby and join ``joiners`` more players after the starter.

        So ``joiners=1`` gives the two players a round needs.

        ``settings`` is what the command asked for, as the registry takes it: a
        :class:`~esgbot.game.rules.FlipRequest` for a flip that left ``Y`` or
        ``Z`` to the bot, or plain :class:`~esgbot.game.rules.Settings` for one
        that gave all four.
        """
        flip = await self.registry.create_flip(
            group_id=self.group_id if group_id is None else group_id,
            topic_id=topic_id,
            starter=player(11),
            settings=self.settings if settings is None else settings,
        )
        for joiner in players(joiners, first_id=12):
            await flip.join(joiner, press_id=self.press())
        return flip

    async def start(self, flip: Flip) -> None:
        """Press Start as the starter and let the deal get going."""
        await flip.start(flip.starter.user_id, press_id=self.press())
        await self.clock.quiesce()

    async def deal(self, flip: Flip) -> None:
        """Start the round and run it up to the moment the peek window opens."""
        await self.start(flip)
        await self.clock.advance(timing.BOARD_GAP_SECONDS)

    async def finish(self, flip: Flip, *, limit: int = 40) -> None:
        """Advance until the flip is over, one waiting deadline at a time.

        Raises:
            AssertionError: if the flip is still going after ``limit`` steps, which
                means it is waiting for something a test has to do itself.
        """
        for _ in range(limit):
            if flip.closed:
                await flip.wait_closed()
                return
            if await self.clock.advance_to_next() == 0.0 and not flip.closed:
                raise AssertionError(f"flip {flip.token} is waiting on nothing")
        raise AssertionError(f"flip {flip.token} did not finish in {limit} steps")


def harness(*, settings: Settings = SETTINGS, group_id: int = GROUP) -> Harness:
    """A fresh clock, gateway, store and registry, wired together."""
    clock = FakeClock()
    return harness_over(FakeGateway(clock), clock=clock, settings=settings, group_id=group_id)


def harness_over(
    gateway: FakeGateway,
    *,
    clock: FakeClock,
    store: FakeStore | None = None,
    settle: Settler = settle_inline,
    settings: Settings = SETTINGS,
    group_id: int = GROUP,
) -> Harness:
    """The same, over a gateway, store or settler the test built itself.

    :func:`harness` is this with the plain fakes. A test that needs output or
    storage to hold or fail at one particular moment builds that part -- usually
    :class:`HoldsAView` or :class:`HoldsAnAnswer` -- and comes here for the rest,
    rather than wiring a registry of its own. ``settle`` is the showdown's own
    work, which a test can count to see how far a round really got.
    """
    flip_store = FakeStore() if store is None else store
    registry = FlipRegistry(gateway=gateway, store=flip_store, clock=clock, settle=settle)
    return Harness(
        clock=clock,
        gateway=gateway,
        store=flip_store,
        registry=registry,
        settings=settings,
        group_id=group_id,
    )

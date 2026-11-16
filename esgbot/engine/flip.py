"""One flip, from lobby to result.

A :class:`Flip` is a small state machine with a few timers around it. It is built
by :class:`~esgbot.engine.registry.FlipRegistry`, which allows one per group,
and it talks to the world through a :class:`~esgbot.engine.gateway.Gateway`, a
:class:`~esgbot.engine.store.FlipStore` and a
:class:`~esgbot.engine.clock.Clock`, so a test can run a whole flip in
milliseconds with three fakes.

**The lobby.** Creating the flip posts the lobby message and writes the row that
says this flip is open. The starter is in from the start and keeps control of the
lobby even if they leave the player list: only their Start and Cancel count, and
Start wants at least :data:`~esgbot.game.rules.MIN_PLAYERS` players. Join stops
at the player cap for this flip's ``X``, repeat joins change nothing, and Leave
works until Start. A lobby that goes
:data:`~esgbot.engine.timing.LOBBY_IDLE_SECONDS` without a join or a leave
cancels itself, and so does one that reaches
:data:`~esgbot.engine.timing.LOBBY_MAX_AGE_SECONDS` old.

**The settings are settled at Start, not before.** A flip is opened with a
:class:`~esgbot.game.rules.FlipRequest`: what the command asked for, which may
leave ``Y`` and ``Z`` to the bot. ``Y`` could be filled in at any time, but
``Z`` cannot -- left out, it is the number of players, and the player list is
only final when Start locks it. So Start resolves the request against that list
and keeps the result on the round, and everything from there on -- the deal, the
showdown, the money, the stored flip, ``/verify`` -- sees ordinary
:class:`~esgbot.game.rules.Settings` with all four values in them. The lobby,
which has nothing settled, shows what was asked for instead.

**The round.** Start locks the player list and draws the seed, and takes the
round on: the lobby timer goes, the half-hour watchdog starts counting from that
instant, and the round's own task exists before the press is answered, so
nothing that happens while Telegram is answering can leave a round nobody owns
or a watchdog that was never installed. The round then publishes, in order after
that answer: the seed to the row, the commitment as its own message, and the
lobby message becomes the table. The seed goes first because the commitment is a
promise to reveal it, and after a restart the row is the only copy; a seed that
cannot be stored voids the round before a single card is shown. Then: Board 1,
two seconds, Board 2, the extra cards if the deck has enough for a full round,
and the peek window opens. The window ends
:data:`~esgbot.engine.timing.PEEK_WINDOW_SECONDS` after the last card, or
:data:`~esgbot.engine.timing.PEEK_ALL_SECONDS` after the last player peeked at
their final hand, whichever comes first. Then the showdown runs the evaluator in
a worker thread and reveals itself in five steps, the flip is recorded, and the
result goes out as a new message.

**Peek works from the commitment.** A player can see their own cards once the
commitment has gone out, and not before -- the cards exist from Start, but a
hand shown before the group has been told what deck it came from would be a hand
nobody could check. A Peek pressed earlier is answered as expired. A peek counts
towards "everyone has peeked" only when the hand it showed was final and the
pop-up carrying it actually went out.

**A pop-up also says what the hand has made, and only against boards the group
can see.** Every card is fixed at Start, so a hand read against a board still to
come would say something about cards nobody has been shown; a board is read only
once the table edit carrying it has come back. The reading itself runs in a
worker thread, outside both locks -- the cards and the boards are snapshotted
under the state lock, and the lock is let go before the work starts -- so a Peek
is answered promptly however slow Telegram is, and several at once do not queue
behind each other. See :meth:`Flip.peek` and :mod:`esgbot.engine.peek`.

**Two locks, neither held while waiting.** The state lock is taken for the moment
it takes to change something and never across a gateway call or a sleep, so a
Peek press is answered at once however slow Telegram is being. The output lock
keeps this flip's posts and edits in order, and button presses are answered
before any message that follows them. Nothing else in the engine takes a lock.
Because a press is answered before the message it leads to, and the state may
have moved on by then, a view that describes one phase is only shown while the
flip is still in it: a Join answered slowly cannot put an open lobby back over a
cancelled one, or over the table.

**One ending, decided once, told once.** A flip ends either finished -- recorded
or not -- or void, and that outcome is settled in one place and announced by one
caller. :meth:`Flip.void` is where every ending but the round's own finish
arrives: the watchdog, a send the gateway has given up on, a round that broke, a
round whose seed would not store. It stops the round and waits for it *before*
it decides anything, which is also how it learns whether money was recorded: a
commit already in flight cannot be called back, so if the write got through, the
flip is finished rather than void and the result is told instead of a void
notice. Nothing can commit behind the decision, because by the time it is taken
the round is over; and no round step may move a flip out of a terminal phase or
show anything for one, so a round that is overtaken in the moment in between
stops where it is and says nothing.

**Finishing is one decision, and it is storage's.** The write that records the
flip also retires its in-progress row, in one transaction, and it is seen
through even if the round is cancelled while it is in flight, because a worker
thread in sqlite cannot be called back. So a flip is finished the moment that
write succeeds -- it has a number and no row -- and a write that fails is final
the other way: nothing was recorded, and the result is posted anyway saying so.

**The announcement belongs to the flip, not to whoever decided it.** Saying how
a flip ended is one task, started by the decision itself -- the write coming
back, or a void being settled -- and every ending caller after that joins it
instead of starting another. Nothing cancels it: not the round that is waiting
for it, not a watchdog being stopped, not a second void, not the bot shutting
down. Only its own deadline does, after
:data:`~esgbot.engine.timing.TELLING_SECONDS`, which is what keeps a group from
being held by a message Telegram will not take. That is also why stopping the
bot waits: see :meth:`~esgbot.engine.registry.FlipRegistry.shut_down`, which is
the last moment anything can send a result that has just been recorded.

**The row is the last line of recovery, so it goes only once something has been
said.** Until the result or the void notice is out, that row holds the only
durable copy of the seed; it is cleared after the group has been told, and left
behind when the telling failed, so the next start can reveal the seed instead.
The one exception is a flip voided because sending has stopped working: it has
nothing to say and nothing to wait for, so its row goes.

**When it cannot finish.** A flip still running
:data:`~esgbot.engine.timing.FLIP_WATCHDOG_SECONDS` after Start is voided, as is
one whose gateway raises :class:`~esgbot.engine.gateway.PermanentFailureError`,
or that breaks in some way nobody foresaw. Voiding records nothing and reveals
the seed, so the group can still check what the deal would have been. The
watchdog is also the bound on everything else: a result post that will not
return is cancelled there, which closes the flip and frees the group, and the
flip stays finished -- a committed flip is never announced as voided, so the
group keeps a result it can verify even if the message itself never arrived.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from esgbot.engine import timing
from esgbot.engine.clock import Clock
from esgbot.engine.gateway import Gateway, MessageTarget, PermanentFailureError
from esgbot.engine.peek import HandReader, read_hand_in_thread
from esgbot.engine.store import FlipStore
from esgbot.engine.views import (
    BoardView,
    CommitmentView,
    FlipView,
    HandView,
    LobbyStatus,
    LobbyView,
    MadeHand,
    Notice,
    PeekAnswer,
    Player,
    PressAnswer,
    ResultView,
    SeatView,
    ShowdownStep,
    ShowdownView,
    TableStage,
    TableView,
    VoidReason,
    VoidView,
)
from esgbot.game import fairness
from esgbot.game.cards import Card
from esgbot.game.deal import Deal, deal_cards
from esgbot.game.rules import MIN_PLAYERS, FlipRequest, Settings
from esgbot.game.scoring import Board
from esgbot.game.settlement import FlipOutcome, settle_deal
from esgbot.logging_setup import get_logger
from esgbot.storage import ActiveFlip, FinishedFlip, FlipPlayer, FlipState, StorageError
from esgbot.tasks import see_through

__all__ = [
    "TOKEN_BYTES",
    "Flip",
    "FlipPhase",
    "Settler",
    "new_token",
    "settle_in_thread",
]

_logger = get_logger(__name__)

TOKEN_BYTES: Final = 8
"""Random bytes behind a flip's internal token, which button data carries."""


def new_token() -> str:
    """A fresh internal token: short, random and safe in callback data.

    It identifies the flip in button data and in its active-flip row. The public
    flip number is a different thing entirely and only exists once the flip is
    saved.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


Settler = Callable[[Deal, Settings], Awaitable[FlipOutcome]]
"""How a flip works out its result. :func:`settle_in_thread` is the real one."""


async def settle_in_thread(deal: Deal, settings: Settings) -> FlipOutcome:
    """Score and settle a deal in a worker thread.

    The evaluator is the one expensive thing in a flip -- 20,349 five-card hands
    per player in the worst case -- so it runs off the event loop and Telegram
    polling and other groups' timers carry on meanwhile. It still shares the
    interpreter lock, so this buys responsiveness rather than parallelism.
    """
    return await asyncio.to_thread(settle_deal, deal, settings)


class FlipPhase(StrEnum):
    """Where a flip has got to. The last three are final."""

    LOBBY = "lobby"
    DEALING = "dealing"
    SHOWDOWN = "showdown"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    VOIDED = "voided"

    @property
    def started(self) -> bool:
        """Whether the cards have been dealt, and so there is a seed to reveal."""
        return self in (FlipPhase.DEALING, FlipPhase.SHOWDOWN)

    @property
    def terminal(self) -> bool:
        """Whether the flip is over, one way or another."""
        return self in (FlipPhase.FINISHED, FlipPhase.CANCELLED, FlipPhase.VOIDED)


_LOBBY_PHASES: Final[dict[LobbyStatus, FlipPhase]] = {
    LobbyStatus.OPEN: FlipPhase.LOBBY,
    LobbyStatus.CANCELLED: FlipPhase.CANCELLED,
    LobbyStatus.TIMED_OUT: FlipPhase.CANCELLED,
    LobbyStatus.VOIDED: FlipPhase.VOIDED,
}
"""The phase each lobby message is about, so a view that has gone stale is dropped.

A lobby is only shown as open while the flip is open, as cancelled once it has
been cancelled, and so on. Everything that shows a lobby does so after awaiting
a press answer, by which time another press may have started or cancelled the
flip, and a message is a worse record than none at all once it disagrees.
"""


class _SeedNotStoredError(Exception):
    """The seed could not be put on the flip's row, so the round cannot be published.

    Internal to this module. The round raises it before the commitment goes out
    and is voided with nothing shown, which is the one case where a failed
    ``update_active_flip`` costs a flip: see :meth:`Flip._save_seed`.
    """


class _FlipOverError(Exception):
    """The round found the flip already decided, so it must stop where it is.

    Internal to this module. A flip has one ending, and whoever decided it owns
    telling the group; a round that has been overtaken says nothing and records
    nothing. :meth:`Flip.void` stops the round before deciding, so this is the
    backstop for the moment in between rather than the usual way a round ends.
    """


@dataclass(frozen=True, slots=True)
class _Ending:
    """How a flip ended, and what the group has to be told about it.

    Fixed the moment the ending is decided, because what the message says
    depends on where the flip had got to -- whether the table was ever drawn,
    which stage it was showing, whether there is a seed to reveal -- and none of
    that is still true by the time the message goes out. ``reason`` is ``None``
    for a flip that finished, which says its result instead.
    """

    reason: VoidReason | None
    was: FlipPhase
    table_live: bool
    stage: TableStage


@dataclass(frozen=True, slots=True)
class _Round:
    """What Start fixes and nothing afterwards changes.

    ``seated`` is the locked player list, where seat ``i`` is ``seated[i]``, which
    is the same order the deal uses. ``started_monotonic`` is Start itself, which
    is where the watchdog's half hour runs from however long the press takes to
    answer or the round takes to get going.

    ``settings`` belongs here because this is where the flip's settings become
    all four numbers: a ``Z`` the command left out is the number of players, and
    the player list is only final at Start. Everything after this point -- the
    deal, the showdown, the money, the stored flip -- works from these, so
    nothing downstream has to know what the command left out.
    """

    seed: bytes
    commitment: str
    deal: Deal
    seated: tuple[Player, ...]
    settings: Settings
    started_at: datetime
    started_monotonic: float


class Flip:
    """One flip. Create it with :meth:`~esgbot.engine.registry.FlipRegistry.create_flip`.

    Every button press comes in through :meth:`join`, :meth:`leave`,
    :meth:`start`, :meth:`cancel` or :meth:`peek`. Each answers the press with a
    :class:`~esgbot.engine.views.Notice` (or, for a peek, that player's cards),
    returns the same answer to its caller, and then posts or edits whatever the
    press changed. A press that changes nothing sends no message.
    """

    __slots__ = (
        "_activity",
        "_boards_shown",
        "_cards_out",
        "_clock",
        "_closed",
        "_created_at",
        "_created_monotonic",
        "_deciding",
        "_ending",
        "_flip_number",
        "_gateway",
        "_group_id",
        "_hands_final",
        "_last_activity",
        "_last_count_edit",
        "_last_peek_at",
        "_message_id",
        "_on_close",
        "_outcome",
        "_output_lock",
        "_peek_count_shown",
        "_peeked",
        "_phase",
        "_players",
        "_read_hand",
        "_request",
        "_round",
        "_run_task",
        "_settle",
        "_stage",
        "_start_answered",
        "_starter",
        "_state_lock",
        "_store",
        "_table_live",
        "_telling",
        "_timer_task",
        "_token",
        "_told",
        "_topic_id",
        "_wake",
        "_window_start",
    )

    def __init__(
        self,
        *,
        group_id: int,
        topic_id: int | None,
        settings: Settings | FlipRequest,
        starter: Player,
        gateway: Gateway,
        store: FlipStore,
        clock: Clock,
        token: str | None = None,
        settle: Settler = settle_in_thread,
        read_hand: HandReader = read_hand_in_thread,
        on_close: Callable[[Flip], None] | None = None,
    ) -> None:
        """Set a flip up without touching the gateway or the store.

        The registry calls this and then opens the flip, which is what posts the
        lobby and writes its row. ``settle``, ``read_hand`` and ``token`` are
        seams for tests; the defaults are what the bot runs, and both of the
        first two put their work in a worker thread.

        ``settings`` is what the command asked for: a
        :class:`~esgbot.game.rules.FlipRequest` when it left ``Y`` or ``Z`` to
        the bot, or plain :class:`~esgbot.game.rules.Settings`, which is the same
        thing with nothing left out. Either way the flip keeps it as a request
        and resolves it at Start.
        """
        self._token = token if token is not None else new_token()
        self._group_id = group_id
        self._topic_id = topic_id
        self._request = (
            settings if isinstance(settings, FlipRequest) else FlipRequest.from_settings(settings)
        )
        self._starter = starter
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._settle = settle
        self._read_hand = read_hand
        self._on_close = on_close

        self._phase = FlipPhase.LOBBY
        self._players: list[Player] = [starter]
        self._message_id = 0
        self._created_at = clock.now()
        self._created_monotonic = clock.monotonic()
        self._last_activity = self._created_monotonic
        self._round: _Round | None = None
        self._outcome: FlipOutcome | None = None
        self._flip_number: int | None = None
        self._stage = TableStage.HOLE_CARDS

        self._cards_out = False
        self._boards_shown = 0
        self._table_live = False
        self._told = False
        self._ending: _Ending | None = None
        self._hands_final = False
        self._window_start = 0.0
        self._last_peek_at = 0.0
        self._peeked: set[int] = set()
        self._peek_count_shown = 0
        self._last_count_edit = 0.0

        self._state_lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._activity = asyncio.Event()
        self._wake = asyncio.Event()
        self._closed = asyncio.Event()
        self._start_answered = asyncio.Event()
        self._timer_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._telling: asyncio.Task[None] | None = None
        self._deciding: asyncio.Task[int | None] | None = None

    def __repr__(self) -> str:
        return (
            f"<Flip {self._token} group={self._group_id} "
            f"phase={self._phase.value} players={len(self._players)}>"
        )

    # ---------------------------------------------------------------- identity

    @property
    def token(self) -> str:
        """The flip's internal token: button data, and the key of its row."""
        return self._token

    @property
    def group_id(self) -> int:
        """The group this flip belongs to, which is a different id after a migration."""
        return self._group_id

    @property
    def topic_id(self) -> int | None:
        """The forum topic every message of this flip goes to, if the group has topics."""
        return self._topic_id

    @property
    def target(self) -> MessageTarget:
        """Where this flip's messages go."""
        return MessageTarget(group_id=self._group_id, topic_id=self._topic_id)

    @property
    def request(self) -> FlipRequest:
        """What the command asked for, fixed when the flip was created.

        This is the flip as it was typed, so ``/again`` can ask for the same
        again: a ``Y`` or ``Z`` the command left out is left out here too.
        """
        return self._request

    @property
    def settings(self) -> Settings | None:
        """The four settings this flip is playing, or ``None`` before Start.

        Start is where they become four numbers, because a ``Z`` left out of the
        command is the number of players and the player list is not final until
        then. :attr:`request` is what the flip was asked to be before that.
        """
        round_ = self._round
        return None if round_ is None else round_.settings

    @property
    def starter(self) -> Player:
        """Whoever created the flip. They keep control of the lobby either way."""
        return self._starter

    @property
    def created_at(self) -> datetime:
        """When the flip was created, in UTC."""
        return self._created_at

    # ------------------------------------------------------------------- state

    @property
    def phase(self) -> FlipPhase:
        """Where the flip has got to."""
        return self._phase

    @property
    def message_id(self) -> int:
        """The message that is the lobby, and later the table. Zero before it is posted."""
        return self._message_id

    @property
    def players(self) -> tuple[Player, ...]:
        """The players, in join order. Seats follow this order once Start locks it."""
        return tuple(self._players)

    @property
    def player_count(self) -> int:
        """How many players are in."""
        return len(self._players)

    @property
    def flip_number(self) -> int | None:
        """The public number storage gave the flip, or ``None`` if it was never saved."""
        return self._flip_number

    @property
    def closed(self) -> bool:
        """Whether the flip's outcome is decided: finished, cancelled or voided.

        Decided is not the same as done: a flip that has just been recorded is
        finished, and still has its result to send. :meth:`wait_closed` is the
        one that waits for that.
        """
        return self._phase.terminal

    async def wait_closed(self) -> None:
        """Wait until the flip is over and everything it had to say has been said.

        Which is the end of its announcement, successful or given up on, and the
        moment it lets go of its group.
        """
        await self._closed.wait()

    def stop(self) -> None:
        """Drop the flip's round and timers, for shutting the bot down.

        The active-flip row stays behind on purpose: the next start finds it and
        tells the group the flip was voided. What this does not stop is an
        announcement: a flip whose result was recorded as the bot was stopping
        has no row for the next start to find, so that one message is the only
        thing that can tell its group, and it is left to finish. Pair this with
        :meth:`tell_outcome` to wait for it, as
        :meth:`~esgbot.engine.registry.FlipRegistry.shut_down` does.
        """
        self._cancel_pending()

    async def _follow_group(self, group_id: int) -> None:
        """Move the flip to the id Telegram now gives its group, its row with it.

        Only :meth:`~esgbot.engine.registry.FlipRegistry.migrate_group` calls
        this, because the registry keeps one flip per group and both have to
        agree on which group that is. The move itself happens before the first
        await, so nothing can find the registry and the flip disagreeing.

        :attr:`target` is read afresh for every message, so the next post or edit
        goes to the new group; one already in flight has gone to the old id and
        cannot be called back. The topic is left alone: it belongs to the group,
        not to the id it is known by.

        The flip's own row is moved here, by token, rather than left to the bulk
        rewrite that moves the group's history: that rewrite can fail, and a row
        left on the old id would send this flip's void notice to the dead chat
        after a restart. Either write moving the row is enough, and like every
        other row update this one is best effort -- a storage failure costs a
        void notice in the wrong place at worst, never the flip.
        """
        self._group_id = group_id
        try:
            await self._store.update_active_flip(self._token, group_id=group_id)
        except (StorageError, sqlite3.Error) as error:
            _logger.error("flip %s: could not store its new group id: %s", self._token, error)

    # ------------------------------------------------------------ button presses

    async def join(self, player: Player, *, press_id: str | None = None) -> Notice:
        """Add a player to the lobby.

        Refused with :attr:`~esgbot.engine.views.Notice.LOBBY_FULL` at the player
        cap, :attr:`~esgbot.engine.views.Notice.ALREADY_JOINED` for somebody
        already in, and once the round has started it is too late.
        """
        async with self._state_lock:
            notice = self._add_player(player)
        await self._answer(press_id, notice)
        if notice is Notice.JOINED:
            await self._show_lobby()
        return notice

    async def leave(self, user_id: int, *, press_id: str | None = None) -> Notice:
        """Take a player off the lobby list, which works until Start.

        The starter may leave as well, and still runs the lobby.
        """
        async with self._state_lock:
            notice = self._remove_player(user_id)
        await self._answer(press_id, notice)
        if notice is Notice.LEFT:
            await self._show_lobby()
        return notice

    async def cancel(self, user_id: int, *, press_id: str | None = None) -> Notice:
        """Cancel the lobby. Only the starter can, and only before Start.

        Nothing is recorded and the flip's row is cleared.
        """
        async with self._state_lock:
            notice = self._close_lobby(user_id)
        await self._answer(press_id, notice)
        if notice is Notice.CANCELLED:
            _logger.info("flip %s cancelled by its starter", self._token)
            await self._drop_active_row()
            await self._show_lobby(LobbyStatus.CANCELLED)
            self._close()
        return notice

    async def start(self, user_id: int, *, press_id: str | None = None) -> Notice:
        """Start the round: lock the players, draw the seed and deal.

        Only the starter can, and only with at least
        :data:`~esgbot.game.rules.MIN_PLAYERS` players.

        The round is the flip's before the press is answered -- its task and its
        watchdog both exist, and the half hour runs from here -- so a slow or
        cancelled answer cannot move the deadline, and :meth:`stop` in the gap
        stops a round that is already there rather than racing one that is about
        to be created. What the round has to say still waits for the answer:
        nothing is published until the press has been dealt with, success or
        failure, and then it carries on without this call.
        """
        async with self._state_lock:
            notice = self._begin_round(user_id)
            if notice is Notice.STARTED:
                self._own_round()
        try:
            await self._answer(press_id, notice)
        finally:
            if notice is Notice.STARTED:
                # The deal is fixed and the round is running, so it publishes
                # whatever became of the answer -- even if this call was
                # cancelled on the way through it.
                self._start_answered.set()
        return notice

    async def peek(self, user_id: int, *, press_id: str | None = None) -> PressAnswer:
        """Show one player their own cards and what they have made, and count the peek.

        Returns that player's :class:`~esgbot.engine.views.PeekAnswer`, or
        :attr:`~esgbot.engine.views.Notice.NOT_A_PLAYER` for somebody who is not
        in this flip. Before the extra cards are out the answer holds the hole
        cards alone and does not count towards "everyone has peeked"; a player
        whose hand is still to grow has to look again afterwards. Before the
        commitment has been posted there are no cards to show yet and the answer
        is :attr:`~esgbot.engine.views.Notice.EXPIRED`.

        A final hand counts only once the pop-up holding it has gone out. A
        pop-up that failed showed nobody anything, so counting it would let the
        ten-second rule end the window on hands no player has seen. What the
        answer *says* makes no difference to that: the hand it carries is final
        or it is not, and the reading below cannot change which.

        **Three things happen in this order, and only the first holds a lock.**
        The cards the player holds now and the boards the table has already shown
        are taken under the state lock, which is let go at once. The reading is
        then worked out in a worker thread -- tens of milliseconds for a 21-card
        hand -- with nothing held, so the round's timers keep their times, this
        flip's output is unblocked and another player's Peek is answered
        meanwhile rather than queued behind this one. Only then is the press
        answered. A flip that ends in between is no obstacle: the hand was that
        player's when they pressed, and the boards read against are on the table
        either way, so the pop-up still goes out and whoever ended the flip still
        owns what the group is told.
        """
        async with self._state_lock:
            answer, final = self._peek_answer(user_id)
            boards = self._boards_out() if isinstance(answer, PeekAnswer) else ()
        if isinstance(answer, PeekAnswer):
            answer = replace(answer, made=await self._made_hands(answer.cards, boards))
        shown = await self._answer(press_id, answer)
        if final and shown:
            async with self._state_lock:
                self._count_peek(user_id)
        return answer

    async def _made_hands(
        self, cards: Sequence[Card], boards: Sequence[Sequence[Card]]
    ) -> tuple[MadeHand, ...]:
        """What a hand has made, worked out off the event loop and outside both locks.

        A reading that breaks costs the player those lines and not their cards:
        the pop-up goes out with the hand alone, because a press nobody answers
        leaves a spinner going on somebody's phone, and a Peek is the one thing
        in a flip that no ending depends on. A cancellation -- the bot stopping
        while a press is part way through -- is not a failure and is left to
        carry on up.
        """
        try:
            return await self._read_hand(cards, boards)
        except Exception:
            _logger.exception(
                "flip %s: a hand could not be read; showing the cards alone", self._token
            )
            return ()

    # ------------------------------------------------------------ state changes

    def _add_player(self, player: Player) -> Notice:
        """Join, under the state lock."""
        if self._phase is not FlipPhase.LOBBY:
            return self._lobby_gone()
        if any(seated.user_id == player.user_id for seated in self._players):
            return Notice.ALREADY_JOINED
        if len(self._players) >= self._request.max_players:
            return Notice.LOBBY_FULL
        self._players.append(player)
        self._note_activity()
        return Notice.JOINED

    def _remove_player(self, user_id: int) -> Notice:
        """Leave, under the state lock."""
        if self._phase is not FlipPhase.LOBBY:
            return self._lobby_gone()
        for index, seated in enumerate(self._players):
            if seated.user_id == user_id:
                del self._players[index]
                self._note_activity()
                return Notice.LEFT
        return Notice.NOT_IN_LOBBY

    def _close_lobby(self, user_id: int) -> Notice:
        """Cancel, under the state lock."""
        if self._phase is not FlipPhase.LOBBY:
            return self._lobby_gone()
        if user_id != self._starter.user_id:
            return Notice.NOT_THE_STARTER
        self._enter(FlipPhase.CANCELLED)
        return Notice.CANCELLED

    def _begin_round(self, user_id: int) -> Notice:
        """Start, under the state lock: everything that must happen in one go.

        The player list, the settings, the seed, the button and every card are
        fixed here, so nothing a later press does can change the deal -- or the
        money, which is the other thing the player list decides: a ``Z`` the
        command left out becomes the number of players, and this is the one
        moment at which that number is final.
        """
        if self._phase is not FlipPhase.LOBBY:
            return self._lobby_gone()
        if user_id != self._starter.user_id:
            return Notice.NOT_THE_STARTER
        if len(self._players) < MIN_PLAYERS:
            return Notice.NEED_MORE_PLAYERS
        seated = tuple(self._players)
        settings = self._request.resolve(len(seated))
        seed = fairness.generate_seed()
        deal = deal_cards(
            seed=seed,
            player_ids=[player.user_id for player in seated],
            x=settings.x,
            y=settings.y,
        )
        self._round = _Round(
            seed=seed,
            commitment=fairness.commitment(seed),
            deal=deal,
            seated=seated,
            settings=settings,
            started_at=self._clock.now(),
            started_monotonic=self._clock.monotonic(),
        )
        self._enter(FlipPhase.DEALING)
        self._stage = TableStage.HOLE_CARDS
        # With no extra cards to come, every hand is final as soon as it is dealt,
        # so a peek during the board reveals already counts.
        self._hands_final = deal.extra_count == 0
        _logger.info(
            "flip %s started in group %d: %d players, X=%d Y=%d Z=%d extra=%d",
            self._token,
            self._group_id,
            len(seated),
            settings.x,
            settings.y,
            settings.z,
            deal.extra_count,
        )
        return Notice.STARTED

    def _own_round(self) -> None:
        """Take the round on, under the state lock, the instant Start is allowed.

        The lobby timer goes and both of the round's tasks are created here, with
        no await in between, so there is no moment at which the flip has started
        and nothing is watching it: a Start handler that is cancelled, a
        shutdown, or a press arriving in the gap all meet a round that is
        already owned, and nothing creates one afterwards.
        """
        self._cancel_pending()
        self._run_task = asyncio.create_task(self._run(), name=f"flip-{self._token}-run")
        self._timer_task = asyncio.create_task(
            self._watchdog(), name=f"flip-{self._token}-watchdog"
        )

    def _peek_answer(self, user_id: int) -> tuple[PressAnswer, bool]:
        """Peek, under the state lock: the answer, and whether its hand is final.

        Nothing is counted here. Whether this peek counts depends on the answer
        reaching the player, which is :meth:`peek`'s business, and on the hand in
        *this* answer being the final one -- not on whether the hand has become
        final since.
        """
        if self._phase is not FlipPhase.DEALING or not self._cards_out:
            return Notice.EXPIRED, False
        deal = self._require_round().deal
        try:
            seat = deal.seat_of(user_id)
        except KeyError:
            return Notice.NOT_A_PLAYER, False
        hand = deal.hand_for_seat(seat)
        final = self._hands_final
        answer = PeekAnswer(seat=seat, hole=hand.hole, extra=hand.extra if final else ())
        return answer, final

    def _boards_out(self) -> tuple[tuple[Card, ...], ...]:
        """The community boards the table has already shown, Board 1 first. Under the lock.

        These are the only boards a hand may be read against. Every card is
        fixed at Start, so a reading taken against a board the group has not seen
        would say something about cards nobody has been shown -- and a board
        counts as shown once the table edit carrying it has come back, not when
        the round set out to make it.
        """
        return self._require_round().deal.boards[: self._boards_shown]

    def _count_peek(self, user_id: int) -> None:
        """Write down that this player has seen their final hand, under the state lock.

        The ten-second rule runs from here rather than from the press, because
        this is when the player actually saw the hand.
        """
        if user_id in self._peeked:
            return
        self._peeked.add(user_id)
        self._last_peek_at = self._clock.monotonic()
        self._wake.set()

    def _enter(self, phase: FlipPhase) -> bool:
        """Move the flip to ``phase``, unless it is already over. Under the state lock.

        Every phase change goes through here, so a flip has one ending and
        nothing can move it out of one: a round step that finds this refusing has
        been overtaken, and stops.
        """
        if self._phase.terminal:
            return False
        self._phase = phase
        return True

    def _lobby_gone(self) -> Notice:
        """The answer to a lobby button pressed too late."""
        return Notice.EXPIRED if self._phase.terminal else Notice.ALREADY_STARTED

    def _note_activity(self) -> None:
        """Push the idle timeout out and wake the lobby timer to recompute it."""
        self._last_activity = self._clock.monotonic()
        self._activity.set()

    # ------------------------------------------------------------------ opening

    async def _open(self) -> None:
        """Post the lobby and write the flip's row. The registry calls this once.

        Raises:
            PermanentFailureError: if the lobby cannot be posted.
            StorageError, sqlite3.Error: if the row cannot be written. The lobby
                that was already posted is closed again, so nothing is left in
                the group, and the flip does not exist.
        """
        self._message_id = await self._gateway.post(self.target, self._lobby_view())
        try:
            await self._store.add_active_flip(
                ActiveFlip(
                    token=self._token,
                    group_id=self._group_id,
                    topic_id=self._topic_id,
                    message_id=self._message_id,
                    state=FlipState.LOBBY,
                    seed=None,
                    created_at=self._created_at,
                )
            )
        except (StorageError, sqlite3.Error):
            await self._abandon_unopened()
            raise
        self._timer_task = asyncio.create_task(
            self._lobby_timer(), name=f"flip-{self._token}-lobby"
        )
        _logger.info(
            "flip %s opened in group %d by %d", self._token, self._group_id, self._starter.user_id
        )

    async def _abandon_unopened(self) -> None:
        """Close the lobby of a flip whose row could not be written.

        The flip does not exist: there is no row, and the registry is about to
        give its place back. But the lobby message is already in the group, so
        without this the group keeps a lobby whose buttons answer "expired" for
        ever -- and with no row, not even the next start's sweep would find it.
        The message is edited to say the flip was voided, which takes its buttons
        away, and nothing is posted beyond that.
        """
        self._enter(FlipPhase.VOIDED)
        try:
            await self._edit(self._lobby_view(LobbyStatus.VOIDED))
        except PermanentFailureError:
            _logger.warning("flip %s: its abandoned lobby could not be closed", self._token)
        _logger.warning("flip %s could not be opened; its lobby was closed again", self._token)
        self._close()

    # ------------------------------------------------------------------- timers

    async def _lobby_timer(self) -> None:
        """Cancel the lobby once it has been idle too long, or has lived long enough."""
        oldest = self._created_monotonic + timing.LOBBY_MAX_AGE_SECONDS
        while True:
            self._activity.clear()
            now = self._clock.monotonic()
            deadline = min(oldest, self._last_activity + timing.LOBBY_IDLE_SECONDS)
            if now >= deadline:
                await self._timeout()
                return
            await self._sleep(deadline - now, self._activity)

    async def _timeout(self) -> None:
        """The lobby ran out of time: cancel it and say so on the lobby message."""
        async with self._state_lock:
            if self._phase is not FlipPhase.LOBBY:
                return
            self._enter(FlipPhase.CANCELLED)
        _logger.info("flip %s timed out in the lobby", self._token)
        await self._drop_active_row()
        await self._show_lobby(LobbyStatus.TIMED_OUT)
        self._close()

    async def _watchdog(self) -> None:
        """Void a round that is still going half an hour after Start.

        The deadline is Start itself, not the moment this task first runs, so
        neither a slow acknowledgement nor a busy event loop can move it.

        What happens then is :meth:`void`'s business, which is where every
        ending is decided: it stops the round, and if the round had got its
        result recorded by then, the flip is finished rather than void and that
        result is told rather than a void notice. So this is also the bound on a
        result post that will not return -- the flip closes and frees its group,
        and stays finished.
        """
        round_ = self._require_round()
        deadline = round_.started_monotonic + timing.FLIP_WATCHDOG_SECONDS
        await self._clock.sleep(deadline - self._clock.monotonic())
        if self._closed.is_set():
            return
        _logger.warning(
            "flip %s is still unfinished %.0f seconds after Start",
            self._token,
            timing.FLIP_WATCHDOG_SECONDS,
        )
        await self.void(VoidReason.TIMED_OUT)

    # ----------------------------------------------------------------- the round

    async def _run(self) -> None:
        """The whole round, on one task: deal, wait, show, record, post."""
        try:
            # Ordered after the Start press's answer, so nothing this round says
            # reaches the group before the player who pressed hears back.
            await self._start_answered.wait()
            await self._deal_out()
            await self._peek_window()
            await self._showdown()
            await self._finish()
        except _FlipOverError as over:
            # The flip was decided while this round was running: whoever decided
            # it owns what the group is told, so there is nothing to do here.
            _logger.info("flip %s: its round stopped because %s", self._token, over)
        except _SeedNotStoredError as error:
            _logger.error("flip %s cannot promise its seed: %s; voiding", self._token, error)
            await self.void(VoidReason.ERROR)
        except PermanentFailureError:
            _logger.warning("flip %s: the gateway gave up; voiding quietly", self._token)
            await self.void(VoidReason.SEND_FAILED)
        except Exception:
            _logger.exception("flip %s broke mid-round; voiding", self._token)
            await self.void(VoidReason.ERROR)

    async def _deal_out(self) -> None:
        """Seed, commitment, table, Board 1, two seconds, Board 2, extra cards, window open."""
        round_ = self._require_round()
        await self._save_seed(round_.seed)
        async with self._state_lock:
            # From here the round's messages are queued in order behind the
            # output lock, so the lobby message is on its way to being the table
            # however slow Telegram is, and that is what a void has to close.
            self._table_live = True
        await self._post(CommitmentView(token=self._token, commitment=round_.commitment))
        async with self._state_lock:
            # The seed is stored and the group has been told which deck this is,
            # so the hands are now something a player can be shown and check.
            self._cards_out = True
        await self._show_table(TableStage.HOLE_CARDS)
        await self._show_table(TableStage.BOARD1)
        async with self._state_lock:
            # Board 1 is on the table, so a peek may now be told what the player
            # has made on it. Not a moment earlier: see _boards_out.
            self._boards_shown = 1
        await self._clock.sleep(timing.BOARD_GAP_SECONDS)
        await self._show_table(TableStage.BOARD2)
        async with self._state_lock:
            self._boards_shown = 2
            # The extra cards go out now, which is the last card dealt and the
            # moment every hand is final, so both peek deadlines run from here.
            self._hands_final = True
            self._window_start = self._clock.monotonic()
            self._peek_count_shown = len(self._peeked)
        await self._show_table(TableStage.PEEKING, peeked=self._peek_count_shown)
        async with self._state_lock:
            # The throttle on count edits runs from the edit that got through.
            self._last_count_edit = self._clock.monotonic()

    async def _peek_window(self) -> None:
        """Wait out the peek window, updating the peek count at most once every five seconds."""
        latest = self._window_start + timing.PEEK_WINDOW_SECONDS
        while True:
            self._wake.clear()
            now = self._clock.monotonic()
            deadline = min(latest, self._everyone_peeked_deadline())
            if now >= deadline:
                return
            if self._count_edit_due(now):
                # The count as it stands, not the one that fell due: peeks that
                # arrived while an earlier edit was in flight are shown by this
                # one rather than queued behind it. And the five seconds are
                # counted from the edit that got through, so an edit Telegram
                # made the bot wait for is not followed straight away by another.
                shown = len(self._peeked)
                await self._show_table(TableStage.PEEKING, peeked=shown)
                async with self._state_lock:
                    self._peek_count_shown = shown
                    self._last_count_edit = self._clock.monotonic()
                continue
            due = self._count_edit_at()
            await self._sleep((deadline if due is None else min(deadline, due)) - now, self._wake)

    def _everyone_peeked_deadline(self) -> float:
        """When the ten-second rule ends the window, or infinity while anyone is missing.

        The ten seconds run from the window opening or from the last peek that
        was needed, whichever came later, which is why a table that peeked early
        still gets the full ten.
        """
        if len(self._peeked) < self._require_round().deal.player_count:
            return math.inf
        return max(self._window_start, self._last_peek_at) + timing.PEEK_ALL_SECONDS

    def _count_edit_due(self, now: float) -> bool:
        """Whether the peek count has moved on and the throttle has run out."""
        return (
            len(self._peeked) != self._peek_count_shown
            and now - self._last_count_edit >= timing.PEEK_EDIT_INTERVAL_SECONDS
        )

    def _count_edit_at(self) -> float | None:
        """When the next peek-count edit may go out, or ``None`` if none is waiting."""
        if len(self._peeked) == self._peek_count_shown:
            return None
        return self._last_count_edit + timing.PEEK_EDIT_INTERVAL_SECONDS

    async def _showdown(self) -> None:
        """Work out the result off the event loop, then reveal it in five steps.

        Raises:
            _FlipOverError: if the flip was decided while the result was being
                worked out. The hands stay private and nothing is recorded: a
                flip that has been called off does not get a showdown.
        """
        round_ = self._require_round()
        outcome = await self._settle(round_.deal, round_.settings)
        async with self._state_lock:
            self._outcome = outcome
            if not self._enter(FlipPhase.SHOWDOWN):
                raise _FlipOverError(f"it was {self._phase.value} before its showdown")
        for step in ShowdownStep:
            if step is not ShowdownStep.HANDS:
                await self._clock.sleep(timing.SHOWDOWN_STEP_SECONDS)
            await self._edit(self._showdown_view(step), only_while=FlipPhase.SHOWDOWN)

    async def _finish(self) -> None:
        """Commit the flip's result and tell the group: one decision, one message.

        Raises:
            _FlipOverError: if the flip was decided before the write began.
        """
        await self._commit(self._clock.now())
        # The announcement is the flip's own, and it is what lets go of the
        # group, so being stopped while waiting here costs the group nothing.
        await self.tell_outcome()

    async def _commit(self, finished_at: datetime) -> int | None:
        """Record the flip, retire its row, and settle its phase. Returns its number.

        One storage call does the recording and the retiring, so "recorded" and
        "no longer in progress" are one fact: there is no window in which a
        recorded flip still has a row for the next start to announce as voided.

        The call is seen through even if the round is cancelled while it is in
        flight, because it cannot be called back -- a worker thread already in
        sqlite will commit or not whatever this task does -- so waiting is the
        only way to know which happened. The flip is finished either way before
        this returns: with a number if the write got through, without one if it
        did not. It is also kept where anybody else can wait for it, because
        until it comes back nobody can say how this flip ended -- a shutdown
        least of all.

        Raises:
            _FlipOverError: if the flip was decided before the write began. No
                money is moved for a flip that has been called off.
        """
        async with self._state_lock:
            if self._phase.terminal:
                raise _FlipOverError(f"it was {self._phase.value} before its result was recorded")
            decision = asyncio.ensure_future(self._record(finished_at))
            self._deciding = decision
        return await see_through(decision)

    async def _record(self, finished_at: datetime) -> int | None:
        """The commit itself: the write, then the phase that follows from it.

        A storage failure costs the flip its number, not its result: nothing was
        recorded, and the group is told so. The row is left alone either way --
        a successful write retired it in the same transaction, and after a
        failed one it holds the only durable copy of the seed, which is not
        thrown away before the seed has been revealed.
        """
        number: int | None = None
        try:
            number = await self._store.record_flip(
                self._finished_flip(finished_at), token=self._token
            )
        except (StorageError, sqlite3.Error) as error:
            _logger.error("flip %s could not be recorded: %s", self._token, error)
        async with self._state_lock:
            self._flip_number = number
            self._ending = _Ending(
                reason=None, was=self._phase, table_live=self._table_live, stage=self._stage
            )
            if not self._enter(FlipPhase.FINISHED) and number is not None:
                # Out of reach: a void stops the round and waits for it before it
                # decides anything, so no commit can be in flight by then. Said
                # loudly all the same, because it would be money against a flip
                # the group has been told was called off.
                _logger.error(
                    "flip %s was recorded as #%d although the flip is %s",
                    self._token,
                    number,
                    self._phase.value,
                )
            # Started here, not by whoever waits for this write: by the time it
            # comes back, the task that asked for it may be gone.
            self._begin_telling()
        return number

    async def tell_outcome(self) -> None:
        """Say how this flip ended, and wait until it has been said.

        The announcement is the flip's own task, started once by whichever
        ending gets here first and joined by everyone after it: a watchdog, a
        second void, the round itself, a shutdown draining what is owed. Nothing
        cancels it but its own deadline, so a caller that is stopped while
        waiting here -- and a caller that stops another one -- cannot cost the
        group its result.

        Returns as soon as there is nothing to say: a flip still playing, or one
        whose ending is the lobby message itself.
        """
        deciding = self._deciding
        if deciding is not None and not deciding.done():
            # A write in flight decides this flip, and it cannot be called back.
            await asyncio.wait({deciding})
        async with self._state_lock:
            telling = self._begin_telling()
        if telling is None:
            return
        # Shielded, because awaiting a task makes it what a cancellation of this
        # caller lands on: the whole point is that no ending caller can cut
        # short the message another one is in the middle of sending.
        await asyncio.shield(telling)

    def _begin_telling(self) -> asyncio.Task[None] | None:
        """Start the flip's one announcement, or hand back the one already going.

        Under the state lock, and called the moment an ending is decided rather
        than when somebody gets round to waiting for it: the task is the owner,
        so a decision is never left with nobody to speak for it -- not by a
        cancelled round, not by a stopped watchdog, not by a bot shutting down
        between the write and the message.

        Returns ``None`` when there is nothing to say: a flip still playing, or
        one whose ending is the lobby message itself.
        """
        if self._ending is None:
            return None
        if self._telling is None:
            self._telling = asyncio.create_task(
                self._announce(), name=f"flip-{self._token}-telling"
            )
        return self._telling

    async def _announce(self) -> None:
        """The flip's last word, said once, and then it lets go of its group.

        Bounded by :data:`~esgbot.engine.timing.TELLING_SECONDS` on the flip's
        own clock, so a message Telegram will not take -- a flood-control wait
        of hours, a post that never comes back -- cannot keep a group occupied
        for ever. Giving up is logged and loses the message, never the record: a
        flip that storage has recorded is never announced as voided, here or
        after a restart.
        """
        ending = self._require_ending()
        delivered = False
        try:
            delivered = await self._within(timing.TELLING_SECONDS, self._deliver(ending))
            if not delivered:
                _logger.error(
                    "flip %s gave up saying how it ended after %.0f seconds",
                    self._token,
                    timing.TELLING_SECONDS,
                )
        except PermanentFailureError:
            _logger.warning("flip %s: the group could not be told how it ended", self._token)
        except Exception:
            _logger.exception("flip %s: saying how it ended broke", self._token)
        async with self._state_lock:
            self._told = True
        await self._retire_row(ending, delivered=delivered)
        self._close()

    async def _deliver(self, ending: _Ending) -> None:
        """Post the one message this ending calls for.

        Raises:
            PermanentFailureError: if the gateway has given up on the group.
        """
        if ending.reason is None:
            number = self._flip_number
            await self._post(self._result_view(number))
            _logger.info("flip %s finished in group %d as #%s", self._token, self._group_id, number)
            return
        if ending.reason is VoidReason.SEND_FAILED:
            # Nothing is posted, because posting is what has stopped working.
            return
        # The live message is edited to take its buttons away. At the showdown
        # there are none, and the hands are already out, so it is left as the
        # record it is and only the notice follows.
        if not ending.table_live:
            # The round never began publishing -- a lobby, or a round whose seed
            # would not store -- so the message is still a lobby and closes as
            # one. Showing a table here would put card counts under a round the
            # group never saw begin.
            await self._edit(self._lobby_view(LobbyStatus.VOIDED))
        elif ending.was is FlipPhase.DEALING:
            await self._edit(self._table_view(ending.stage, voided=True))
        # A flip that had started reveals its seed either way, so the deal it
        # would have made can still be checked.
        await self._post(self._void_view(ending.reason, started=ending.was.started))

    async def _retire_row(self, ending: _Ending, *, delivered: bool) -> None:
        """Let go of the flip's row, now that there is nothing left to recover.

        The row carries the only durable copy of the seed until the group has
        been told, so it goes afterwards and not before. One that could not be
        told keeps its row, and the next start reveals the seed from it instead
        -- except for a flip voided because sending has stopped working, which
        has nothing to say and nothing to wait for.
        """
        if ending.reason is None and self._flip_number is not None:
            # The write that recorded the flip retired its row in the same
            # transaction. There has never been one to clear since.
            return
        if delivered or ending.reason is VoidReason.SEND_FAILED:
            await self._drop_active_row()
            return
        _logger.warning(
            "flip %s: nothing could be posted, so its row is left for the next start",
            self._token,
        )

    async def _within(self, seconds: float, work: Coroutine[Any, Any, None]) -> bool:
        """Run ``work`` against the flip's clock, and say whether it finished in time.

        The deadline stops ``work`` and nothing else: being cancelled while
        waiting here leaves neither of them running.

        Raises:
            Exception: whatever ``work`` raised, if it got that far.
        """
        doing = asyncio.ensure_future(work)
        limit = asyncio.ensure_future(self._clock.sleep(seconds))
        try:
            await asyncio.wait({doing, limit}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            limit.cancel()
            if not doing.done():
                doing.cancel()
            await asyncio.wait({doing, limit})
        if doing.cancelled():
            return False
        doing.result()
        return True

    async def void(self, reason: VoidReason) -> None:
        """Abandon the flip: stop its round, record nothing, reveal the seed, clear its row.

        Every ending but the round's own finish comes through here -- the
        watchdog, a send the gateway has given up on, a round that broke, a
        round whose seed would not store -- and this is the one place a flip is
        decided as void.

        The round is stopped and waited for *first*, before anything is written
        or said, so nothing it does can land behind the decision. That wait is
        also how this learns what the round managed: a commit in flight cannot
        be called back, so if the write got through, the flip is finished rather
        than void, and what is left is to tell the group the result the round
        never got back to. Called from inside the round itself there is nothing
        to stop, and cancelling the current task would abandon this very call.

        Calling this on a flip that is already over does nothing beyond joining
        whatever it is already saying, which is what keeps a late watchdog, or a
        second caller, from announcing a flip voided when storage has recorded
        it -- or from cutting short the message that says so. When the reason is
        :attr:`~esgbot.engine.views.VoidReason.SEND_FAILED` nothing is posted,
        because posting is what has stopped working.
        """
        await self._stop_round()
        async with self._state_lock:
            was = self._phase
            if was.terminal:
                already = was
            else:
                already = None
                self._ending = _Ending(
                    reason=reason,
                    was=was,
                    table_live=self._table_live,
                    stage=self._stage,
                )
                self._enter(FlipPhase.VOIDED)
        if already is None:
            _logger.warning("flip %s voided (%s)", self._token, reason.value)
        else:
            _logger.info(
                "flip %s was already %s when it was to be voided (%s)",
                self._token,
                already.value,
                reason.value,
            )
        await self.tell_outcome()

    async def _stop_round(self) -> None:
        """Stop the round and wait for it, so nothing it does outlives a decision.

        A round partway through its commit cannot be called back -- the write is
        protected from cancellation -- so this waits for it and the write's
        answer stands. Called from the round's own task there is nothing to do:
        cancelling the current task here would abandon the caller that is
        deciding, and a round in its own error handler has already stopped.
        """
        run = self._run_task
        if run is None or run.done() or run is asyncio.current_task():
            return
        # asyncio.wait, so the round's cancellation does not land on this task.
        run.cancel()
        await asyncio.wait({run})

    # ------------------------------------------------------------------- output

    async def _post(self, view: FlipView) -> int:
        """Post a message, in order with this flip's other output."""
        async with self._output_lock:
            return await self._gateway.post(self.target, view)

    async def _edit(self, view: FlipView, *, only_while: FlipPhase | None = None) -> None:
        """Edit the flip's message, following it if the gateway had to repost it.

        ``only_while`` is the phase this view describes. The check happens under
        the output lock, with the edit, so a view built before a press was
        answered cannot land after the flip has moved on -- an open lobby over a
        cancelled one, or over the table of a round that has since started.
        """
        async with self._output_lock:
            if only_while is not None and self._phase is not only_while:
                _logger.info(
                    "flip %s: a %s view was dropped; the flip is %s now",
                    self._token,
                    only_while.value,
                    self._phase.value,
                )
                return
            before = self._message_id
            after = await self._gateway.edit(self.target, before, view)
            if after == before:
                return
            self._message_id = after
        _logger.info("flip %s now lives in message %d", self._token, after)
        try:
            await self._store.update_active_flip(self._token, message_id=after)
        except (StorageError, sqlite3.Error) as error:
            _logger.error("flip %s: could not store the new message id: %s", self._token, error)

    async def _show_table(self, stage: TableStage, *, peeked: int | None = None) -> None:
        """Move the table on to a stage and show it, while the round is still dealing."""
        self._stage = stage
        await self._edit(self._table_view(stage, peeked=peeked), only_while=FlipPhase.DEALING)

    async def _show_lobby(self, status: LobbyStatus = LobbyStatus.OPEN) -> None:
        """Show the lobby, voiding the flip if the gateway has given up on it.

        The lobby is only shown while the flip is in the phase the status is
        about, so a Join or Leave whose answer came back late says nothing.
        """
        try:
            await self._edit(self._lobby_view(status), only_while=_LOBBY_PHASES[status])
        except PermanentFailureError:
            _logger.warning("flip %s: the gateway gave up on the lobby; voiding", self._token)
            await self.void(VoidReason.SEND_FAILED)

    async def _answer(self, press_id: str | None, answer: PressAnswer) -> bool:
        """Answer a button press, before anything else this press leads to.

        A pop-up that will not go out is logged and forgotten: one lost answer is
        not a lost group, and it never costs the flip. Whether it went out comes
        back all the same, because a Peek nobody saw is not a peek.

        A press with no id is the engine being called directly, which is how the
        tests drive it; there is nothing to deliver and nothing to fail.
        """
        if press_id is None:
            return True
        try:
            shown = await self._gateway.answer_press(press_id, answer)
        except PermanentFailureError:
            shown = False
        if not shown:
            _logger.warning("flip %s: press %s went unanswered", self._token, press_id)
        return shown

    async def _save_seed(self, seed: bytes) -> None:
        """Put the seed and the running state on the flip's row, before any card is shown.

        This is the one row update a flip cannot carry on without. The
        commitment about to be posted is a promise that the seed behind it will
        be revealed, and this row is the only copy of that seed a restart has, so
        a round published without it could end in a void notice that reveals
        nothing and a commitment nobody can ever check. Better to abandon the
        round here, with the seed still in memory and not a card shown.

        Raises:
            _SeedNotStoredError: if the write failed, or there was no row left to
                write to.
        """
        try:
            stored = await self._store.update_active_flip(
                self._token, state=FlipState.RUNNING, seed=seed
            )
        except (StorageError, sqlite3.Error) as error:
            raise _SeedNotStoredError(f"its seed could not be stored: {error}") from error
        if not stored:
            raise _SeedNotStoredError("it has no row to store its seed on")

    async def _drop_active_row(self) -> None:
        """Forget the flip's row. A leftover row only costs a void after a restart."""
        try:
            await self._store.remove_active_flip(self._token)
        except (StorageError, sqlite3.Error) as error:
            _logger.error("flip %s: could not clear its row: %s", self._token, error)

    # -------------------------------------------------------------------- views

    def _lobby_view(self, status: LobbyStatus = LobbyStatus.OPEN) -> LobbyView:
        return LobbyView(
            token=self._token,
            request=self._request,
            status=status,
            starter=self._starter,
            players=tuple(self._players),
        )

    def _table_view(
        self, stage: TableStage, *, peeked: int | None = None, voided: bool = False
    ) -> TableView:
        round_ = self._require_round()
        deal = round_.deal
        extra = deal.extra_count if stage is TableStage.PEEKING else 0
        return TableView(
            token=self._token,
            settings=round_.settings,
            y_defaulted=self._request.y_defaulted,
            commitment=round_.commitment,
            stage=stage,
            button_seat=deal.button_seat,
            seats=tuple(
                SeatView(
                    seat=hand.seat,
                    player=round_.seated[hand.seat],
                    card_count=len(hand.hole) + extra,
                )
                for hand in deal.hands
            ),
            board1=() if stage is TableStage.HOLE_CARDS else deal.board1,
            board2=deal.board2 if stage in (TableStage.BOARD2, TableStage.PEEKING) else (),
            extra_count=extra,
            peeked=len(self._peeked) if peeked is None else peeked,
            voided=voided,
        )

    def _showdown_view(self, step: ShowdownStep) -> ShowdownView:
        round_ = self._require_round()
        outcome = self._require_outcome()
        return ShowdownView(
            token=self._token,
            settings=round_.settings,
            y_defaulted=self._request.y_defaulted,
            commitment=round_.commitment,
            step=step,
            button_seat=round_.deal.button_seat,
            boards=self._board_views(),
            hands=self._hand_views(),
            winners=outcome.winners,
            scoop_seat=outcome.scoop_seat,
        )

    def _result_view(self, flip_number: int | None) -> ResultView:
        round_ = self._require_round()
        outcome = self._require_outcome()
        return ResultView(
            token=self._token,
            flip_number=flip_number,
            settings=round_.settings,
            y_defaulted=self._request.y_defaulted,
            commitment=round_.commitment,
            seed=round_.seed,
            button_seat=round_.deal.button_seat,
            boards=self._board_views(),
            hands=self._hand_views(),
            winners=outcome.winners,
            payments=outcome.payments,
            scoop_seat=outcome.scoop_seat,
        )

    def _void_view(self, reason: VoidReason, *, started: bool) -> VoidView:
        """The notice for a flip that was abandoned, saying as much as is known.

        A flip Start had settled says its four settings; one that never started
        has only the request it was opened with, so that is what its notice
        carries. Both are passed, because the notice shows the settled figures
        where there are any and ``/again`` is the only other reader of a request.
        """
        round_ = self._round
        return VoidView(
            token=self._token,
            reason=reason,
            request=self._request,
            settings=None if round_ is None else round_.settings,
            players=round_.seated if round_ is not None else tuple(self._players),
            seed=round_.seed if started and round_ is not None else None,
            commitment=round_.commitment if started and round_ is not None else None,
        )

    def _board_views(self) -> tuple[BoardView, ...]:
        deal = self._require_round().deal
        outcome = self._require_outcome()
        community = {Board.BOARD1: deal.board1, Board.BOARD2: deal.board2}
        views: list[BoardView] = []
        for board in Board:
            score = outcome.board(board)
            best = score.hand_for_seat(score.winners[0])
            views.append(
                BoardView(
                    board=board,
                    winners=score.winners,
                    best_cards=best.cards,
                    description=best.description,
                    share=score.share,
                    cards=community.get(board, ()),
                )
            )
        return tuple(views)

    def _hand_views(self) -> tuple[HandView, ...]:
        round_ = self._require_round()
        outcome = self._require_outcome()
        return tuple(
            HandView(
                seat=hand.seat,
                player=round_.seated[hand.seat],
                hole=hand.hole,
                extra=hand.extra,
                points=result.points,
                net_dollars=result.net_dollars,
                winner=result.winner,
                scooped=result.scooped,
            )
            for hand, result in zip(round_.deal.hands, outcome.seats, strict=True)
        )

    def _finished_flip(self, finished_at: datetime) -> FinishedFlip:
        round_ = self._require_round()
        outcome = self._require_outcome()
        return FinishedFlip(
            group_id=self._group_id,
            settings=round_.settings,
            y_defaulted=self._request.y_defaulted,
            z_defaulted=self._request.z_defaulted,
            seed=round_.seed,
            commitment=round_.commitment,
            deal=round_.deal,
            players=tuple(
                FlipPlayer(
                    user_id=player.user_id,
                    display_name=player.display_name,
                    username=player.username,
                    seat=result.seat,
                    points=result.points,
                    net_dollars=result.net_dollars,
                    scooped=result.scooped,
                )
                for player, result in zip(round_.seated, outcome.seats, strict=True)
            ),
            started_at=round_.started_at,
            finished_at=finished_at,
        )

    # ------------------------------------------------------------------ plumbing

    def _require_round(self) -> _Round:
        if self._round is None:
            raise RuntimeError(f"flip {self._token} has not started")
        return self._round

    def _require_outcome(self) -> FlipOutcome:
        if self._outcome is None:
            raise RuntimeError(f"flip {self._token} has no result yet")
        return self._outcome

    def _require_ending(self) -> _Ending:
        if self._ending is None:
            raise RuntimeError(f"flip {self._token} has not ended")
        return self._ending

    async def _sleep(self, seconds: float, wake: asyncio.Event) -> None:
        """Wait on the clock, or until ``wake`` is set, whichever comes first.

        Both deadlines in a flip can be cut short -- a peek can bring the
        showdown forward, a join can push the idle timeout out -- so every wait
        the engine makes can be woken to recompute.

        Tidying up must never swallow a cancellation meant for this flip, or
        stopping it would quietly fail and shutting the bot down would hang. Both
        waits here are :func:`asyncio.wait`, which keeps the two inner tasks'
        cancellations to itself and lets ours straight through.
        """
        if seconds <= 0:
            return
        sleeper = asyncio.create_task(self._clock.sleep(seconds))
        waker = asyncio.create_task(wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
            await asyncio.wait({sleeper, waker})
        if not sleeper.cancelled():
            # A clock that raised is a bug, not a sleep that ended.
            sleeper.result()

    def _cancel_pending(self) -> None:
        """Stop this flip's timer and round tasks, apart from whichever is running now.

        The announcement is never among them. It is the flip's last word, owned
        by the flip rather than by whoever decided the ending, and nothing but
        its own deadline stops it -- which is what keeps one ending caller from
        costing the group the message another was in the middle of sending. The
        round is left alone once that announcement has begun, because by then it
        is only waiting for it.
        """
        current = asyncio.current_task()
        pending = [self._timer_task]
        if self._telling is None:
            pending.append(self._run_task)
        for task in pending:
            if task is not None and task is not current and not task.done():
                task.cancel()

    def _close(self) -> None:
        """Last thing a flip does: drop its timers and let the registry forget it."""
        self._cancel_pending()
        self._closed.set()
        if self._on_close is not None:
            self._on_close(self)

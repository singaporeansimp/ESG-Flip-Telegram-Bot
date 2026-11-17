"""One flip per group, and the token every button press arrives with.

The registry is the engine's front door. It holds the flips that are open, keyed
both by group -- so a second ``/flip`` can be refused while one is running -- and
by token, so a press can be matched to its flip. A token the registry does not
know is a press on a lobby or table from before a restart, or on one that has
already finished, and the Telegram layer answers it with an "expired" notice.

A flip takes its place in the registry before its lobby message is posted, so two
``/flip`` commands racing in the same group cannot both get through; if opening
then fails, the place is given back. Flips take themselves out when they finish,
are cancelled or are voided. :meth:`FlipRegistry.migrate_group` moves one from
the id a group had to the id Telegram has just given it, so a flip in a group
that becomes a supergroup keeps playing in the same chat under its new number.
The id it has left behind goes on naming that group for the rest of the run --
:meth:`FlipRegistry.group_id_for` is how every slot here is found -- because a
command the bot is part way through reading was read under the old id, and a
flip opened under it would be a second flip in a chat that is allowed one.

This is all in memory. Nothing here survives a restart, which is the point: the
flips that were open are the rows the next start finds and voids. The one thing
the registry does reach storage for is that migration, which takes the moved
flip's own row along so the rows a restart reads name the chat the flip is
really in.

:meth:`FlipRegistry.shut_down` is how the bot stops, and it is not simply
:meth:`FlipRegistry.close`: a flip whose result was being recorded as polling
ended is finished rather than open, and recording it took its row away, so there
is nothing for the next start to find and that flip's result has to go out now
or never. Stopping therefore waits for those writes and those last messages,
briefly, and before the Telegram session closes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import TracebackType
from typing import Self

from esgbot.engine import timing
from esgbot.engine.clock import Clock, SystemClock
from esgbot.engine.flip import Flip, Settler, settle_in_thread
from esgbot.engine.gateway import Gateway
from esgbot.engine.peek import HandReader, read_hand_in_thread
from esgbot.engine.store import FlipStore
from esgbot.engine.views import Player, VoidReason
from esgbot.game.rules import FlipRequest, Settings
from esgbot.logging_setup import get_logger

__all__ = ["FlipAlreadyOpenError", "FlipRegistry"]

_logger = get_logger(__name__)


class FlipAlreadyOpenError(Exception):
    """This group already has a flip open, and only one runs at a time.

    ``flip`` is the one that is open, so the refusal can point at its message.
    """

    def __init__(self, flip: Flip) -> None:
        super().__init__(f"group {flip.group_id} already has flip {flip.token} open")
        self.flip = flip

    @property
    def group_id(self) -> int:
        """The group that already has a flip open."""
        return self.flip.group_id


class FlipRegistry:
    """The flips that are open, by group and by token."""

    __slots__ = (
        "_by_group",
        "_by_token",
        "_clock",
        "_gateway",
        "_moved",
        "_read_hand",
        "_settle",
        "_store",
        "_voiding",
    )

    def __init__(
        self,
        *,
        gateway: Gateway,
        store: FlipStore,
        clock: Clock | None = None,
        settle: Settler = settle_in_thread,
        read_hand: HandReader = read_hand_in_thread,
    ) -> None:
        """Hold the parts every flip in this process shares.

        ``clock`` defaults to the real one, and ``settle`` and ``read_hand`` to
        the worker-thread showdown and the worker-thread peek reading; tests
        replace all three.
        """
        self._gateway = gateway
        self._store = store
        self._clock: Clock = SystemClock() if clock is None else clock
        self._settle = settle
        self._read_hand = read_hand
        self._by_group: dict[int, Flip] = {}
        self._by_token: dict[str, Flip] = {}
        self._moved: dict[int, int] = {}
        self._voiding: set[asyncio.Task[None]] = set()

    def __len__(self) -> int:
        return len(self._by_token)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def clock(self) -> Clock:
        """The clock every flip here runs on."""
        return self._clock

    @property
    def flips(self) -> tuple[Flip, ...]:
        """Every flip that is open, in no particular order."""
        return tuple(self._by_token.values())

    def group_id_for(self, group_id: int) -> int:
        """The id this group is keyed by now, following every move it has made.

        A group that becomes a supergroup is the same group under a new id, and
        :meth:`migrate_group` writes the pair down, so an id Telegram has retired
        still names the group it belonged to. Everything here that is about "this
        group" goes through this, which is what makes one flip per group hold for
        a command that arrived under an id the group has since left behind.
        """
        # Bounded by the number of moves known, so a pair of ids pointing at each
        # other cannot loop.
        for _ in range(len(self._moved)):
            moved = self._moved.get(group_id)
            if moved is None:
                break
            group_id = moved
        return group_id

    def flip_for_group(self, group_id: int) -> Flip | None:
        """The flip open in this group, or ``None``, whichever id it is asked for."""
        return self._by_group.get(self.group_id_for(group_id))

    def flip_for_token(self, token: str) -> Flip | None:
        """The flip a button press belongs to, or ``None`` if this process never knew it."""
        return self._by_token.get(token)

    async def create_flip(
        self,
        *,
        group_id: int,
        starter: Player,
        settings: Settings | FlipRequest,
        also_seated: Sequence[Player] = (),
        topic_id: int | None = None,
        token: str | None = None,
    ) -> Flip:
        """Open a lobby: post its message, write its row, and start its timer.

        The starter joins as the first player, and ``also_seated`` -- the
        players who asked to be put into flips like this one -- after them, as
        far as the player cap allows.

        ``settings`` is what the command asked for: a
        :class:`~esgbot.game.rules.FlipRequest` when it left ``Y`` or ``Z`` to
        the bot, or plain :class:`~esgbot.game.rules.Settings` when it gave all
        four. Either way the flip resolves them at Start; see :class:`Flip`.

        ``group_id`` is taken as naming a chat rather than as a key: a command
        that was read under an id the group has since left behind -- one that
        waited on storage while the migration arrived -- opens its flip under the
        id the group is keyed by now, or is refused because that is where the
        group's one flip already is. See :meth:`group_id_for`.

        Raises:
            FlipAlreadyOpenError: if this group already has one open.
            PermanentFailureError: if the lobby message cannot be posted.
            StorageError, sqlite3.Error: if the flip's row cannot be written.
                Either way no flip is left behind and the group can try again.
        """
        group_id = self.group_id_for(group_id)
        open_already = self._by_group.get(group_id)
        if open_already is not None:
            raise FlipAlreadyOpenError(open_already)
        flip = Flip(
            group_id=group_id,
            topic_id=topic_id,
            settings=settings,
            starter=starter,
            also_seated=also_seated,
            gateway=self._gateway,
            store=self._store,
            clock=self._clock,
            token=token,
            settle=self._settle,
            read_hand=self._read_hand,
            on_close=self._forget,
        )
        # Claimed before the first await, so two /flip commands cannot race in.
        self._by_group[group_id] = flip
        self._by_token[flip.token] = flip
        try:
            await flip._open()
        except BaseException:
            self._forget(flip)
            raise
        return flip

    async def migrate_group(self, *, old_group_id: int, new_group_id: int) -> Flip | None:
        """Follow a group Telegram has given a new id, taking its open flip along.

        A group that becomes a supergroup keeps its flip but not its id, and the
        old one stops working, so the flip's place here and everything it has
        left to say move to the new one. Nothing else about the flip changes: the
        same token, players, timer and topic, so the round carries on where it
        was, and a ``/flip`` in the new group is refused while it runs.

        The flip's active-flip row follows it too, which is the only thing here
        that waits on the store and the only reason this is a coroutine: see
        :meth:`Flip._follow_group`. Everything in memory has moved before that
        write, and a write that fails is logged rather than raised. The group's
        finished flips move separately, with the rest of its history.

        **The old id is not left free**: the pair is written down, so it goes on
        naming this group for as long as the bot runs, whether or not there was
        a flip to move. Telegram sends nothing to a retired id, but the bot can
        still be holding a command that arrived under one -- a ``/again`` part
        way through reading the group's last settings -- and a flip opened under
        a dead id is a second flip in a chat that is only allowed one. See
        :meth:`group_id_for`.

        A clash is the one case nothing can be made tidy: both ids have a flip,
        so one chat has two, and the move has nowhere to go. The flip on the id
        Telegram has retired is the one that cannot stay, so it is taken out
        here and voided -- which clears its row and tells the group -- and the
        flip in the group that still exists keeps its place. Telegram only
        migrates a group to an id nothing has used, so this takes a ``/flip``
        under the new id before the bot has heard of the move.

        Returns:
            The flip that moved, or ``None`` when there was nothing to move: the
            two ids are the same or already known to be, the group had no flip
            open, its flip is over and already forgotten, or the new id had a
            flip of its own and this group's was voided. One that has closed but
            is still here moves too, so the last thing it has to say lands in
            the group that still exists.
        """
        old_group_id = self.group_id_for(old_group_id)
        if old_group_id == new_group_id:
            return None
        # Written down before anything else, and before the first await, so no
        # command can reserve the old id from here on and nothing can find the
        # registry and the flip disagreeing about which group this is.
        self._moved[old_group_id] = new_group_id
        flip = self._by_group.get(old_group_id)
        if flip is None:
            return None
        open_already = self._by_group.get(new_group_id)
        if open_already is not None:
            _logger.error(
                "group %d became %d, which already has flip %s open; its own flip %s is voided",
                old_group_id,
                new_group_id,
                open_already.token,
                flip.token,
            )
            self._forget(flip)
            self._abandon(flip)
            return None
        del self._by_group[old_group_id]
        self._by_group[new_group_id] = flip
        await flip._follow_group(new_group_id)
        _logger.info(
            "flip %s moved with its group from %d to %d", flip.token, old_group_id, new_group_id
        )
        return flip

    def _abandon(self, flip: Flip) -> None:
        """Have a flip void itself, without waiting for it.

        Not awaited, because the move that abandons it may have been discovered
        by this very flip's own send: the gateway follows a new group id from
        inside the call that was refused with it, and the flip holds its output
        while that call is in flight, so it cannot post a void notice until the
        send that started all this has let go. The task is held until it is done,
        so nothing collects it part way through, and a bot that stops before it
        finishes leaves the flip's row for the next start to void.
        """
        voiding = asyncio.create_task(flip.void(VoidReason.ERROR), name=f"void-{flip.token}")
        self._voiding.add(voiding)
        voiding.add_done_callback(self._voiding.discard)

    def close(self) -> None:
        """Drop every open flip's timers and forget them, abruptly.

        Their rows stay, so the next start voids them and tells each group. What
        this does not do is wait for anything: a flip part way through recording
        its result is left to finish that write on its own, and whatever it had
        to say goes unsaid. :meth:`shut_down` is the version the bot uses.
        """
        for flip in self.flips:
            flip.stop()
        count = len(self._by_token)
        self._by_group.clear()
        self._by_token.clear()
        if count:
            _logger.info("left %d open flip(s) for the next start to void", count)

    async def shut_down(self, *, within: float = timing.SHUTDOWN_TELLING_SECONDS) -> None:
        """Stop every flip, let it say how it ended, and forget them all.

        This is what the bot does when polling stops, and it runs before the
        Telegram session is closed, because the one thing a stopping flip may
        still owe its group is a message. A flip that was recording its result
        when the bot was asked to stop has a write that cannot be called back:
        waiting for it is the only way to know the flip finished, and once it
        has, nothing else will ever be in a position to say so -- it has no row
        left for the next start to find.

        Bounded by ``within``, in real seconds rather than on the flip's clock,
        because what is being waited for is a database write in a worker thread
        and a send on the wire. A stop that cannot finish is worse than a result
        that arrives nowhere, so a flip still trying when the time is up is left
        to its own deadline and the loss is logged. The flip is recorded either
        way, and ``/verify`` can still answer for it.
        """
        flips = self.flips
        for flip in flips:
            flip.stop()
        if flips:
            telling = [
                asyncio.create_task(flip.tell_outcome(), name=f"shutdown-{flip.token}")
                for flip in flips
            ]
            _, late = await asyncio.wait(telling, timeout=within)
            for task in late:
                task.cancel()
            if late:
                _logger.error(
                    "%d flip(s) could not say how they ended within %.0f seconds of stopping",
                    len(late),
                    within,
                )
        self._by_group.clear()
        self._by_token.clear()
        if flips:
            _logger.info("stopped %d open flip(s)", len(flips))

    def _forget(self, flip: Flip) -> None:
        """Take a flip out, once it is over or if it never opened."""
        if self._by_group.get(flip.group_id) is flip:
            del self._by_group[flip.group_id]
        if self._by_token.get(flip.token) is flip:
            del self._by_token[flip.token]

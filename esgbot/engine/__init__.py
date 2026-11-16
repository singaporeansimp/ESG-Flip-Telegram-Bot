"""The flip engine: one flip's state machine, its timers, and one per group.

This is the half of the bot that knows the rules of a flip but nothing about
Telegram. It reaches the world through three small interfaces, so the whole thing
runs under test with three fakes and no network, database or waiting:

* :class:`~esgbot.engine.gateway.Gateway` -- post a message, edit a message,
  answer a button press. The engine passes
  :mod:`view data <esgbot.engine.views>`, never text.
* :class:`~esgbot.engine.store.FlipStore` -- the four storage calls a flip makes.
  :class:`esgbot.storage.Database` satisfies it as it stands.
* :class:`~esgbot.engine.clock.Clock` -- monotonic time and sleeping, plus UTC
  wall time for stored timestamps.

Running a flip::

    registry = FlipRegistry(gateway=gateway, store=database)
    flip = await registry.create_flip(group_id=-1001, starter=starter, settings=settings)
    await flip.join(player, press_id=press.id)
    await flip.start(starter.user_id, press_id=press.id)   # the round runs itself
    await flip.wait_closed()

Every button press goes to :meth:`~esgbot.engine.flip.Flip.join`,
:meth:`~esgbot.engine.flip.Flip.leave`, :meth:`~esgbot.engine.flip.Flip.start`,
:meth:`~esgbot.engine.flip.Flip.cancel` or
:meth:`~esgbot.engine.flip.Flip.peek`, which answer the press first and then post
or edit whatever changed. Use
:meth:`~esgbot.engine.registry.FlipRegistry.flip_for_token` to find the flip a
press belongs to; a token this process does not know is a press from before a
restart, or on a flip that is over, and deserves an "expired" answer.

:mod:`esgbot.engine.timing` holds every delay in a flip, including the two
numbers the table message quotes: thirty seconds, or ten after everyone has
peeked.
"""

from esgbot.engine.clock import Clock, SystemClock
from esgbot.engine.flip import Flip, FlipPhase, Settler, new_token, settle_in_thread
from esgbot.engine.gateway import Gateway, MessageTarget, PermanentFailureError
from esgbot.engine.registry import FlipAlreadyOpenError, FlipRegistry
from esgbot.engine.store import FlipStore
from esgbot.engine.views import (
    BoardView,
    CommitmentView,
    FlipView,
    HandView,
    LobbyStatus,
    LobbyView,
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

__all__ = [
    "BoardView",
    "Clock",
    "CommitmentView",
    "Flip",
    "FlipAlreadyOpenError",
    "FlipPhase",
    "FlipRegistry",
    "FlipStore",
    "FlipView",
    "Gateway",
    "HandView",
    "LobbyStatus",
    "LobbyView",
    "MessageTarget",
    "Notice",
    "PeekAnswer",
    "PermanentFailureError",
    "Player",
    "PressAnswer",
    "ResultView",
    "SeatView",
    "Settler",
    "ShowdownStep",
    "ShowdownView",
    "SystemClock",
    "TableStage",
    "TableView",
    "VoidReason",
    "VoidView",
    "new_token",
    "settle_in_thread",
]

"""Fakes the Telegram layer's tests run on. No network, no bot token, no waiting.

:class:`FakeTelegram` is a stand-in for :class:`aiogram.Bot`, as the gateway's
:class:`~esgbot.telegram.gateway.TelegramClient` protocol sees it: four calls,
each of which writes down what it was asked to do and can be made to fail on cue.
That is enough to drive every refusal the gateway handles -- a flood-control
wait, an edit Telegram calls unchanged, a deleted table, a group the bot has been
thrown out of -- without anything leaving the process.

**Two clocks, and they are not interchangeable.** The engine's timers need
:class:`~tests.engine_helpers.FakeClock`, which only moves when a test moves it:
give the engine a clock that returns from every sleep at once and a lobby cancels
itself the moment it opens. The gateway wants the opposite --
:class:`RecordingClock`, which returns from a sleep immediately and writes down
how long it was asked to wait -- because a flood-control wait happens inside the
call a test is awaiting, and nothing would be left running to move a
:class:`FakeClock` on. :func:`bot_harness` gives each the one it needs, and
``harness.waits`` is what the gateway slept for.

:func:`group_message`, :func:`topic_message`, :func:`button_press` and
:func:`migration_message` build the aiogram objects a dispatcher expects, and
:func:`bot_harness` wires a dispatcher, a gateway, a registry and a store
together the way :mod:`esgbot.telegram.app` does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from esgbot.engine import FlipRegistry, MessageTarget
from esgbot.storage import (
    DEFAULT_LEADERBOARD_LIMIT,
    ActiveFlip,
    FinishedFlip,
    FlipState,
    GroupMigration,
    PlayerStats,
    StoredFlip,
    queries,
    schema,
)
from esgbot.telegram.gateway import (
    FLOOD_RETRIES,
    SEND_ATTEMPTS,
    MigrationHandler,
    TelegramGateway,
)
from esgbot.telegram.handlers import build_router
from tests.engine_helpers import GROUP, SETTINGS, FakeClock, FakeStore, settle_inline

__all__ = [
    "BOT_TOKEN",
    "DEFAULT_SETTINGS",
    "GROUP",
    "STARTER",
    "TOPIC",
    "BotHarness",
    "Call",
    "FakeDatabase",
    "FakeTelegram",
    "RecordingClock",
    "active_row",
    "bad_request",
    "bot_harness",
    "button_press",
    "deleted_message",
    "flood_wait",
    "group_message",
    "kicked_out",
    "migration_message",
    "not_modified",
    "telegram_gateway",
    "topic_message",
    "update",
    "user",
]

TOPIC: Final = 77
"""The forum topic the topic-routing tests use."""

BOT_TOKEN: Final = "123456:test-token-never-used"
"""A token shaped like a real one. Nothing here ever calls Telegram with it."""

STARTER: Final = 11
"""The user id ``/flip`` is sent by, matching :func:`tests.engine_helpers.player`."""

_EPOCH: Final = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------- the clock


class RecordingClock:
    """A clock whose sleeps are over as soon as they start, and are written down.

    The gateway waits inside the call a test is awaiting -- a flood-control pause
    sits between two attempts at the same send -- so there is nobody left to move
    a :class:`~tests.engine_helpers.FakeClock` along. This one returns at once and
    keeps the figures in :attr:`sleeps`, which is what a test checks: that the
    gateway waited for exactly as long as Telegram asked.

    Time still moves by what was slept, so :meth:`monotonic` and :meth:`now` say
    what they would have said had the waiting been real.
    """

    __slots__ = ("_monotonic", "_origin", "_start", "sleeps")

    def __init__(self, *, start: datetime = _EPOCH, monotonic: float = 10_000.0) -> None:
        self._start = start
        self._origin = monotonic
        self._monotonic = monotonic
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        """Wall-clock time, as far on as the sleeps have taken it."""
        return self._start + timedelta(seconds=self._monotonic - self._origin)

    def monotonic(self) -> float:
        """The current reading."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Note the wait, move time on by it, and carry on at once."""
        if seconds > 0:
            self.sleeps.append(seconds)
            self._monotonic += seconds
        await asyncio.sleep(0)

    @property
    def waited(self) -> float:
        """Everything this clock has been asked to wait for, added up."""
        return sum(self.sleeps)


# ------------------------------------------------------- Telegram's refusals


def flood_wait(seconds: int) -> TelegramRetryAfter:
    """ "Wait this long and try again", which is not a failure."""
    return TelegramRetryAfter(
        method=SendMessage(chat_id=GROUP, text="x"),
        message=f"Too Many Requests: retry after {seconds}",
        retry_after=seconds,
    )


def not_modified() -> TelegramBadRequest:
    """An edit Telegram considers identical to what is already there."""
    return bad_request("Bad Request: message is not modified")


def deleted_message() -> TelegramBadRequest:
    """What an edit gets once somebody has deleted the message."""
    return bad_request("Bad Request: message to edit not found")


def kicked_out() -> TelegramForbiddenError:
    """The bot is not in the group any more. Trying again will not help."""
    return TelegramForbiddenError(
        method=SendMessage(chat_id=GROUP, text="x"),
        message="Forbidden: bot was kicked from the supergroup chat",
    )


def bad_request(message: str) -> TelegramBadRequest:
    """Any other 400, with Telegram's wording."""
    return TelegramBadRequest(method=EditMessageText(text="x"), message=message)


# --------------------------------------------------------------- the client


@dataclass(frozen=True, slots=True)
class Call:
    """One thing the client was asked to do.

    ``kind`` is ``"send"``, ``"edit"``, ``"markup"`` or ``"answer"``. The fields
    that do not apply to a kind are left at their defaults, so ``text`` is empty
    on a ``"markup"`` call and ``message_id`` is zero on a ``"send"``.
    """

    kind: str
    chat_id: int = 0
    message_id: int = 0
    thread_id: int | None = None
    text: str = ""
    parse_mode: str | None = None
    markup: InlineKeyboardMarkup | None = None
    press_id: str = ""
    alert: bool = False

    @property
    def buttons(self) -> tuple[str, ...]:
        """The labels on this call's buttons, in order, flattened across rows."""
        if self.markup is None:
            return ()
        return tuple(button.text for row in self.markup.inline_keyboard for button in row)

    @property
    def callback_data(self) -> tuple[str, ...]:
        """The callback data behind this call's buttons, in the same order."""
        if self.markup is None:
            return ()
        return tuple(
            button.callback_data or "" for row in self.markup.inline_keyboard for button in row
        )


class FakeTelegram:
    """A Telegram client that writes down what it was asked and fails when told to.

    Two ways to make a call go wrong, which cover every case the gateway handles:

    * ``failures`` is a queue. Each send, edit or button-clearing call takes the
      next item off it; an exception is raised and ``None`` is let through. When
      the queue runs dry, calls succeed. So ``[flood_wait(3), None]`` is one wait
      followed by a success, and ``[kicked_out()] * 3`` is a group that is gone.
    * ``deleted`` holds message ids somebody has deleted. Editing one raises
      :func:`deleted_message`, as Telegram does, however many attempts are left.
      Delete an id this client actually handed out, so the fresh message the
      gateway posts in its place is plainly a different one.

    ``answer_callback_query`` has its own ``answer_failure``, because a pop-up
    that will not go out is not retried and never costs a flip.

    ``attempts`` counts every send, edit and button-clearing call that was tried,
    whether it got through or not, which is how a test checks that the gateway
    gave up after three rather than two or four. ``calls`` holds only the ones
    that worked, because a call that raised did nothing.
    """

    __slots__ = (
        "_next_message_id",
        "answer_failure",
        "attempts",
        "calls",
        "deleted",
        "failures",
    )

    def __init__(self, *, first_message_id: int = 500) -> None:
        self.calls: list[Call] = []
        self.failures: list[BaseException | None] = []
        self.answer_failure: BaseException | None = None
        self.deleted: set[int] = set()
        self.attempts = 0
        self._next_message_id = first_message_id

    # ------------------------------------------------------------- the client

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        await self._maybe_fail()
        message_id = self._next_message_id
        self._next_message_id += 1
        self.calls.append(
            Call(
                kind="send",
                chat_id=chat_id,
                message_id=message_id,
                thread_id=message_thread_id,
                text=text,
                parse_mode=parse_mode,
                markup=reply_markup,
            )
        )
        return _message(message_id=message_id, chat_id=chat_id, text=text)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        await self._maybe_fail()
        if message_id in self.deleted:
            raise deleted_message()
        self.calls.append(
            Call(
                kind="edit",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                markup=reply_markup,
            )
        )
        return _message(message_id=message_id, chat_id=chat_id, text=text)

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        await self._maybe_fail()
        if message_id in self.deleted:
            raise deleted_message()
        self.calls.append(
            Call(kind="markup", chat_id=chat_id, message_id=message_id, markup=reply_markup)
        )
        return True

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        await asyncio.sleep(0)
        if self.answer_failure is not None:
            raise self.answer_failure
        self.calls.append(
            Call(
                kind="answer",
                press_id=callback_query_id,
                text=text if text is not None else "",
                alert=show_alert,
            )
        )
        return True

    async def _maybe_fail(self) -> None:
        await asyncio.sleep(0)
        self.attempts += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure

    # ------------------------------------------------------------- readback

    def of(self, kind: str) -> tuple[Call, ...]:
        """Every call of one kind, in order."""
        return tuple(call for call in self.calls if call.kind == kind)

    @property
    def sends(self) -> tuple[Call, ...]:
        """The messages that were sent, in order."""
        return self.of("send")

    @property
    def edits(self) -> tuple[Call, ...]:
        """The edits that were made, in order."""
        return self.of("edit")

    @property
    def answers(self) -> tuple[Call, ...]:
        """The button presses that were answered, in order."""
        return self.of("answer")

    @property
    def texts(self) -> tuple[str, ...]:
        """Everything that was sent or edited into the group, in order."""
        return tuple(call.text for call in self.calls if call.kind in ("send", "edit"))

    def last(self, kind: str) -> Call:
        """The most recent call of one kind.

        Raises:
            AssertionError: if there has not been one, which is a clearer failure
                than an index error three lines later.
        """
        calls = self.of(kind)
        if not calls:
            raise AssertionError(f"nothing was {kind!r}ed; calls so far: {self.calls}")
        return calls[-1]


# ---------------------------------------------------------------- the store


class FakeDatabase(FakeStore):
    """:class:`~tests.engine_helpers.FakeStore`, plus what the Telegram layer asks of it.

    Flips in progress stay in the parent's dictionary, because that is what the
    engine's tests read and what the startup sweep is driven through. **Finished
    flips go into a real database**: this one owns an in-memory SQLite
    connection, migrated with the bot's own schema, and answers
    :meth:`record_flip`, the two stats questions and :meth:`fetch_flip` by
    calling :mod:`esgbot.storage.queries` on it. So a flip number is a real
    number handed out by the insert, and ``/leaderboard``, ``/stats`` and
    ``/verify`` are answered by the SQL the bot ships rather than by a fake that
    would have to reimplement the ranking, the username lookup and the stored
    deal to be any use.

    Nothing here blocks: an in-memory connection never waits on a disk, so the
    queries run inline and the clock still only moves when a test moves it.

    Set ``list_error``, ``migrate_error``, ``stats_error`` or ``fetch_error`` to
    the exception that call should raise. For damage rather than failure, write
    to :attr:`connection` directly -- ``PRAGMA ignore_check_constraints = ON``
    first, as the storage tests do -- which is how a test tampers with a stored
    flip and then watches ``/verify`` catch it.
    """

    __slots__ = (
        "connection",
        "fetch_error",
        "list_error",
        "migrate_error",
        "migrations",
        "stats_error",
    )

    def __init__(self) -> None:
        super().__init__()
        self.connection = schema.connect(":memory:")
        schema.apply_migrations(self.connection)
        self.list_error: Exception | None = None
        self.migrate_error: Exception | None = None
        self.stats_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.migrations: list[tuple[int, int]] = []

    async def close(self) -> None:
        """Let go of the in-memory database, as the real one is closed. Twice is harmless."""
        await asyncio.sleep(0)
        self.connection.close()

    # ------------------------------------------------------------ finished flips

    async def record_flip(self, flip: FinishedFlip, *, token: str | None = None) -> int:
        """Write a finished flip for real, and give it the number sqlite hands out.

        The flip's in-progress row lives in the parent's dictionary rather than
        in this database, so that is what ``token`` drops here; the real store
        deletes the row in the same transaction as the insert.
        """
        await asyncio.sleep(0)
        if self.record_error is not None:
            raise self.record_error
        number = queries.record_flip(self.connection, flip)
        self.recorded.append(flip)
        if token is not None:
            self.active.pop(token, None)
        return number

    async def leaderboard(
        self, group_id: int, *, limit: int | None = DEFAULT_LEADERBOARD_LIMIT
    ) -> tuple[PlayerStats, ...]:
        """One group's players, ranked by the real query."""
        await asyncio.sleep(0)
        if self.stats_error is not None:
            raise self.stats_error
        return queries.leaderboard(self.connection, group_id, limit=limit)

    async def player_stats(self, *, group_id: int, user_id: int) -> PlayerStats | None:
        """One player's figures in one group, by Telegram user id."""
        await asyncio.sleep(0)
        if self.stats_error is not None:
            raise self.stats_error
        return queries.player_stats(self.connection, group_id=group_id, user_id=user_id)

    async def player_stats_by_username(self, *, group_id: int, username: str) -> PlayerStats | None:
        """One player's figures in one group, by the username they played under."""
        await asyncio.sleep(0)
        if self.stats_error is not None:
            raise self.stats_error
        return queries.player_stats_by_username(
            self.connection, group_id=group_id, username=username
        )

    async def fetch_flip(self, *, group_id: int, flip_number: int) -> StoredFlip | None:
        """One finished flip read back out, as ``/verify`` gets it."""
        await asyncio.sleep(0)
        if self.fetch_error is not None:
            raise self.fetch_error
        return queries.fetch_flip(self.connection, group_id=group_id, flip_number=flip_number)

    # -------------------------------------------------------- the whole group

    async def list_active_flips(self) -> tuple[ActiveFlip, ...]:
        """Every open flip's row, oldest first, as the real database gives them."""
        await asyncio.sleep(0)
        if self.list_error is not None:
            raise self.list_error
        return tuple(sorted(self.active.values(), key=lambda row: row.created_at))

    async def migrate_group_id(self, *, old_group_id: int, new_group_id: int) -> GroupMigration:
        """Move a group's finished flips and open rows to its new id.

        The finished flips move in the database, so stats and ``/verify`` follow
        the group; the open flips move in the dictionary they live in, which is
        where the count of them comes from. A flip that is still live has already
        moved its own row by token, as the engine does, so what this moves is
        whatever was left over from an earlier run.
        """
        await asyncio.sleep(0)
        if self.migrate_error is not None:
            raise self.migrate_error
        self.migrations.append((old_group_id, new_group_id))
        moved = {
            token: replace(row, group_id=new_group_id)
            for token, row in self.active.items()
            if row.group_id == old_group_id
        }
        self.active.update(moved)
        migration = queries.migrate_group_id(
            self.connection, old_group_id=old_group_id, new_group_id=new_group_id
        )
        return GroupMigration(flips=migration.flips, active_flips=len(moved))


# -------------------------------------------------------- aiogram objects


def _message(*, message_id: int, chat_id: int, text: str) -> Message:
    """The message a send or an edit comes back as."""
    return Message(
        message_id=message_id,
        date=_EPOCH,
        chat=Chat(id=chat_id, type="supergroup"),
        text=text,
    )


def user(user_id: int = STARTER, name: str = "Sam", username: str | None = "sam") -> User:
    """One Telegram user. ``full_name`` is what the group would see."""
    return User(id=user_id, is_bot=False, first_name=name, username=username)


def group_message(
    text: str,
    *,
    sender: User | None = None,
    chat_id: int = GROUP,
    chat_type: str = "supergroup",
    message_id: int = 1,
) -> Message:
    """A message in a group without topics."""
    return Message(
        message_id=message_id,
        date=_EPOCH,
        chat=Chat(id=chat_id, type=chat_type),
        from_user=user() if sender is None else sender,
        text=text,
    )


def topic_message(
    text: str,
    *,
    topic_id: int = TOPIC,
    sender: User | None = None,
    chat_id: int = GROUP,
    message_id: int = 1,
) -> Message:
    """A message inside a forum topic, which is where that flip then lives."""
    return Message(
        message_id=message_id,
        date=_EPOCH,
        chat=Chat(id=chat_id, type="supergroup"),
        from_user=user() if sender is None else sender,
        text=text,
        message_thread_id=topic_id,
        is_topic_message=True,
    )


def migration_message(*, old_group_id: int = GROUP, new_group_id: int) -> Message:
    """The service message Telegram sends when a group becomes a supergroup."""
    return Message(
        message_id=99,
        date=_EPOCH,
        chat=Chat(id=old_group_id, type="group"),
        from_user=user(),
        migrate_to_chat_id=new_group_id,
    )


def button_press(
    data: str,
    *,
    user_id: int = STARTER,
    press_id: str = "press-1",
    name: str = "Sam",
    message_id: int = 500,
    chat_id: int = GROUP,
) -> CallbackQuery:
    """Somebody pressing a button, with the callback data it carries."""
    return CallbackQuery(
        id=press_id,
        from_user=user(user_id, name=name, username=None),
        chat_instance=f"instance-{chat_id}",
        data=data,
        message=_message(message_id=message_id, chat_id=chat_id, text="…"),
    )


def update(
    *, message: Message | None = None, callback_query: CallbackQuery | None = None
) -> Update:
    """Wrap a message or a press as the update a dispatcher is fed."""
    return Update(update_id=1, message=message, callback_query=callback_query)


# -------------------------------------------------------------- the harness


@dataclass(slots=True)
class BotHarness:
    """A dispatcher, a gateway, a registry and a store, wired as the bot wires them."""

    clock: FakeClock
    sends_clock: RecordingClock
    client: FakeTelegram
    store: FakeDatabase
    gateway: TelegramGateway
    registry: FlipRegistry
    dispatcher: Dispatcher
    bot: Bot
    group_id: int = GROUP
    presses: int = field(default=0)

    @property
    def target(self) -> MessageTarget:
        """The group, without a topic."""
        return MessageTarget(group_id=self.group_id)

    @property
    def waits(self) -> tuple[float, ...]:
        """Every wait the gateway made, in order: flood control and retry pauses."""
        return tuple(self.sends_clock.sleeps)

    def press_id(self) -> str:
        """A fresh press id, so every answer can be told apart."""
        self.presses += 1
        return f"press-{self.presses}"

    async def feed(
        self, *, message: Message | None = None, callback_query: CallbackQuery | None = None
    ) -> None:
        """Put one update through the dispatcher, then let the engine catch up."""
        await self.dispatcher.feed_update(
            self.bot, update(message=message, callback_query=callback_query)
        )
        await self.clock.quiesce()

    async def flip_command(
        self, text: str = "/flip 50 5 2 3", *, topic_id: int | None = None
    ) -> None:
        """Send ``/flip``, in a topic or not."""
        if topic_id is None:
            await self.feed(message=group_message(text))
        else:
            await self.feed(message=topic_message(text, topic_id=topic_id))

    async def close(self) -> None:
        """Let go of everything the harness opened."""
        self.registry.close()
        await self.store.close()
        await self.bot.session.close()


def telegram_gateway(
    *,
    retry_delay: float = 0.0,
    attempts: int = SEND_ATTEMPTS,
    flood_retries: int = FLOOD_RETRIES,
    on_migrated: MigrationHandler | None = None,
) -> tuple[FakeTelegram, RecordingClock, TelegramGateway]:
    """A gateway on a fake client and a :class:`RecordingClock`, for its own tests.

    ``attempts`` and ``flood_retries`` are the retry policy, for a test that
    wants a shorter budget than the bot's, and ``on_migrated`` is told about a
    group whose id has changed, which the bot wires to the registry.
    """
    client = FakeTelegram()
    clock = RecordingClock()
    gateway = TelegramGateway(
        client,
        clock=clock,
        retry_delay=retry_delay,
        attempts=attempts,
        flood_retries=flood_retries,
        on_migrated=on_migrated,
    )
    return client, clock, gateway


def bot_harness(
    *, group_id: int = GROUP, retry_delay: float = 0.0, client: FakeTelegram | None = None
) -> BotHarness:
    """A fresh harness, wired as :func:`esgbot.telegram.app.run_bot` wires the bot.

    The engine runs on a :class:`~tests.engine_helpers.FakeClock` the test drives;
    the gateway runs on a :class:`RecordingClock`, so a flood-control wait inside
    a handler does not deadlock the test that is awaiting it. ``retry_delay`` is
    zero, so the only waits recorded are the ones Telegram asked for.

    ``client`` takes a :class:`FakeTelegram` of the test's own, for the cases the
    queue and the deleted set cannot express -- a send that hangs part way
    through a round, say, which is how a press arriving mid-publication is
    tested.
    """
    clock = FakeClock()
    sends_clock = RecordingClock()
    client = FakeTelegram() if client is None else client
    store = FakeDatabase()
    gateway = TelegramGateway(client, clock=sends_clock, retry_delay=retry_delay)
    registry = FlipRegistry(gateway=gateway, store=store, clock=clock, settle=settle_inline)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(registry=registry, gateway=gateway, store=store))
    return BotHarness(
        clock=clock,
        sends_clock=sends_clock,
        client=client,
        store=store,
        gateway=gateway,
        registry=registry,
        dispatcher=dispatcher,
        bot=Bot(token=BOT_TOKEN),
        group_id=group_id,
    )


def active_row(
    *,
    token: str = "leftover",
    group_id: int = GROUP,
    topic_id: int | None = None,
    message_id: int = 500,
    running: bool = False,
    seed: bytes | None = None,
    created_at: datetime | None = None,
) -> ActiveFlip:
    """One leftover row, as the startup sweep finds it.

    ``running=True`` gives it the seed a started flip has, which is what the void
    notice reveals; a lobby row has none.
    """
    return ActiveFlip(
        token=token,
        group_id=group_id,
        topic_id=topic_id,
        message_id=message_id,
        state=FlipState.RUNNING if running else FlipState.LOBBY,
        seed=(seed if seed is not None else bytes(range(32))) if running else None,
        created_at=_EPOCH if created_at is None else created_at,
    )


DEFAULT_SETTINGS: Final = SETTINGS
"""The settings the helpers open flips with: ``/flip 50 5 2 3``."""

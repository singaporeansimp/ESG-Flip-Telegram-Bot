"""The real gateway: the engine's view data, out through Telegram.

:class:`TelegramGateway` is what :mod:`esgbot.engine.gateway` describes. It
renders a view with :mod:`esgbot.telegram.render`, hangs the right buttons off it
with :mod:`esgbot.telegram.keyboards`, and sends it to the group -- and to the
forum topic, when the flip lives in one, which is why every call takes a
:class:`~esgbot.engine.MessageTarget` rather than a chat id.

Five things Telegram does that it has to handle, and the engine does not:

* **"Wait."** A :class:`~aiogram.exceptions.TelegramRetryAfter` is not a failure.
  The gateway sleeps for exactly as long as it was told and makes the same call
  again. Waiting does not use up an attempt, though
  :data:`FLOOD_RETRIES` waits in a row for one call is taken as the group
  being closed to the bot for good. In a row means exactly that: any other
  failure in between starts the count again, because the call is then being
  refused rather than paced.
* **"Not modified."** An edit Telegram considers identical is a success. The
  engine redraws the table on a schedule and sometimes nothing has changed.
* **"No such message."** Somebody deleted the table. The gateway posts a fresh
  one and returns its id, and the engine follows it from then on and rewrites the
  flip's row.
* **"That group is a supergroup now."** The group has not gone anywhere; its id
  has changed, which Telegram reports as the answer to the call that was made.
  The gateway writes the new id down, makes the same call there -- which costs
  neither an attempt nor a wait -- and tells the bot, which moves the group's
  open flip and its rows exactly as the migration service message does. So a
  flip whose group is upgraded mid-round carries on in the same chat instead of
  being voided for a failure that never happened.
* **Anything else.** Counted, and after :data:`SEND_ATTEMPTS` attempts at the
  same call it raises :class:`~esgbot.engine.PermanentFailureError`, which is how
  the engine learns the flip cannot be shown any more and voids it.

A button press is answered once and never retried: a callback answer is only
valid for a few seconds, so a failure there is logged and dropped, and
:meth:`TelegramGateway.answer_press` says whether the pop-up went out. The flip
carries on without it, as :meth:`~esgbot.engine.Gateway.answer_press` says,
though a Peek that never arrived does not count as a peek.

Nothing here logs the token or a hand. The Peek pop-up's text never reaches a
log line, only the id of the press it answered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, Protocol, TypeVar, runtime_checkable

from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardMarkup

from esgbot.engine import (
    Clock,
    FlipView,
    MessageTarget,
    PeekAnswer,
    PermanentFailureError,
    PressAnswer,
    SystemClock,
)
from esgbot.logging_setup import get_logger
from esgbot.telegram import keyboards, render

__all__ = [
    "FLOOD_RETRIES",
    "MIGRATIONS_FOLLOWED",
    "RETRY_DELAY_SECONDS",
    "SEND_ATTEMPTS",
    "MigrationHandler",
    "SentMessage",
    "TelegramClient",
    "TelegramGateway",
]

_logger = get_logger(__name__)

SEND_ATTEMPTS: Final = 3
"""Attempts at one send or edit before the flip is given up on."""

RETRY_DELAY_SECONDS: Final = 1.0
"""Between two attempts at the same call, so a blip is not hammered at."""

FLOOD_RETRIES: Final = 5
"""Consecutive "wait and retry" rounds on one call before it is given up on.

Telegram's own waits are honoured however long they are; this only stops a call
that is told to wait over and over from waiting for ever. Any other failure in
between breaks the run, and the count starts again from there.
"""

MIGRATIONS_FOLLOWED: Final = 2
"""New group ids one call follows before it gives up.

A group becomes a supergroup once, so one is the number this ever needs. The
limit is here so that a server answering every attempt with a new id cannot keep
a call going round for ever, since following an id costs neither an attempt nor a
wait.
"""

MigrationHandler = Callable[[int, int], Awaitable[None]]
"""Told that a group's id has changed: the old id first, then the new one.

:func:`esgbot.telegram.handlers.build_router` wires this to the same move the
migration service message makes, so whichever arrives first does the work.
"""

_T = TypeVar("_T")

_NOT_MODIFIED: Final = "message is not modified"
_GONE: Final = (
    "message to edit not found",
    "message to be edited not found",
    "message can't be edited",
    "message_id_invalid",
    "message identifier is not specified",
)


@runtime_checkable
class SentMessage(Protocol):
    """A message Telegram accepted. :class:`aiogram.types.Message` is one."""

    @property
    def message_id(self) -> int:
        """The id the message landed on."""
        ...


@runtime_checkable
class TelegramClient(Protocol):
    """The four calls this gateway makes. :class:`aiogram.Bot` satisfies it.

    Taking the narrow type rather than the whole bot is what lets the gateway's
    tests hand it a client that raises on cue, and keeps what the bot layer
    actually touches written down in one place.
    """

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> SentMessage: ...

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> SentMessage | bool: ...

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> SentMessage | bool: ...

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool: ...


class _MessageGoneError(Exception):
    """The message an edit was for is not there any more. Internal to this module."""


def _describes(error: TelegramBadRequest, needles: tuple[str, ...]) -> bool:
    """Whether Telegram's complaint is one of these, whatever case it came in."""
    message = error.message.lower()
    return any(needle in message for needle in needles)


class TelegramGateway:
    """Output for the engine, and the one place Telegram's refusals are handled."""

    __slots__ = (
        "_attempts",
        "_client",
        "_clock",
        "_floods",
        "_moved",
        "_on_migrated",
        "_retry_delay",
    )

    def __init__(
        self,
        client: TelegramClient,
        *,
        clock: Clock | None = None,
        attempts: int = SEND_ATTEMPTS,
        flood_retries: int = FLOOD_RETRIES,
        retry_delay: float = RETRY_DELAY_SECONDS,
        on_migrated: MigrationHandler | None = None,
    ) -> None:
        """Wrap a client.

        ``clock`` is where both kinds of waiting happen, so a test can drive a
        flood-control pause without one passing in real time; it defaults to the
        real clock. The three numbers are the retry policy, and the defaults are
        what the bot runs. ``on_migrated`` is usually set later with
        :meth:`follow_migrations`, because what it does needs the registry.

        Raises:
            ValueError: if ``attempts`` or ``flood_retries`` is below one.
        """
        if attempts < 1:
            raise ValueError(f"a call needs at least one attempt, got {attempts}")
        if flood_retries < 1:
            raise ValueError(f"flood_retries must be at least one, got {flood_retries}")
        self._client = client
        self._clock: Clock = SystemClock() if clock is None else clock
        self._attempts = attempts
        self._floods = flood_retries
        self._retry_delay = retry_delay
        self._on_migrated = on_migrated
        self._moved: dict[int, int] = {}

    def follow_migrations(self, handler: MigrationHandler) -> None:
        """Say who to tell when Telegram reports that a group has a new id.

        Set after the registry exists, because moving a group is about the flip
        it has open as much as about its rows. Without a handler the gateway
        still reaches the group under its new id; what it cannot do is get that
        id written to the flip's own row, so a restart would send that flip's
        void notice to the chat the group has left.
        """
        self._on_migrated = handler

    # ------------------------------------------------------------- the gateway

    async def post(self, target: MessageTarget, view: FlipView) -> int:
        """Post a flip's message and return its id.

        Raises:
            PermanentFailureError: if it cannot be posted at all.
        """
        return await self.send(target, render.render(view), markup=keyboards.markup_for(view))

    async def edit(self, target: MessageTarget, message_id: int, view: FlipView) -> int:
        """Show a view on the flip's message, and say which message now holds it.

        That is ``message_id``, unless the message has been deleted, in which case
        a fresh one is posted and its id comes back instead.

        Raises:
            PermanentFailureError: if the view cannot be shown at all.
        """
        text = render.render(view)
        markup = keyboards.markup_for(view)

        async def attempt(chat_id: int) -> int:
            try:
                await self._client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except TelegramBadRequest as error:
                if _describes(error, (_NOT_MODIFIED,)):
                    return message_id
                if _describes(error, _GONE):
                    raise _MessageGoneError(error.message) from error
                raise
            except TelegramNotFound as error:
                raise _MessageGoneError(str(error)) from error
            return message_id

        try:
            return await self._retry(
                attempt, group_id=target.group_id, what=f"editing message {message_id}"
            )
        except _MessageGoneError as gone:
            _logger.info(
                "message %d in group %d is gone (%s); posting a fresh one",
                message_id,
                target.group_id,
                gone,
            )
            return await self.post(target, view)

    async def answer_press(self, press_id: str, answer: PressAnswer) -> bool:
        """Answer a button press with a private pop-up, and never raise.

        A Peek gets the alert form, which stays until it is dismissed, because it
        is the player's hand. Everything else gets the brief toast.

        A pop-up Telegram will not take is logged and dropped: the answer is only
        good for a few seconds, so there is nothing to retry, and one lost pop-up
        never costs a flip.

        Returns:
            Whether Telegram took it. The engine counts a Peek as a peek only
            when the player was really shown their hand.
        """
        if isinstance(answer, PeekAnswer):
            text, alert = render.render_peek(answer), True
        else:
            text, alert = render.render_notice(answer), False
        try:
            await self._client.answer_callback_query(press_id, text=text, show_alert=alert)
        except TelegramAPIError as error:
            _logger.info("press %s went unanswered: %s", press_id, error)
            return False
        return True

    # ---------------------------------------------------------- the bot's own

    async def send(
        self,
        target: MessageTarget,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
    ) -> int:
        """Send already-rendered HTML to the group, and to its topic if it has one.

        Raises:
            PermanentFailureError: if it cannot be sent at all.
        """

        async def attempt(chat_id: int) -> int:
            sent = await self._client.send_message(
                chat_id=chat_id,
                message_thread_id=target.topic_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return sent.message_id

        return await self._retry(
            attempt, group_id=target.group_id, what=f"sending to group {target.group_id}"
        )

    async def say(self, target: MessageTarget, text: str) -> None:
        """Reply to a command, where there is nothing to do if the reply fails.

        The usage line, a refusal, a complaint about settings: all courtesies, and
        a group the bot cannot post in does not need to hear about it twice.
        """
        try:
            await self.send(target, text)
        except PermanentFailureError:
            _logger.warning("could not reply in group %d", target.group_id)

    async def remove_buttons(self, target: MessageTarget, message_id: int) -> None:
        """Take the buttons off a message, leaving what it says alone.

        This is how the startup sweep closes a lobby or table it has no other
        memory of: the row says which message it was, and nothing else about the
        flip is known well enough to rewrite its text.

        A message that is already bare, or gone altogether, is nothing to worry
        about.

        Raises:
            PermanentFailureError: if the group cannot be reached at all.
        """

        async def attempt(chat_id: int) -> None:
            try:
                await self._client.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except TelegramBadRequest as error:
                if _describes(error, (_NOT_MODIFIED, *_GONE)):
                    return
                raise
            except TelegramNotFound:
                return

        await self._retry(
            attempt,
            group_id=target.group_id,
            what=f"clearing the buttons on message {message_id}",
        )

    # ------------------------------------------------------------------ retries

    def chat_id_for(self, group_id: int) -> int:
        """The chat id this group is known by now, following any move it has made.

        The engine's groups are the ids their flips were created with, and
        Telegram changes a group's id when it becomes a supergroup. Every call
        goes to the id this returns, so the one message that was in flight when
        the move happened reaches the group as well as the ones that follow it.
        """
        chat_id = group_id
        # Bounded by the number of moves known, so a pair of ids pointing at each
        # other cannot loop.
        for _ in range(len(self._moved)):
            moved = self._moved.get(chat_id)
            if moved is None:
                break
            chat_id = moved
        return chat_id

    async def _retry(
        self, attempt: Callable[[int], Awaitable[_T]], *, group_id: int, what: str
    ) -> _T:
        """Make a call, honouring Telegram's waits, following its moves, counting failures.

        The call is made against the chat id the group is known by now, which is
        not the id it was created with once Telegram has made it a supergroup.

        Raises:
            PermanentFailureError: once the call has failed :data:`SEND_ATTEMPTS`
                times, has been told to wait :data:`FLOOD_RETRIES` times in a
                row, or has been sent after more than :data:`MIGRATIONS_FOLLOWED`
                new group ids.
        """
        chat_id = self.chat_id_for(group_id)
        failures = 0
        floods = 0
        migrations = 0
        while True:
            try:
                return await attempt(chat_id)
            except TelegramMigrateToChat as error:
                # Not a failure and not a wait: the group is still there under a
                # new id, and the same call made there is the call that was
                # wanted all along. So it costs neither an attempt nor a retry.
                migrations += 1
                if migrations > MIGRATIONS_FOLLOWED:
                    raise PermanentFailureError(
                        f"{what} was moved to a new group {migrations} times"
                    ) from error
                chat_id = await self._follow(chat_id, error.migrate_to_chat_id, what=what)
            except TelegramRetryAfter as error:
                floods += 1
                if floods > self._floods:
                    raise PermanentFailureError(
                        f"{what} was asked to wait {floods} times in a row"
                    ) from error
                _logger.info(
                    "flood control: waiting %s seconds before retrying %s",
                    error.retry_after,
                    what,
                )
                await self._clock.sleep(error.retry_after)
            except TelegramAPIError as error:
                failures += 1
                # The run of waits is over: this call is being refused rather
                # than paced, so the next wait starts a fresh run.
                floods = 0
                _logger.warning(
                    "%s failed (attempt %d of %d): %s", what, failures, self._attempts, error
                )
                if failures >= self._attempts:
                    raise PermanentFailureError(f"{what} failed {failures} times") from error
                await self._clock.sleep(self._retry_delay)

    async def _follow(self, chat_id: int, new_chat_id: int, *, what: str) -> int:
        """Write down a group's new id and tell the bot about it. Returns the new id.

        The handler does what the migration service message does -- move the
        group's open flip and its rows -- and doing it twice does nothing the
        second time, so it does not matter which of the two gets there first. A
        handler that fails costs the group its history or its row, not this
        message, so it is logged and the call carries on to the new id.
        """
        _logger.info("group %d is now %d; %s goes there instead", chat_id, new_chat_id, what)
        self._moved[chat_id] = new_chat_id
        handler = self._on_migrated
        if handler is None:
            # Nothing but this gateway will ever know: the group's rows stay
            # under an id it no longer answers to, and this alias hides that
            # from every later send. The bot installs the handler before it
            # makes its first call, so this is a wiring mistake, not a case.
            _logger.error(
                "group %d became %d with nobody to record it; its rows stay on the old id",
                chat_id,
                new_chat_id,
            )
            return new_chat_id
        try:
            await handler(chat_id, new_chat_id)
        except Exception:
            _logger.exception("group %d became %d but following it up failed", chat_id, new_chat_id)
        return new_chat_id

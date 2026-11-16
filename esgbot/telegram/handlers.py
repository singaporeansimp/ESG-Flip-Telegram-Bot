"""The things that arrive from Telegram: a command, a press, a migration.

``/flip`` reads its settings with the game's own parser, so the limits and the
complaints are the ones written down in :mod:`esgbot.game.rules`, and opens a
lobby through the registry, which refuses a second flip in a group that already
has one. Only the wager and ``X`` have to be given: what is left off the end of
the command is what the bot fills in, and the flip carries it as a
:class:`~esgbot.game.rules.FlipRequest` until Start settles it.

``/again`` is that same command again, as it was typed: it reads the settings of
the last flip the group *finished* -- storage keeps one row per group for exactly
this -- and opens a lobby with them, so a value that was left out is left out
again and settles on this table rather than on the last one. Both commands open
their lobby through the same call, which is what makes every rule about starting
a flip hold for either of them.

``/leaderboard``, ``/stats`` and ``/verify`` read finished flips back out. They
are all scoped to the group the command was sent in: a flip number or a player
from another group is simply not there. ``/stats`` is about a ``@name`` given in
the command, else whoever the command replied to, else the person asking -- in
that order, because a name typed out is a clearer request than the message it
hangs under.
``/verify`` hands the stored flip to :func:`esgbot.verify.verify_flip` in a
worker thread, since re-running the evaluator is the one piece of real work a
command does.

A press carries the flip's token in its callback data. The token is looked up in
the registry and the press handed to the flip, which answers it and then shows
whatever changed. A token the registry does not know belongs to a flip from
before a restart, or one that has since finished, and is answered as expired --
which is also the answer to callback data this bot did not write.

The migration event is Telegram saying a group has become a supergroup under a
new id. The registry moves whatever flip is open to the new id so the round goes
on in the same chat, and storage rewrites the id so the group keeps its history.
That same move is what the gateway asks for when a message it was sending comes
back refused with the group's new id, so it is written once, in
:func:`follow_group_move`, and doing it twice does nothing the second time.

**Topics.** A flip in a forum group lives in one topic: the one ``/flip`` was
sent in. Everything the flip says goes there, which is why the topic is read off
the command message once and carried on the flip, and why replies are sent
through the gateway rather than with :meth:`aiogram.types.Message.answer`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Final, Protocol, runtime_checkable

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, User

from esgbot.engine import (
    FlipAlreadyOpenError,
    FlipRegistry,
    MessageTarget,
    Notice,
    PermanentFailureError,
    Player,
)
from esgbot.game.rules import FlipRequest, SettingsError, parse_flip_request
from esgbot.logging_setup import get_logger
from esgbot.storage import (
    DEFAULT_LEADERBOARD_LIMIT,
    CorruptRecordError,
    GroupMigration,
    PlayerStats,
    StorageError,
    StoredFlip,
)
from esgbot.telegram import render
from esgbot.telegram.gateway import TelegramGateway
from esgbot.telegram.keyboards import Press, parse
from esgbot.verify import verify_flip

__all__ = ["CommandStore", "MigrationStore", "build_router", "follow_group_move"]

_logger = get_logger(__name__)

_GROUP_CHATS = frozenset({"group", "supergroup"})

_DIGITS: Final = frozenset("0123456789")

_FLIP_NUMBER_DIGITS: Final = 18
"""Digits a flip number may have. Longer is not a flip number anybody has, and
handing sqlite an integer it cannot hold would be an error rather than a miss."""


@runtime_checkable
class MigrationStore(Protocol):
    """The group-id change, on its own. :class:`esgbot.storage.Database` is one."""

    async def migrate_group_id(self, *, old_group_id: int, new_group_id: int) -> GroupMigration:
        """Move a group's history and open flips to the id Telegram now uses."""
        ...


@runtime_checkable
class CommandStore(MigrationStore, Protocol):
    """Every storage call the handlers make. :class:`esgbot.storage.Database` is one.

    The reads are the three commands that look at finished flips, and every one
    of them takes the group, because stats and flip numbers never cross groups.
    """

    async def leaderboard(
        self, group_id: int, *, limit: int | None = DEFAULT_LEADERBOARD_LIMIT
    ) -> tuple[PlayerStats, ...]:
        """One group's players ranked by net winnings, then wins, then name."""
        ...

    async def player_stats(self, *, group_id: int, user_id: int) -> PlayerStats | None:
        """One player's figures in one group, or ``None`` if they never played there."""
        ...

    async def player_stats_by_username(self, *, group_id: int, username: str) -> PlayerStats | None:
        """The same, found by the username the player last used in this group."""
        ...

    async def fetch_flip(self, *, group_id: int, flip_number: int) -> StoredFlip | None:
        """One finished flip with its players and stored deal, or ``None``."""
        ...

    async def last_flip_request(self, group_id: int) -> FlipRequest | None:
        """What the last flip this group finished was asked for, or ``None``."""
        ...


def _display_name(user: User) -> str:
    """What the group sees this user called, falling back to their id."""
    return user.full_name or str(user.id)


def _player(user: User) -> Player:
    """A Telegram user, as the flip knows them.

    ``full_name`` is what the group sees. The username is kept too, for
    ``/stats @name`` to find them by later.
    """
    return Player(
        user_id=user.id,
        display_name=_display_name(user),
        username=user.username,
    )


def _target(message: Message) -> MessageTarget:
    """Where this message's flip belongs: the chat, and the topic if it is in one.

    ``message_thread_id`` is also set on a plain reply thread, so
    ``is_topic_message`` is what tells a forum topic from one.
    """
    return MessageTarget(
        group_id=message.chat.id,
        topic_id=message.message_thread_id if message.is_topic_message else None,
    )


def _in_group(message: Message) -> bool:
    """Whether this arrived in a group, which is the only place a flip happens."""
    return message.chat.type in _GROUP_CHATS


def _flip_number(args: str | None) -> int | None:
    """The flip number ``/verify`` was given, or ``None`` if it was not a number.

    Digits only, as settings are: a flip number is what the bot printed in a
    result message, so there is nothing to guess at.
    """
    text = (args or "").strip()
    if not text or not _DIGITS.issuperset(text) or len(text) > _FLIP_NUMBER_DIGITS:
        return None
    number = int(text)
    return number if number >= 1 else None


async def follow_group_move(
    *, registry: FlipRegistry, store: MigrationStore, old_group_id: int, new_group_id: int
) -> None:
    """Move a group to the id Telegram now uses: its open flip first, then its rows.

    The open flip goes first, because its very next message has to go to the new
    id, and it takes its own active-flip row along: that row has to name the chat
    the flip is in whatever happens next. The group's history follows, and a
    storage failure there costs the group its history rather than its flip -- so
    the count logged at the end is of what that rewrite moved, the live flip's
    row having already gone with the flip.

    Two things tell the bot about a move: the service message Telegram sends to
    the group, and a call the gateway makes being refused with the new id. Both
    come through here, and whichever is second finds nothing left to do -- the
    registry already knows the old id is this group's, and the rewrite moves no
    rows -- so it does not matter which arrives first.
    """
    await registry.migrate_group(old_group_id=old_group_id, new_group_id=new_group_id)
    try:
        moved = await store.migrate_group_id(old_group_id=old_group_id, new_group_id=new_group_id)
    except (StorageError, sqlite3.Error) as error:
        _logger.error(
            "group %d became %d but its rows could not be moved: %s",
            old_group_id,
            new_group_id,
            error,
        )
        return
    _logger.info(
        "group %d became %d: moved %d flip(s) and %d open flip row(s)",
        old_group_id,
        new_group_id,
        moved.flips,
        moved.active_flips,
    )


def build_router(
    *,
    registry: FlipRegistry,
    gateway: TelegramGateway,
    store: CommandStore,
) -> Router:
    """A router wired to this process's registry, gateway and database.

    This is also where the gateway is told who to tell about a group that has
    become a supergroup, since that move needs the same registry and store the
    service message's handler uses: see :func:`follow_group_move`.
    """
    router = Router(name="esgbot")

    async def group_moved(old_group_id: int, new_group_id: int) -> None:
        await follow_group_move(
            registry=registry,
            store=store,
            old_group_id=old_group_id,
            new_group_id=new_group_id,
        )

    gateway.follow_migrations(group_moved)

    async def open_lobby(target: MessageTarget, user: User, request: FlipRequest) -> None:
        """Open a lobby for this request, and say so when it cannot be opened.

        Both commands that start a flip end up here, so every rule about
        starting one -- one flip at a time per group, the topic it belongs to,
        what a group is told when the lobby will not post -- is written once and
        holds for ``/again`` exactly as it does for ``/flip``.
        """
        try:
            await registry.create_flip(
                group_id=target.group_id,
                topic_id=target.topic_id,
                starter=_player(user),
                settings=request,
            )
        except FlipAlreadyOpenError as already:
            await gateway.say(
                target,
                render.render_already_open(already.flip.request, already.flip.settings),
            )
        except PermanentFailureError:
            # The lobby would not post, so there is nowhere to complain about it.
            _logger.warning(
                "could not open a flip in group %d: the lobby would not post", target.group_id
            )
        except (StorageError, sqlite3.Error) as error:
            _logger.error("could not open a flip in group %d: %s", target.group_id, error)
            await gateway.say(target, render.STORAGE_TROUBLE)

    @router.message(Command("flip"))
    async def open_flip(message: Message, command: CommandObject) -> None:
        """``/flip <wager> <private cards> [extra cards] [scoop multiplier]``: open a lobby.

        The wager and X are required; Y and Z are positional, so what is left
        off the end is what the bot fills in -- Y with the maximum, which the
        deal reads as as many extra cards as fit, and Z with the number of
        players, once Start says how many that is.
        """
        user = message.from_user
        if user is None:
            return
        target = _target(message)
        if not _in_group(message):
            await gateway.say(target, render.NO_FLIP_IN_PRIVATE)
            return
        try:
            request = parse_flip_request((command.args or "").split())
        except SettingsError as error:
            await gateway.say(target, render.render_settings_error(error))
            return
        await open_lobby(target, user, request)

    @router.message(Command("again"))
    async def flip_again(message: Message, command: CommandObject) -> None:
        """``/again``: the last flip this group finished, with the same settings.

        The same settings means the same command, not the same figures: a flip
        whose ``Z`` was left out is opened with ``Z`` left out again, so it
        settles on however many players turn up this time. Whoever types it
        opens the flip and is its first player; the last flip's players are not
        invited.

        Only a flip that was recorded counts as one this group has played, so a
        lobby that was cancelled or a round that was voided is not "the last
        flip" -- neither of them was ever written down.
        """
        user = message.from_user
        if user is None:
            return
        target = _target(message)
        if not _in_group(message):
            await gateway.say(target, render.NO_FLIP_IN_PRIVATE)
            return
        if command.args:
            # Settings given to /again would be settings it ignores, and a flip
            # quietly played at somebody else's wager is worth a sentence.
            await gateway.say(target, render.AGAIN_USAGE)
            return
        try:
            request = await store.last_flip_request(target.group_id)
        except (StorageError, sqlite3.Error) as error:
            _logger.error(
                "could not read group %d's last flip settings: %s", target.group_id, error
            )
            await gateway.say(target, render.STORAGE_TROUBLE)
            return
        if request is None:
            await gateway.say(target, render.NO_FLIP_TO_REPEAT)
            return
        await open_lobby(target, user, request)

    @router.message(Command("leaderboard"))
    async def show_leaderboard(message: Message) -> None:
        """``/leaderboard``: this group's players, best net first."""
        target = _target(message)
        if not _in_group(message):
            await gateway.say(target, render.NO_STATS_IN_PRIVATE)
            return
        try:
            rows = await store.leaderboard(target.group_id)
        except (StorageError, sqlite3.Error) as error:
            _logger.error("could not read group %d's leaderboard: %s", target.group_id, error)
            await gateway.say(target, render.STATS_TROUBLE)
            return
        await gateway.say(target, render.render_leaderboard(rows))

    @router.message(Command("stats"))
    async def show_stats(message: Message, command: CommandObject) -> None:
        """``/stats``, ``/stats @name``, or ``/stats`` as a reply to somebody.

        A name given in the command wins over a message replied to, and with
        neither it is the asker's own figures. ``subject`` holds the Telegram
        user the answer is about, and is ``None`` exactly when a name was given
        instead, because then there is nobody to look up -- only a name that
        some flip in this group was played under, or was not.
        """
        asker = message.from_user
        if asker is None:
            return
        target = _target(message)
        if not _in_group(message):
            await gateway.say(target, render.NO_STATS_IN_PRIVATE)
            return
        wanted = (command.args or "").split()
        if len(wanted) > 1:
            await gateway.say(target, render.STATS_USAGE)
            return
        subject: User | None = None
        if not wanted:
            replied = message.reply_to_message
            subject = asker if replied is None else replied.from_user
            if subject is None:
                # A reply to a post with no sender: a channel's, or one Telegram
                # will not name. Nothing to look anybody up by.
                await gateway.say(target, render.STATS_USAGE)
                return
        try:
            stats = (
                await store.player_stats(group_id=target.group_id, user_id=subject.id)
                if subject is not None
                else await store.player_stats_by_username(
                    group_id=target.group_id, username=wanted[0]
                )
            )
        except (StorageError, sqlite3.Error) as error:
            _logger.error("could not read stats in group %d: %s", target.group_id, error)
            await gateway.say(target, render.STATS_TROUBLE)
            return
        if stats is not None:
            await gateway.say(target, render.render_stats(stats))
        elif subject is None:
            await gateway.say(target, render.render_unknown_player(wanted[0]))
        elif subject.id == asker.id:
            await gateway.say(target, render.NO_FLIPS_OF_YOUR_OWN)
        else:
            await gateway.say(target, render.render_no_stats_for(_display_name(subject)))

    @router.message(Command("verify"))
    async def verify_recorded_flip(message: Message, command: CommandObject) -> None:
        """``/verify <flip number>``: re-run a finished flip against its seed.

        A flip that will not read back is a failed verification rather than a
        database problem: something has changed it since it was saved, which is
        exactly what this command is for finding out.
        """
        target = _target(message)
        if not _in_group(message):
            await gateway.say(target, render.NO_STATS_IN_PRIVATE)
            return
        number = _flip_number(command.args)
        if number is None:
            await gateway.say(target, render.VERIFY_USAGE)
            return
        try:
            stored = await store.fetch_flip(group_id=target.group_id, flip_number=number)
        except CorruptRecordError as damaged:
            _logger.warning(
                "flip %d in group %d cannot be read back: %s", number, target.group_id, damaged
            )
            await gateway.say(target, render.render_unreadable_flip(number))
            return
        except (StorageError, sqlite3.Error) as error:
            _logger.error("could not read flip %d in group %d: %s", number, target.group_id, error)
            await gateway.say(target, render.STATS_TROUBLE)
            return
        if stored is None:
            await gateway.say(target, render.render_no_such_flip(number))
            return
        report = await asyncio.to_thread(verify_flip, stored)
        if not report.passed:
            _logger.warning(
                "flip %d in group %d does not verify (%s)",
                number,
                target.group_id,
                ", ".join(report.differences),
            )
        await gateway.say(target, render.render_verification(report))

    @router.message(F.migrate_to_chat_id)
    async def migrate_group(message: Message) -> None:
        """This group has become a supergroup: move its flip, then its rows.

        The work is :func:`follow_group_move`, which the gateway also reaches
        when a message it was sending is refused with the new id.
        """
        new_group_id = message.migrate_to_chat_id
        if new_group_id is None:
            return
        await group_moved(message.chat.id, new_group_id)

    @router.callback_query()
    async def press_button(query: CallbackQuery) -> None:
        """A button on a lobby or a table. The token says which flip it belongs to.

        Every press is answered, including one this bot cannot make sense of: an
        unanswered press leaves a spinner going on somebody's phone.
        """
        pressed = parse(query.data)
        flip = None if pressed is None else registry.flip_for_token(pressed.token)
        if pressed is None or flip is None:
            await gateway.answer_press(query.id, Notice.EXPIRED)
            return
        user = query.from_user
        match pressed.press:
            case Press.JOIN:
                await flip.join(_player(user), press_id=query.id)
            case Press.LEAVE:
                await flip.leave(user.id, press_id=query.id)
            case Press.START:
                await flip.start(user.id, press_id=query.id)
            case Press.CANCEL:
                await flip.cancel(user.id, press_id=query.id)
            case Press.PEEK:
                await flip.peek(user.id, press_id=query.id)

    return router

"""Starting the bot: open the database, void what was left over, then poll.

The order matters. The sweep runs before the first update is fetched, so a group
hears that its flip was voided before anybody can press a button on it, and the
presses that do arrive afterwards find a registry that has never heard of those
tokens. It runs *after* the router is built, though, because that is where the
gateway learns who to tell about a group whose id has changed: the sweep talks to
groups that may have become supergroups while the bot was down, and a move it
discovers has to reach storage like any other.

Long polling, and exactly one process. Two pollers on one token fight over
updates, which is why there is no second instance and no webhook: a single VPS
needs no domain, no TLS and no reverse proxy.

Shutting down leaves the open flips' rows behind on purpose. They are what the
next start finds, voids and tells each group about. One kind of flip has no row
to leave: one whose result was recorded as polling stopped, because recording a
flip retires its row in the same write. So stopping waits -- briefly, and in
real time -- for flips like that to say how they ended, and it waits before the
Telegram session is closed, because afterwards nothing can say anything.

Which is why the poller is told ``close_bot_session=False``. Left to itself it
closes the session in its own cleanup, before it returns here: the drain would
then be sending a result down a session that had just been pulled out from under
it, and a send that is already on the wire comes back as a disconnection rather
than a success -- so the gateway retries it and the group is told its result
twice. The session belongs to this function, which closes it once, last, after
everything that might still have something to say.
"""

from __future__ import annotations

from typing import Final

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from esgbot.engine import FlipRegistry
from esgbot.logging_setup import get_logger
from esgbot.storage import Database
from esgbot.telegram.gateway import TelegramGateway
from esgbot.telegram.handlers import build_router
from esgbot.telegram.settings import BotSettings
from esgbot.telegram.sweep import void_leftover_flips

__all__ = ["ALLOWED_UPDATES", "COMMANDS", "register_commands", "run_bot"]

_logger = get_logger(__name__)

ALLOWED_UPDATES: Final = ("message", "callback_query")
"""What the bot asks Telegram for: slash commands, button presses, and the
service message that says a group has become a supergroup. Everything else is
left on the server, which is also all the bot needs under Telegram's default
privacy mode."""

COMMANDS: Final = (
    BotCommand(
        command="flip",
        description="Start a flip: /flip <wager> <private cards> [extra cards] [scoop multiplier]",
    ),
    BotCommand(command="again", description="Repeat this group's last flip, same settings"),
    BotCommand(
        command="autojoin",
        description="Join flips here automatically: /autojoin <wager>, or /autojoin off",
    ),
    BotCommand(command="leaderboard", description="Top players in this group by net winnings"),
    BotCommand(command="stats", description="Your stats, or /stats @name, or reply to someone"),
    BotCommand(command="verify", description="Check a finished flip: /verify <flip number>"),
)
"""The slash commands Telegram offers when someone types ``/`` in a chat with the
bot. They are what the bot answers to, written down once, so the menu cannot
promise a command the handlers do not have.

The settings are named here as they are named everywhere else the bot speaks:
the menu is where somebody meets the command for the first time, and a menu that
said ``<X> [Y] [Z]`` would be the one place they had to learn the letters."""


async def register_commands(bot: Bot) -> None:
    """Tell Telegram which commands to list, and carry on if it will not.

    The menu is a convenience: every command works typed out in full, so a
    Telegram that refuses or cannot be reached here is no reason to stop the bot
    from starting. The failure is logged, and the next start tries again.
    """
    try:
        await bot.set_my_commands(list(COMMANDS))
    except TelegramAPIError as error:
        _logger.warning("could not register the command menu: %s", error)
    else:
        _logger.info("registered %d commands", len(COMMANDS))


async def run_bot(settings: BotSettings) -> None:
    """Run the bot until it is stopped, then close everything it opened.

    The bot is built first, because that is what checks the token is shaped like
    one, and there is no sense opening a database for a token Telegram will
    refuse. Everything opened after it is closed on the way out, whichever way
    that is, and each exactly once.
    """
    bot = Bot(token=settings.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        database = await Database.open(settings.db_path)
        try:
            gateway = TelegramGateway(bot)
            registry = FlipRegistry(gateway=gateway, store=database)
            dispatcher = Dispatcher()
            # The router is built before the sweep, not because anything is
            # routed yet -- polling starts further down -- but because building
            # it is what tells the gateway where to send a group that has become
            # a supergroup. The sweep is often the first thing to find that out,
            # from a group that was upgraded while the bot was down, and a move
            # nobody records leaves that group's history under an id it no
            # longer answers to.
            dispatcher.include_router(
                build_router(registry=registry, gateway=gateway, store=database)
            )
            await void_leftover_flips(store=database, gateway=gateway)
            await register_commands(bot)
            _logger.info("polling for updates")
            try:
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=list(ALLOWED_UPDATES),
                    # The poller closes the bot's session in its own cleanup
                    # unless told not to, which would put it beyond use before
                    # this function gets a word in. The session has to outlive
                    # the drain below, so closing it is this function's job.
                    close_bot_session=False,
                )
            finally:
                # Before the session closes, because a flip that was recording
                # its result when polling stopped still owes its group that
                # result, and this is the last moment anything can send it.
                await registry.shut_down()
        finally:
            await database.close()
    finally:
        # Last of all: the session is what a flip's final message goes out on,
        # so nothing that might still have something to say outlives it.
        await bot.session.close()
        _logger.info("stopped")

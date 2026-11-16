"""The Telegram half of the bot: commands, buttons, wording and output.

The engine knows the rules of a flip and no text at all; this package is
everything else. It is deliberately in layers, so the parts that need Telegram
are small and the parts that need testing are pure:

=====================================  ========================================================
:mod:`~esgbot.telegram.settings`       The token and the database path, from the environment.
:mod:`~esgbot.telegram.render`         View data in, HTML out. Pure, and where every word lives.
:mod:`~esgbot.telegram.keyboards`      The buttons, and the token their callback data carries.
:mod:`~esgbot.telegram.gateway`        :class:`TelegramGateway`: the engine's output, for real,
                                       with Telegram's waits and refusals handled.
:mod:`~esgbot.telegram.handlers`       ``/flip``, the three commands that read flips back,
                                       button presses and the supergroup migration.
:mod:`~esgbot.telegram.sweep`          Voiding the flips that were open when the bot last stopped.
:mod:`~esgbot.telegram.app`            Wiring it together and long-polling.
=====================================  ========================================================

Two rules hold throughout. Messages go out in HTML parse mode with every
user-supplied name escaped, which :mod:`~esgbot.telegram.render` does and nothing
else has to think about. And a flip in a forum group lives in one topic: the
topic is read off the ``/flip`` command and carried on the flip, so every message
it posts lands in the same place.
"""

from __future__ import annotations

from esgbot.telegram.app import run_bot
from esgbot.telegram.gateway import TelegramGateway
from esgbot.telegram.handlers import build_router
from esgbot.telegram.settings import BotSettings, ConfigError, load_settings
from esgbot.telegram.sweep import void_leftover_flips

__all__ = [
    "BotSettings",
    "ConfigError",
    "TelegramGateway",
    "build_router",
    "load_settings",
    "run_bot",
    "void_leftover_flips",
]

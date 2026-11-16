"""Starting up: what has to have happened before the first update is fetched.

Two claims worth pinning, and both are about order. The startup sweep must run
before polling begins, or a group could press a button on a flip the new process
has already decided to void. And a process with no token must stop with a word
about which variable to set, rather than a traceback or, worse, a bot that starts
and then fails somewhere less obvious.

The real :class:`aiogram.Bot`, :class:`aiogram.Dispatcher` and
:class:`~esgbot.storage.Database` are swapped out here, because what is being
tested is the wiring between them rather than any of the three.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import socket
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.methods import EditMessageReplyMarkup, SetMyCommands
from aiogram.types import BotCommand
from aiohttp import web

from esgbot.engine import Flip, FlipRegistry, timing
from esgbot.game.fairness import seed_to_hex
from esgbot.logging_setup import get_logger
from esgbot.storage import Database, FinishedFlip, queries
from esgbot.telegram import app
from esgbot.telegram.gateway import TelegramGateway
from esgbot.telegram.settings import TOKEN_ENV_VAR, BotSettings
from esgbot.telegram.sweep import void_leftover_flips
from tests.engine_helpers import SETTINGS, FakeClock, player, settle_inline
from tests.storage_helpers import Line, make_flip
from tests.telegram_helpers import GROUP, FakeDatabase, FakeTelegram, active_row

_TOKEN = "123456:secret-token-value"
_DB_PATH = "/data/esg.sqlite3"


class _Database(FakeDatabase):
    """The store, with the two calls ``run_bot`` makes on it beyond the sweep."""

    __slots__ = ("closed", "opened_path")

    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.opened_path = ""

    async def close(self) -> None:
        self.closed = True


class _Session:
    """The one thing ``run_bot`` asks of a bot besides the four client calls."""

    __slots__ = ("closed", "closes")

    def __init__(self) -> None:
        self.closed = False
        self.closes = 0

    async def close(self) -> None:
        self.closed = True
        self.closes += 1


class _WatchedSession(_Session):
    """A session that writes down how much had been sent when it was closed."""

    __slots__ = ("_bot", "calls_at_close")

    def __init__(self, bot: FakeTelegram) -> None:
        super().__init__()
        self._bot = bot
        self.calls_at_close = -1

    async def close(self) -> None:
        self.calls_at_close = len(self._bot.calls)
        await super().close()


class _Bot(FakeTelegram):
    """A bot shaped like the real one: the client calls, plus a session to close."""

    __slots__ = ("command_failure", "commands", "session", "token")

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token
        self.session = _Session()
        self.commands: list[BotCommand] = []
        self.command_failure: BaseException | None = None

    async def set_my_commands(self, commands: list[BotCommand]) -> bool:
        if self.command_failure is not None:
            raise self.command_failure
        self.commands = commands
        return True


class _Dispatcher:
    """A dispatcher that writes down when it was told to start, and stops at once.

    It closes the bot's session on the way out, because
    :meth:`aiogram.Dispatcher.start_polling` does: that is its default, it
    happens in its own cleanup before it returns, and a fake that left it out
    would hide the one thing ``run_bot`` has to ask it not to do. ``True`` here
    would put the session beyond use before the drain could send anything.
    """

    __slots__ = (
        "allowed_updates",
        "calls_before_polling",
        "close_bot_session",
        "closed_the_session",
        "routers",
    )

    def __init__(self) -> None:
        self.routers: list[object] = []
        self.allowed_updates: list[str] = []
        self.calls_before_polling = -1
        self.close_bot_session = True
        self.closed_the_session = False

    def include_router(self, router: object) -> None:
        self.routers.append(router)

    async def start_polling(
        self,
        bot: _Bot,
        *,
        allowed_updates: list[str],
        close_bot_session: bool = True,
        **_: object,
    ) -> None:
        self.allowed_updates = allowed_updates
        self.calls_before_polling = len(bot.calls)
        self.close_bot_session = close_bot_session
        try:
            await self._poll(bot)
        finally:
            if close_bot_session:
                self.closed_the_session = True
                await bot.session.close()

    async def _poll(self, bot: _Bot) -> None:
        """What happens while the bot is polling. Nothing, unless a test says so."""


@dataclass(slots=True)
class _Started:
    """What one run of ``run_bot`` leaves behind for a test to read."""

    database: _Database
    bot: _Bot
    dispatcher: _Dispatcher


class _Starter:
    """``run_bot`` with its bot, dispatcher and database stubbed out.

    Fill :attr:`database` with whatever the last run should have left behind,
    then :meth:`run` it and read what came of it.
    """

    __slots__ = ("_bots", "_dispatchers", "command_failure", "database")

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.database = _Database()
        self._bots: list[_Bot] = []
        self._dispatchers: list[_Dispatcher] = []
        self.command_failure: BaseException | None = None
        opener = self._opener()
        monkeypatch.setattr("esgbot.telegram.app.Database", opener)
        monkeypatch.setattr("esgbot.telegram.app.Bot", self._make_bot)
        monkeypatch.setattr("esgbot.telegram.app.Dispatcher", self._make_dispatcher)
        monkeypatch.setattr("esgbot.telegram.app.DefaultBotProperties", lambda **_: None)

    def _opener(self) -> type:
        database = self.database

        class _Opener:
            @staticmethod
            async def open(path: str | PathLike[str]) -> _Database:
                database.opened_path = str(path)
                return database

        return _Opener

    def _make_bot(self, *, token: str, **_: object) -> _Bot:
        bot = _Bot(token)
        bot.command_failure = self.command_failure
        self._bots.append(bot)
        return bot

    def _make_dispatcher(self, *_: object, **__: object) -> _Dispatcher:
        dispatcher = _Dispatcher()
        self._dispatchers.append(dispatcher)
        return dispatcher

    def run(self, db_path: str = _DB_PATH) -> _Started:
        """Run the bot through to the end of polling, which the stub ends at once."""
        asyncio.run(app.run_bot(BotSettings(token=_TOKEN, db_path=db_path)))
        (bot,), (dispatcher,) = self._bots, self._dispatchers
        return _Started(database=self.database, bot=bot, dispatcher=dispatcher)


@pytest.fixture
def start(monkeypatch: pytest.MonkeyPatch) -> _Starter:
    """A stubbed-out ``run_bot``, ready to run."""
    return _Starter(monkeypatch)


def test_the_sweep_finishes_before_the_first_update_is_fetched(start: _Starter) -> None:
    start.database.active["leftover"] = active_row(token="leftover", group_id=GROUP, message_id=500)

    started = start.run()

    assert [call.kind for call in started.bot.calls] == ["markup", "send"]
    assert "voided" in started.bot.last("send").text
    assert started.dispatcher.calls_before_polling == len(started.bot.calls)
    assert started.database.active == {}


def test_polling_asks_for_the_updates_the_bot_actually_handles(start: _Starter) -> None:
    started = start.run()

    assert started.dispatcher.allowed_updates == list(app.ALLOWED_UPDATES)
    assert {"message", "callback_query"} <= set(app.ALLOWED_UPDATES)
    assert len(started.dispatcher.routers) == 1


def test_the_session_and_the_database_are_closed_on_the_way_out(start: _Starter) -> None:
    started = start.run()

    assert started.bot.session.closed
    assert started.database.closed


def test_the_bot_is_built_from_the_settings_it_was_given(start: _Starter) -> None:
    started = start.run("/elsewhere/esg.db")

    assert started.database.opened_path == "/elsewhere/esg.db"
    assert started.bot.token == _TOKEN


# ------------------------------------------------------- the command menu


def test_the_command_menu_is_registered_before_polling_starts(start: _Starter) -> None:
    started = start.run()

    assert [command.command for command in started.bot.commands] == [
        "flip",
        "leaderboard",
        "stats",
        "verify",
    ]
    assert started.bot.commands == list(app.COMMANDS)


def test_every_menu_entry_is_one_telegram_will_accept() -> None:
    names = [command.command for command in app.COMMANDS]

    assert len(names) == len(set(names))
    for command in app.COMMANDS:
        assert command.command == command.command.lower()
        assert 1 <= len(command.command) <= 32
        assert 3 <= len(command.description) <= 256
    # Building the real request is what Telegram's own limits get checked against.
    SetMyCommands(commands=list(app.COMMANDS))


def test_a_refused_menu_does_not_stop_the_bot_from_polling(
    start: _Starter, caplog: pytest.LogCaptureFixture
) -> None:
    start.command_failure = TelegramBadRequest(
        method=SetMyCommands(commands=[]), message="Bad Request: nope"
    )
    with caplog.at_level(logging.WARNING, logger="esgbot"):
        started = start.run()

    assert started.bot.commands == []
    assert started.dispatcher.calls_before_polling >= 0
    assert started.bot.session.closed
    assert "could not register the command menu" in caplog.text


# ------------------------------------------------- a group that moved while down

_NEW_GROUP = -1009876543210
"""The id ``GROUP`` answers to once it has become a supergroup."""

_OTHER_GROUP = -2002
"""A group that was never upgraded."""

_ORDER_LOG = "with nobody to record it"
"""What the gateway logs when it is told of a move and has no one to tell."""


def _upgraded_while_down() -> TelegramMigrateToChat:
    """Telegram's answer to the sweep's first call to ``GROUP``."""
    return TelegramMigrateToChat(
        method=EditMessageReplyMarkup(chat_id=GROUP, message_id=500),
        message="upgraded while offline",
        migrate_to_chat_id=_NEW_GROUP,
    )


@dataclass(slots=True)
class _RealRun:
    """A run of ``run_bot`` on a real database file, with the gateway kept for questions."""

    bot: _Bot
    dispatcher: _Dispatcher
    gateway: TelegramGateway
    path: Path

    async def leaderboard_names(self, group_id: int) -> list[str]:
        """Who is on a group's leaderboard now, read back from the file."""
        async with await Database.open(self.path) as database:
            return [row.display_name for row in await database.leaderboard(group_id)]

    async def leftover_tokens(self) -> list[str]:
        """The tokens of the open-flip rows still in the file."""
        async with await Database.open(self.path) as database:
            return [row.token for row in await database.list_active_flips()]


async def _seed(path: Path, *, finished_in: int, leftover_in: int) -> None:
    """A database as a stopped bot leaves it: one finished flip, one open row."""
    async with await Database.open(path) as database:
        await database.record_flip(
            make_flip(finished_in, [Line(1, "Ann", 50), Line(2, "Bo", -50)], seed_byte=7)
        )
        await database.add_active_flip(active_row(group_id=leftover_in))


async def _run_on_real_database(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *,
    failures: list[BaseException | None],
    dispatcher: Callable[[], _Dispatcher] = _Dispatcher,
) -> _RealRun:
    """``run_bot`` against the file at ``path``, with Telegram and polling stubbed.

    The database is the real one, so what a test reads afterwards is what the
    storage layer wrote. ``failures`` is the client's queue, so ``[moved]`` is a
    first call answered with a group's new id. ``dispatcher`` builds the stub
    that stands in for polling, for a test that needs polling to do something.
    """
    bots: list[_Bot] = []
    dispatchers: list[_Dispatcher] = []
    gateways: list[TelegramGateway] = []

    def make_bot(*, token: str, **_: object) -> _Bot:
        bot = _Bot(token)
        bot.failures.extend(failures)
        bot.session = _WatchedSession(bot)
        bots.append(bot)
        return bot

    def make_dispatcher(*_: object, **__: object) -> _Dispatcher:
        dispatchers.append(dispatcher())
        return dispatchers[-1]

    def make_gateway(client: _Bot) -> TelegramGateway:
        gateways.append(TelegramGateway(client))
        return gateways[-1]

    monkeypatch.setattr("esgbot.telegram.app.Bot", make_bot)
    monkeypatch.setattr("esgbot.telegram.app.Dispatcher", make_dispatcher)
    monkeypatch.setattr("esgbot.telegram.app.DefaultBotProperties", lambda **_: None)
    monkeypatch.setattr("esgbot.telegram.app.TelegramGateway", make_gateway)
    await app.run_bot(BotSettings(token=_TOKEN, db_path=str(path)))
    return _RealRun(bot=bots[0], dispatcher=dispatchers[0], gateway=gateways[0], path=path)


def test_a_move_the_sweep_discovers_reaches_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "esg.db"
        await _seed(path, finished_in=GROUP, leftover_in=GROUP)

        run = await _run_on_real_database(monkeypatch, path, failures=[_upgraded_while_down()])

        assert await run.leaderboard_names(_NEW_GROUP) == ["Ann", "Bo"]
        assert await run.leaderboard_names(GROUP) == []
        assert await run.leftover_tokens() == []
        assert [call.chat_id for call in run.bot.calls] == [_NEW_GROUP, _NEW_GROUP]
        assert "voided" in run.bot.last("send").text
        assert run.gateway.chat_id_for(GROUP) == _NEW_GROUP

    asyncio.run(scenario())


def test_the_move_is_followed_at_startup_with_no_flip_open_and_nothing_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the registry-is-empty case: a fresh process has no live flips at all.

    The test above runs in that state too, so this one adds what it does not: the
    move is the only thing the sweep finds in the group, a second group is left
    alone, and polling still begins once the sweep is through.
    """

    async def scenario() -> None:
        path = tmp_path / "esg.db"
        await _seed(path, finished_in=GROUP, leftover_in=GROUP)
        async with await Database.open(path) as database:
            await database.record_flip(
                make_flip(_OTHER_GROUP, [Line(3, "Cy", 20), Line(4, "Di", -20)], seed_byte=9)
            )

        run = await _run_on_real_database(monkeypatch, path, failures=[_upgraded_while_down()])

        assert await run.leaderboard_names(_NEW_GROUP) == ["Ann", "Bo"]
        assert await run.leaderboard_names(_OTHER_GROUP) == ["Cy", "Di"]
        assert run.dispatcher.calls_before_polling == len(run.bot.calls)

    asyncio.run(scenario())


def test_a_leftover_in_a_group_that_did_not_move_keeps_its_rows_where_they_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        path = tmp_path / "esg.db"
        await _seed(path, finished_in=_OTHER_GROUP, leftover_in=_OTHER_GROUP)

        with caplog.at_level(logging.ERROR):
            run = await _run_on_real_database(monkeypatch, path, failures=[])

        assert await run.leaderboard_names(_OTHER_GROUP) == ["Ann", "Bo"]
        assert await run.leaderboard_names(_NEW_GROUP) == []
        assert await run.leftover_tokens() == []
        assert [call.chat_id for call in run.bot.calls] == [_OTHER_GROUP, _OTHER_GROUP]
        assert run.gateway.chat_id_for(_OTHER_GROUP) == _OTHER_GROUP
        assert caplog.records == []

    asyncio.run(scenario())


def test_the_gateway_knows_who_to_tell_before_the_sweep_talks_to_anyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The ordering itself: a migration found by the sweep must have a handler waiting.

    Without one the gateway logs that a move had nobody to record it, which is the
    only trace of the history that was orphaned.
    """

    async def scenario() -> None:
        path = tmp_path / "esg.db"
        await _seed(path, finished_in=GROUP, leftover_in=GROUP)

        with caplog.at_level(logging.INFO):
            await _run_on_real_database(monkeypatch, path, failures=[_upgraded_while_down()])

        assert _ORDER_LOG not in caplog.text
        assert f"group {GROUP} became {_NEW_GROUP}: moved 1 flip(s)" in caplog.text

    asyncio.run(scenario())


# ------------------------------------------------ stopping with a result in flight

_STEP = 0.005
"""Real seconds between looks, while something on a worker thread catches up."""

_WAIT = 5.0
"""Real seconds any one wait may take before the test calls it a hang."""


async def _until(condition: Callable[[], bool], *, within: float = _WAIT) -> None:
    """Yield to the loop, for real, until ``condition`` holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("gave up waiting")
        await asyncio.sleep(_STEP)


class _QuickRegistry(FlipRegistry):
    """The registry, with a shutdown drain short enough for a test to sit through.

    ``shut_down``'s default is bound when the method is defined, so patching
    ``timing.SHUTDOWN_TELLING_SECONDS`` would change nothing; the override keeps
    ``run_bot`` calling ``shut_down()`` exactly as it does and only lowers the bound.
    """

    shortest: float = timing.SHUTDOWN_TELLING_SECONDS

    async def shut_down(self, *, within: float = timing.SHUTDOWN_TELLING_SECONDS) -> None:
        await super().shut_down(within=min(within, self.shortest))


class _Scene:
    """A flip taken to the moment its result is being written, with the write held.

    The write is ``queries.record_flip``, which runs in a worker thread, so it is
    held on a :class:`threading.Event` and announces itself to the loop with
    ``call_soon_threadsafe``. ``release_after`` is when, after polling ends, it is
    let go; ``None`` leaves that to the test.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, release_after: float | None, drain: float | None
    ) -> None:
        self.clock = FakeClock()
        self.gate = threading.Event()
        self.entered = asyncio.Event()
        self.release_after = release_after
        self.registry: FlipRegistry | None = None
        self.flip: Flip | None = None
        self.bot: _Bot | None = None
        loop = asyncio.get_running_loop()
        original = queries.record_flip

        def held_record(
            connection: sqlite3.Connection, flip: FinishedFlip, *, token: str | None = None
        ) -> int:
            loop.call_soon_threadsafe(self.entered.set)
            self.gate.wait(timeout=30)
            return original(connection, flip, token=token)

        def make_registry(*, gateway: TelegramGateway, store: Database) -> FlipRegistry:
            registry = _QuickRegistry(
                gateway=gateway, store=store, clock=self.clock, settle=settle_inline
            )
            if drain is not None:
                registry.shortest = drain
            self.registry = registry
            return registry

        monkeypatch.setattr("esgbot.storage.queries.record_flip", held_record)
        monkeypatch.setattr("esgbot.telegram.app.FlipRegistry", make_registry)

    def dispatcher(self) -> _Dispatcher:
        return _PlaysAFlip(self)

    @property
    def open_flip(self) -> Flip:
        assert self.flip is not None
        return self.flip

    async def open_lobby(self) -> None:
        assert self.registry is not None
        self.flip = await self.registry.create_flip(
            group_id=GROUP, starter=player(11), settings=SETTINGS
        )

    async def play_to_the_write(self) -> None:
        """Deal, wait out the peek window and the showdown, and stop at the write."""
        await self.open_lobby()
        flip = self.open_flip
        await flip.join(player(12), press_id="join")
        await flip.start(11, press_id="start")
        await _until(lambda: flip._cards_out)
        for _ in range(60):
            if self.entered.is_set():
                break
            await self.clock.advance(1)
            await asyncio.sleep(_STEP)
        await asyncio.wait_for(self.entered.wait(), _WAIT)
        if self.release_after is not None:
            asyncio.get_running_loop().call_later(self.release_after, self.gate.set)

    def seed_hex(self) -> str:
        round_ = self.open_flip._round
        assert round_ is not None
        return seed_to_hex(round_.seed)


class _PlaysAFlip(_Dispatcher):
    """Polling that has a flip part way through recording its result when it ends."""

    __slots__ = ("_scene",)

    def __init__(self, scene: _Scene) -> None:
        super().__init__()
        self._scene = scene

    async def _poll(self, bot: _Bot) -> None:
        self._scene.bot = bot
        await self._scene.play_to_the_write()


class _OpensALobby(_Dispatcher):
    """Polling that has one flip sitting in its lobby when it ends."""

    __slots__ = ("_scene",)

    def __init__(self, scene: _Scene) -> None:
        super().__init__()
        self._scene = scene

    async def _poll(self, bot: _Bot) -> None:
        await self._scene.open_lobby()


def test_the_poller_is_told_not_to_close_the_session_the_drain_still_needs(
    start: _Starter,
) -> None:
    """Whose session it is, and who closes it, exactly once.

    :meth:`aiogram.Dispatcher.start_polling` closes the bot's session in its own
    cleanup unless told not to, before it ever returns -- which would leave the
    drain sending a flip's result down a session that had just been taken away.
    """
    started = start.run()

    assert started.dispatcher.close_bot_session is False
    assert started.dispatcher.closed_the_session is False
    assert started.bot.session.closed
    assert started.bot.session.closes == 1


def test_the_poller_closes_the_session_by_itself_unless_it_is_told_not_to() -> None:
    """The default this all turns on, read off the real poller rather than assumed.

    If a future aiogram stops closing the session by default, the argument
    ``run_bot`` passes becomes redundant rather than load-bearing, and this is
    where that shows up.
    """
    parameter = inspect.signature(Dispatcher.start_polling).parameters["close_bot_session"]

    assert parameter.default is True
    assert (
        inspect.signature(_Dispatcher.start_polling).parameters["close_bot_session"].default is True
    )


def test_a_result_being_recorded_as_polling_stops_reaches_the_group_before_the_session_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        scene = _Scene(monkeypatch, release_after=0.05, drain=None)
        path = tmp_path / "esg.db"

        run = await asyncio.wait_for(
            _run_on_real_database(monkeypatch, path, failures=[], dispatcher=scene.dispatcher),
            timeout=30,
        )

        seed = scene.seed_hex()
        sent = [index for index, call in enumerate(run.bot.calls) if seed in call.text]
        assert sent, "the result never reached Telegram"
        session = run.bot.session
        assert isinstance(session, _WatchedSession)
        assert session.closed
        assert sent[0] < session.calls_at_close
        assert sorted(await run.leaderboard_names(GROUP)) == ["Player 11", "Player 12"]
        assert await run.leftover_tokens() == []
        calls_before = len(run.bot.calls)
        async with await Database.open(path) as database:
            assert await void_leftover_flips(store=database, gateway=run.gateway) == 0
        assert len(run.bot.calls) == calls_before

    asyncio.run(scenario())


def test_stopping_is_bounded_when_the_write_never_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The drain is bounded by lowering ``within`` to 0.3 s through :class:`_QuickRegistry`.

    What it bounds is the waiting: the drain gives up on a flip that cannot say
    how it ended and stopping carries on. ``run_bot`` as a whole still cannot
    return while the write is held, because closing the database queues behind
    it -- deliberately, since letting go of a connection a worker is still
    inside is the very thing that must not happen.
    """

    async def scenario() -> None:
        scene = _Scene(monkeypatch, release_after=None, drain=0.3)
        path = tmp_path / "esg.db"
        with caplog.at_level(logging.ERROR):
            run_task = asyncio.create_task(
                _run_on_real_database(monkeypatch, path, failures=[], dispatcher=scene.dispatcher)
            )
            try:
                # The drain gives up by itself, with the write still held.
                await _until(lambda: "could not say how they ended" in caplog.text)
                assert not scene.gate.is_set()
            finally:
                scene.gate.set()
            run = await asyncio.wait_for(run_task, timeout=10)

        assert run.bot.session.closed
        assert run.bot.session.closes == 1
        assert sorted(await run.leaderboard_names(GROUP)) == ["Player 11", "Player 12"]

    asyncio.run(scenario())


# ------------------------------------------- the real poller, over the loopback


class _LocalTelegram:
    """A Telegram API on 127.0.0.1, so the real poller and session can be used.

    Everything else here stubs aiogram out; this one does not, because what is
    being pinned is aiogram's own cleanup. It answers whatever method is asked
    of it and holds back the reply to the message carrying ``held``, which is how
    a send is still on the wire when polling ends.
    """

    def __init__(self, *, held: str, delay: float) -> None:
        self.held = held
        self.delay = delay
        self.asked: list[tuple[str, bool]] = []
        self.in_flight = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        application = web.Application()
        application.router.add_post("/bot{token}/{method}", self._answer)
        self._runner = web.AppRunner(application)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = cast(Any, site._server)
        assert server is not None
        sockets: tuple[socket.socket, ...] = server.sockets
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def sent(self) -> int:
        """How many messages carrying ``held`` this has accepted."""
        return sum(holding for _, holding in self.asked)

    async def _answer(self, request: web.Request) -> web.StreamResponse:
        form = await request.post()
        method = request.match_info["method"]
        text = str(form.get("text", ""))
        holding = self.held in text
        self.asked.append((method, holding))
        if holding:
            # Accepted. Only the reply is still coming.
            self.in_flight.set()
            await asyncio.sleep(self.delay)
        sent = {
            "message_id": len(self.asked),
            "date": 1,
            "chat": {"id": int(str(form.get("chat_id", GROUP))), "type": "group"},
            "text": text,
        }
        return web.json_response({"ok": True, "result": sent if method == "sendMessage" else True})


def test_the_real_poller_leaves_one_result_message_and_not_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of it, over the loopback: one flip, one result, through real aiogram.

    The flip's result is on the wire when polling ends. If the poller closed the
    session on its way out, that send would come back as a disconnection, the
    gateway would retry what Telegram had already accepted, and the group would
    be told its result twice -- for one recorded flip, with one revealed seed.

    Overriding ``_polling`` is how the real poller is driven without a real
    Telegram: it is where aiogram would be fetching updates, and returning from
    it is polling ending.
    """

    async def scenario() -> None:
        clock = FakeClock()
        flips: list[Flip] = []
        registries: list[FlipRegistry] = []

        def make_registry(*, gateway: TelegramGateway, store: Database) -> FlipRegistry:
            registry = FlipRegistry(gateway=gateway, store=store, clock=clock, settle=settle_inline)
            registries.append(registry)
            return registry

        telegram = _LocalTelegram(held="Deck seed", delay=0.2)
        await telegram.start()
        api = TelegramAPIServer.from_base(f"http://127.0.0.1:{telegram.port}")
        bot = Bot(token=_TOKEN, session=AiohttpSession(api=api))

        class _PlaysOneFlip(Dispatcher):
            async def start_polling(self, *bots: Bot, **options: Any) -> None:
                options["handle_signals"] = False
                await super().start_polling(*bots, **options)

            # aiogram's own signature is wider; only the bot matters here.
            async def _polling(  # type: ignore[override]
                self, bot: Bot, **options: Any
            ) -> None:
                registry = registries[0]
                flip = await registry.create_flip(
                    group_id=GROUP, starter=player(11), settings=SETTINGS
                )
                flips.append(flip)
                await flip.join(player(12), press_id="join")
                await flip.start(11, press_id="start")
                await _until(lambda: flip._cards_out)
                for _ in range(60):
                    if telegram.in_flight.is_set():
                        break
                    await clock.advance(1)
                    await asyncio.sleep(_STEP)
                await asyncio.wait_for(telegram.in_flight.wait(), _WAIT)

        monkeypatch.setattr("esgbot.telegram.app.Bot", lambda **_: bot)
        monkeypatch.setattr("esgbot.telegram.app.Dispatcher", _PlaysOneFlip)
        monkeypatch.setattr("esgbot.telegram.app.FlipRegistry", make_registry)
        path = tmp_path / "esg.db"
        try:
            await asyncio.wait_for(
                app.run_bot(BotSettings(token=_TOKEN, db_path=str(path))), timeout=30
            )

            assert telegram.sent == 1
            async with await Database.open(path) as database:
                assert len(await database.leaderboard(GROUP)) == 2
                assert await database.list_active_flips() == ()
            assert flips[0].closed
        finally:
            await bot.session.close()
            await telegram.stop()

    asyncio.run(scenario())


def test_stopping_with_no_flip_open_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        with caplog.at_level(logging.ERROR):
            run = await asyncio.wait_for(
                _run_on_real_database(monkeypatch, tmp_path / "esg.db", failures=[]),
                timeout=30,
            )

        assert run.bot.session.closed
        assert run.bot.calls == []
        assert caplog.records == []

    asyncio.run(scenario())


def test_stopping_with_a_flip_in_its_lobby_leaves_its_row_for_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        scene = _Scene(monkeypatch, release_after=None, drain=None)
        with caplog.at_level(logging.ERROR):
            run = await asyncio.wait_for(
                _run_on_real_database(
                    monkeypatch,
                    tmp_path / "esg.db",
                    failures=[],
                    dispatcher=lambda: _OpensALobby(scene),
                ),
                timeout=30,
            )

        assert run.bot.session.closed
        assert await run.leftover_tokens() == [scene.open_flip.token]
        assert caplog.records == []

    asyncio.run(scenario())


# ------------------------------------------------------------------- __main__


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> list[BotSettings]:
    """``__main__`` with the bot itself stubbed out, and logging left to pytest.

    ``setup_logging`` would take pytest's own handler off the root logger, so it
    is stubbed too: what is under test here is what ``main`` decides, not how the
    process configures logging, which ``test_logging_setup.py`` covers.
    """
    ran: list[BotSettings] = []

    async def record(settings: BotSettings) -> None:
        ran.append(settings)

    monkeypatch.setattr("esgbot.__main__.run_bot", record)
    monkeypatch.setattr("esgbot.__main__.setup_logging", lambda *_, **__: get_logger())
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv("ESGBOT_DB_PATH", raising=False)
    return ran


def _main() -> int:
    from esgbot.__main__ import main

    return main()


def test_a_missing_token_stops_the_process_with_a_word_about_the_variable(
    entry: list[BotSettings], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        status = _main()

    assert status != 0
    assert TOKEN_ENV_VAR in caplog.text
    assert entry == []


def test_a_configured_bot_is_run_and_comes_back_clean(
    entry: list[BotSettings], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, _TOKEN)
    monkeypatch.setenv("ESGBOT_DB_PATH", _DB_PATH)

    assert _main() == 0
    assert [(settings.token, settings.db_path) for settings in entry] == [(_TOKEN, _DB_PATH)]


def test_the_token_is_never_logged_on_the_way_up(
    entry: list[BotSettings], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, _TOKEN)
    monkeypatch.setenv("ESGBOT_DB_PATH", _DB_PATH)

    with caplog.at_level(logging.DEBUG):
        _main()

    assert _TOKEN not in caplog.text
    assert _DB_PATH in caplog.text


def test_an_interrupted_bot_exits_cleanly(
    entry: list[BotSettings], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(settings: BotSettings) -> None:
        raise KeyboardInterrupt

    monkeypatch.setenv(TOKEN_ENV_VAR, _TOKEN)
    monkeypatch.setattr("esgbot.__main__.run_bot", interrupted)

    assert _main() == 0
    assert entry == []

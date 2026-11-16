"""Every message the bot can send, rendered once for a realistic flip.

Two jobs. ``tests/test_samples.py`` walks this gallery to check that each message
stays inside Telegram's limits, escapes what a person typed, and never falls back
on the letters ``X``, ``Y`` and ``Z`` that only the documents use. And run as a
script it writes the whole gallery out as plain text::

    uv run python -m tests.samples /root/esgbot-live/samples.txt

which is how somebody reads what the bot actually says without playing a flip.

The flips behind the showdowns and results are **real**: each one is played
through the engine on a seed picked for the outcome it gives, with the fake clock
and gateway the engine's own tests use, and the views it produced are the ones
rendered here. Nothing in this file writes a message by hand, so a sample cannot
drift from what a group would see.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from esgbot.engine import (
    CommitmentView,
    FlipView,
    LobbyStatus,
    LobbyView,
    Notice,
    PeekAnswer,
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
from esgbot.game.cards import FULL_DECK
from esgbot.game.rules import FlipRequest, Settings, SettingsError, parse_flip_request
from esgbot.storage import PlayerStats
from esgbot.telegram import render
from esgbot.verify import Verification, VerifyPart
from tests.engine_helpers import harness, player, players, seed_where, use_seed

__all__ = ["ALERT", "MESSAGE", "Sample", "build_gallery", "main", "write_gallery"]

MESSAGE: Final = "message"
"""A group message: HTML, inside :data:`~esgbot.telegram.render.MESSAGE_LIMIT`."""

ALERT: Final = "alert"
"""A pop-up answering a button press: plain text, inside ``ALERT_LIMIT``."""

_EXPLICIT: Final = Settings(wager=50, x=5, y=2, z=3)
"""``/flip 50 5 2 3``: every setting given."""

_DEFAULTED: Final = FlipRequest(wager=50, x=5)
"""``/flip 50 5``: the extra cards and the scoop multiplier left to the bot."""

_IDS: Final = [11, 12, 13]
_EVIL: Final = "<b>Ev&il</b> Bob"
"""A name somebody chose for themselves, to show it reaching Telegram escaped."""


@dataclass(frozen=True, slots=True)
class Sample:
    """One rendered message, with the heading it appears under in the gallery."""

    section: str
    title: str
    text: str
    kind: str = MESSAGE


@dataclass(frozen=True, slots=True)
class _Played:
    """What one real flip produced, for the samples that need genuine views."""

    tables: tuple[TableView, ...]
    showdowns: tuple[ShowdownView, ...]
    result: ResultView


def _play(
    *,
    settings: Settings | FlipRequest = _EXPLICIT,
    wanted: str = "any",
    player_ids: list[int] | None = None,
) -> _Played:
    """Play one flip to its result and keep every view it produced.

    ``wanted`` picks the seed: a scoop, a chop between two seats, a table that
    ties all round, or whatever the first seed gives.
    """
    ids = _IDS if player_ids is None else player_ids
    resolved = settings.resolve(len(ids)) if isinstance(settings, FlipRequest) else settings
    conditions = {
        "any": lambda outcome: True,
        "scoop": lambda outcome: outcome.scoop_seat is not None,
        "chop": lambda outcome: len(outcome.winners) == 2,
        "tied": lambda outcome: outcome.all_tied,
    }
    seed = seed_where(conditions[wanted], player_ids=ids, settings=resolved)

    played: list[_Played] = []

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=len(ids) - 1, settings=settings)
            await world.start(flip)
            await world.finish(flip)
            views = world.gateway.views
            result = views[-1]
            assert isinstance(result, ResultView)
            played.append(
                _Played(
                    tables=tuple(view for view in views if isinstance(view, TableView)),
                    showdowns=tuple(view for view in views if isinstance(view, ShowdownView)),
                    result=result,
                )
            )

    with pytest.MonkeyPatch.context() as patch:
        use_seed(patch, seed)
        asyncio.run(scenario())
    return played[0]


def _lobby(
    request: FlipRequest,
    *,
    count: int,
    status: LobbyStatus = LobbyStatus.OPEN,
    names: tuple[str, ...] = (),
) -> LobbyView:
    """A lobby with this many players in it, named or numbered."""
    roster = (
        tuple(player(11 + index, name) for index, name in enumerate(names))
        if names
        else players(count)
    )
    return LobbyView(token="tok", request=request, status=status, starter=roster[0], players=roster)


def _table(
    played: _Played, stage: TableStage, *, peeked: int = 0, voided: bool = False
) -> TableView:
    """One of a real flip's tables, at the stage and peek count the gallery wants."""
    for view in played.tables:
        if view.stage is stage:
            return TableView(
                token=view.token,
                settings=view.settings,
                y_defaulted=view.y_defaulted,
                commitment=view.commitment,
                stage=view.stage,
                button_seat=view.button_seat,
                seats=view.seats,
                board1=view.board1,
                board2=view.board2,
                extra_count=view.extra_count,
                peeked=peeked,
                voided=voided,
            )
    raise AssertionError(f"the flip produced no table at {stage}")


def _hostile_table(played: _Played) -> TableView:
    """The same table with a name that would be markup if it were not escaped."""
    table = _table(played, TableStage.PEEKING, peeked=1)
    seats = tuple(
        SeatView(
            seat=seat.seat, player=player(seat.player.user_id, _EVIL), card_count=seat.card_count
        )
        if seat.seat == 0
        else seat
        for seat in table.seats
    )
    return TableView(
        token=table.token,
        settings=table.settings,
        y_defaulted=table.y_defaulted,
        commitment=table.commitment,
        stage=table.stage,
        button_seat=table.button_seat,
        seats=seats,
        board1=table.board1,
        board2=table.board2,
        extra_count=table.extra_count,
        peeked=table.peeked,
        voided=table.voided,
    )


def _unrecorded(result: ResultView) -> ResultView:
    """The same result for a flip storage could not save, so it has no number."""
    return ResultView(
        token=result.token,
        flip_number=None,
        settings=result.settings,
        y_defaulted=result.y_defaulted,
        commitment=result.commitment,
        seed=result.seed,
        button_seat=result.button_seat,
        boards=result.boards,
        hands=result.hands,
        winners=result.winners,
        payments=result.payments,
        scoop_seat=result.scoop_seat,
    )


def _stats(name: str, net: int, flips: int, wins: int, scoops: int) -> PlayerStats:
    return PlayerStats(
        user_id=abs(hash(name)) % 10_000,
        display_name=name,
        username=name.lower(),
        net_dollars=net,
        flips_played=flips,
        wins=wins,
        scoops=scoops,
    )


def _error(tokens: list[str]) -> SettingsError:
    """The complaint a bad ``/flip`` raises, for the samples that show one."""
    try:
        parse_flip_request(tokens)
    except SettingsError as caught:
        return caught
    raise AssertionError(f"{tokens} is not a bad command")


def _verification(*, passed: bool, parts: tuple[VerifyPart, ...] = ()) -> Verification:
    commitment = fairness.commitment(bytes(range(32)))
    other = fairness.commitment(bytes(32))
    return Verification(
        flip_number=42,
        commitment=commitment,
        recomputed_commitment=commitment if passed else other,
        differences=() if passed else parts,
    )


def _flip_samples(section: str, views: dict[str, FlipView]) -> list[Sample]:
    """Render a group of views through the dispatcher every message goes through."""
    return [Sample(section, title, render.render(view)) for title, view in views.items()]


def build_gallery() -> list[Sample]:
    """Every message the bot sends, rendered once, in the order a flip meets them."""
    ordinary = _play(wanted="any")
    scooped = _play(wanted="scoop")
    chopped = _play(wanted="chop")
    tied = _play(wanted="tied")
    defaulted = _play(settings=_DEFAULTED)
    heads_up = _play(settings=_DEFAULTED, player_ids=[11, 12])

    samples: list[Sample] = []

    samples += [
        Sample("Starting a flip", "/flip with no settings", render.USAGE),
        Sample(
            "Starting a flip",
            "/flip 50 — too few numbers",
            render.render_settings_error(_error(["50"])),
        ),
        Sample(
            "Starting a flip",
            "/flip 50 99 — a private-card count outside its limits",
            render.render_settings_error(_error(["50", "99"])),
        ),
        Sample(
            "Starting a flip",
            "/flip 50 5 2 0 — a scoop multiplier outside its limits",
            render.render_settings_error(_error(["50", "5", "2", "0"])),
        ),
        Sample(
            "Starting a flip",
            "/flip $50 5 — something that is not a whole number",
            render.render_settings_error(_error(["$50", "5"])),
        ),
        Sample(
            "Starting a flip",
            "a flip is already open (settings given)",
            render.render_already_open(FlipRequest.from_settings(_EXPLICIT)),
        ),
        Sample(
            "Starting a flip",
            "a flip is already open (settings left to the bot)",
            render.render_already_open(_DEFAULTED),
        ),
        Sample("Starting a flip", "/flip in a private chat", render.NO_FLIP_IN_PRIVATE),
        Sample("Starting a flip", "the database is unavailable", render.STORAGE_TROUBLE),
        Sample("Repeating a flip", "/again with settings after it", render.AGAIN_USAGE),
        Sample("Repeating a flip", "/again with no flip to repeat", render.NO_FLIP_TO_REPEAT),
    ]

    samples += _flip_samples(
        "The lobby",
        {
            "open, one player, every setting given": _lobby(
                FlipRequest.from_settings(_EXPLICIT), count=1
            ),
            "open, three players, settings left to the bot": _lobby(_DEFAULTED, count=3),
            "open, a flip that asked for no extra cards": _lobby(
                FlipRequest(wager=1_000_000, x=21, y=0, z=1), count=2
            ),
            "open, one extra card and no scoop bonus": _lobby(
                FlipRequest(wager=5, x=6, y=1, z=1), count=2
            ),
            "open and full": _lobby(FlipRequest(wager=50, x=21), count=2),
            "a name that would be markup": _lobby(_DEFAULTED, count=2, names=(_EVIL, "Dana")),
            "cancelled by its starter": _lobby(
                FlipRequest.from_settings(_EXPLICIT), count=2, status=LobbyStatus.CANCELLED
            ),
            "timed out": _lobby(_DEFAULTED, count=1, status=LobbyStatus.TIMED_OUT),
            "voided before it started": _lobby(_DEFAULTED, count=2, status=LobbyStatus.VOIDED),
        },
    )

    samples += _flip_samples(
        "The deal",
        {
            "the deck commitment": CommitmentView(
                token="tok", commitment=ordinary.result.commitment
            ),
            "private cards are out": _table(ordinary, TableStage.HOLE_CARDS),
            "board 1 is out": _table(ordinary, TableStage.BOARD1),
            "both boards are out": _table(ordinary, TableStage.BOARD2),
            "every card is out, nobody has peeked": _table(ordinary, TableStage.PEEKING),
            "every card is out, one player has peeked": _table(
                ordinary, TableStage.PEEKING, peeked=1
            ),
            "settings left to the bot, as many extra cards as fit": _table(
                defaulted, TableStage.PEEKING, peeked=2
            ),
            "a table whose flip was voided": _table(
                ordinary, TableStage.PEEKING, peeked=2, voided=True
            ),
            "a name that would be markup": _hostile_table(ordinary),
        },
    )

    samples += _flip_samples(
        "The showdown",
        {
            f"step {step.name.lower().replace('_', ' ')}": view
            for step, view in zip(ShowdownStep, ordinary.showdowns, strict=True)
        },
    )

    samples += _flip_samples(
        "The result",
        {
            "one winner": ordinary.result,
            "a scoop": scooped.result,
            "a chop between two players": chopped.result,
            "everybody tied": tied.result,
            "settings left to the bot": defaulted.result,
            "heads up, settings left to the bot": heads_up.result,
            "the flip could not be saved": _unrecorded(ordinary.result),
        },
    )

    samples += _flip_samples(
        "A flip that was voided",
        {
            "a round that had dealt": VoidView(
                token="tok",
                reason=VoidReason.TIMED_OUT,
                request=FlipRequest.from_settings(_EXPLICIT),
                settings=_EXPLICIT,
                players=players(3),
                seed=ordinary.result.seed,
                commitment=ordinary.result.commitment,
            ),
            "a round whose settings the bot had filled in": VoidView(
                token="tok",
                reason=VoidReason.ERROR,
                request=_DEFAULTED,
                settings=_DEFAULTED.resolve(3),
                players=players(3),
                seed=ordinary.result.seed,
                commitment=ordinary.result.commitment,
            ),
            "a lobby that never dealt a card": VoidView(
                token="tok",
                reason=VoidReason.RESTART,
                request=_DEFAULTED,
                players=players(2),
            ),
            "a leftover row after a restart, and nothing else known": VoidView(
                token="tok", reason=VoidReason.RESTART, seed=ordinary.result.seed
            ),
            "the bot could not post in the group": VoidView(
                token="tok",
                reason=VoidReason.SEND_FAILED,
                request=FlipRequest.from_settings(_EXPLICIT),
                settings=_EXPLICIT,
                players=players(3),
                seed=ordinary.result.seed,
            ),
        },
    )

    peeks = {
        "private cards only": PeekAnswer(seat=0, hole=FULL_DECK[:5]),
        "private cards and the extra cards": PeekAnswer(
            seat=1, hole=FULL_DECK[:5], extra=FULL_DECK[5:7]
        ),
        "the largest hand a flip can deal": PeekAnswer(
            seat=1, hole=FULL_DECK[:5], extra=FULL_DECK[5:21]
        ),
    }
    samples += [
        Sample("Peek", title, render.render_peek(answer), ALERT) for title, answer in peeks.items()
    ]
    samples += [
        Sample("Button answers", notice.value, render.render_notice(notice), ALERT)
        for notice in Notice
    ]

    board = [
        _stats("Sam", 200, 3, 2, 1),
        _stats("Dana", 50, 3, 1, 0),
        _stats("Alex", 0, 2, 0, 0),
        _stats(_EVIL, -250, 3, 0, 0),
    ]
    samples += [
        Sample("Stats", "/leaderboard", render.render_leaderboard(board)),
        Sample("Stats", "/leaderboard in a group with no flips", render.render_leaderboard([])),
        Sample("Stats", "/stats for a player who has played", render.render_stats(board[0])),
        Sample("Stats", "/stats for a player in the red", render.render_stats(board[3])),
        Sample("Stats", "/stats for yourself, with no flips", render.NO_FLIPS_OF_YOUR_OWN),
        Sample("Stats", "/stats for somebody with no flips", render.render_no_stats_for("Dana")),
        Sample(
            "Stats", "/stats for a name nobody played under", render.render_unknown_player("ghost")
        ),
        Sample("Stats", "/stats in a private chat", render.NO_STATS_IN_PRIVATE),
        Sample("Stats", "/stats that cannot be read", render.STATS_TROUBLE),
        Sample("Stats", "/stats with too many arguments", render.STATS_USAGE),
    ]

    samples += [
        Sample("Verifying a flip", "/verify with no number", render.VERIFY_USAGE),
        Sample(
            "Verifying a flip",
            "a flip that verifies",
            render.render_verification(_verification(passed=True)),
        ),
        Sample(
            "Verifying a flip",
            "a flip whose money was changed",
            render.render_verification(_verification(passed=False, parts=(VerifyPart.NETS,))),
        ),
        Sample(
            "Verifying a flip",
            "a flip whose seed was changed",
            render.render_verification(
                _verification(
                    passed=False,
                    parts=(VerifyPart.COMMITMENT, VerifyPart.DEAL, VerifyPart.POINTS),
                )
            ),
        ),
        Sample(
            "Verifying a flip",
            "a flip number this group does not have",
            render.render_no_such_flip(99),
        ),
        Sample(
            "Verifying a flip",
            "a stored flip that will not read back",
            render.render_unreadable_flip(42),
        ),
    ]
    return samples


def write_gallery(path: Path) -> int:
    """Write the gallery out as plain text and return how many samples it holds."""
    samples = build_gallery()
    lines = [
        "ESG Flip Bot — every message it sends",
        "",
        "Rendered from real flips by tests/samples.py. The group sees these with the HTML",
        "tags applied: <b>…</b> is bold, <code>…</code> is monospace.",
        "",
    ]
    section = ""
    for sample in samples:
        if sample.section != section:
            section = sample.section
            lines += ["", "=" * 78, f"== {section}", "=" * 78, ""]
        label = sample.title if sample.kind is MESSAGE else f"{sample.title} (pop-up)"
        lines += ["-" * 78, f"-- {label}", "-" * 78, "", sample.text, ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(samples)


def main(argv: list[str] | None = None) -> int:
    """Write the gallery to the path given on the command line."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python -m tests.samples <path>", file=sys.stderr)
        return 2
    path = Path(arguments[0])
    count = write_gallery(path)
    print(f"wrote {count} samples to {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - a dev helper, not a test
    raise SystemExit(main())

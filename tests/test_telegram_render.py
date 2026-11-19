"""What the bot says: every view the engine produces, read off as the message it becomes.

The renderer is pure, so most of these build a view by hand and read the text. Where
the test needs a showdown or a result that is real -- a scoop, a chop, a table that
ties all round -- it plays an actual flip on a pinned seed and takes the views the
engine produced, which is the most honest way to get one. The points worth pinning
are the ones a player would notice: nothing leaks that should not, nothing a person
typed reaches Telegram unescaped, the peek rule quotes the engine's own numbers, and
no message or pop-up ever outgrows Telegram's limits.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction
from typing import Final

import pytest

from esgbot.engine import (
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
    ResultView,
    SeatView,
    ShowdownStep,
    ShowdownView,
    TableStage,
    TableView,
    VoidReason,
    VoidView,
    read_hand,
    timing,
)
from esgbot.game import fairness
from esgbot.game.cards import FULL_DECK, Card, Rank, Suit, format_cards
from esgbot.game.deal import deal_cards, extra_rounds_for
from esgbot.game.evaluator import BestHand, Category, HandRank, rank_five
from esgbot.game.rules import (
    BOARD_COUNT,
    MIN_PLAYERS,
    WAGER_MAX,
    WAGER_MIN,
    X_MAX,
    X_MIN,
    Y_DEFAULT,
    Y_MAX,
    Y_MIN,
    Z_MAX,
    Z_MIN,
    FlipRequest,
    Settings,
    SettingsError,
    max_players,
    parse_flip_request,
)
from esgbot.game.scoring import Board
from esgbot.game.settlement import FlipOutcome
from esgbot.telegram import keyboards, render
from tests.engine_helpers import (
    harness,
    players,
    seed_where,
    use_seed,
)

SETTINGS = Settings(wager=50, x=5, y=2, z=3)
REQUEST = FlipRequest.from_settings(SETTINGS)
PLAYER_IDS = [11, 12, 13]
EVIL = "<b>Ev&il</b> \"Bob\" <script>alert('x')</script>"
EVIL_ESCAPED = "&lt;b&gt;Ev&amp;il&lt;/b&gt;"
SEED = bytes(range(32))
COMMITMENT = fairness.commitment(SEED)

# The settings strip of SETTINGS, and of a request that left Y and Z to the bot.
STRIP = "5 private cards each · up to 2 extra cards each · scoop pays 3x the wager"
DEFAULTED_STRIP = (
    "5 private cards each · as many extra cards as fit · "
    "scoop pays the wager times the number of players"
)


def _starter() -> Player:
    return players(1)[0]


def _lobby(
    count: int,
    *,
    status: LobbyStatus = LobbyStatus.OPEN,
    request: FlipRequest = REQUEST,
    names: tuple[Player, ...] | None = None,
) -> LobbyView:
    roster = names if names is not None else players(count)
    return LobbyView(token="tok", request=request, status=status, starter=roster[0], players=roster)


def _table(
    stage: TableStage = TableStage.PEEKING,
    *,
    seated: tuple[Player, ...] | None = None,
    extra_count: int = 2,
    peeked: int = 0,
    voided: bool = False,
    board1: tuple[Card, ...] = (),
    board2: tuple[Card, ...] = (),
) -> TableView:
    roster = seated if seated is not None else players(3)
    count = SETTINGS.x + extra_count if stage is TableStage.PEEKING else SETTINGS.x
    return TableView(
        token="tok",
        settings=SETTINGS,
        commitment=COMMITMENT,
        stage=stage,
        button_seat=1,
        seats=tuple(
            SeatView(seat=seat, player=person, card_count=count)
            for seat, person in enumerate(roster)
        ),
        board1=board1,
        board2=board2,
        extra_count=extra_count,
        peeked=peeked,
        voided=voided,
    )


def _cards(text: str) -> tuple[Card, ...]:
    return tuple(Card.parse(token) for token in text.split())


BOARD1 = _cards("2c 3d 4h 5s 9c")
BOARD2 = _cards("Kd Qs Jh 7c 8d")


def _played(
    monkeypatch: pytest.MonkeyPatch,
    wanted: Callable[[FlipOutcome], bool],
    settings: Settings = SETTINGS,
) -> tuple[list[ShowdownView], ResultView]:
    """Play a whole flip on the first seed that fits, and keep its showdown and result."""
    seed = seed_where(wanted, player_ids=PLAYER_IDS, settings=settings)
    use_seed(monkeypatch, seed)
    found: list[tuple[list[ShowdownView], ResultView]] = []

    async def scenario() -> None:
        world = harness(settings=settings)
        async with world.registry:
            flip = await world.open_lobby(joiners=2)
            await world.start(flip)
            await world.finish(flip)
            showdowns = [v for v in world.gateway.views if isinstance(v, ShowdownView)]
            result = world.gateway.posts[-1].view
            assert isinstance(result, ResultView)
            found.append((showdowns, result))

    asyncio.run(scenario())
    return found[0]


def _scoop(outcome: FlipOutcome) -> bool:
    return outcome.scoop_seat is not None


def _chop(outcome: FlipOutcome) -> bool:
    return len(outcome.winners) == 2 and outcome.scoop_seat is None


def _tied(outcome: FlipOutcome) -> bool:
    return outcome.all_tied


def _renamed(view: ShowdownView | ResultView, name: str) -> ShowdownView | ResultView:
    """The same view with every player called ``name`` (distinct ids, same text)."""
    hands = tuple(
        replace(hand, player=Player(user_id=hand.player.user_id, display_name=name))
        for hand in view.hands
    )
    return replace(view, hands=hands)


# ------------------------------------------------------------------------ lobby


def test_an_open_lobby_shows_settings_cap_roster_and_starter() -> None:
    text = render.render_lobby(_lobby(3))
    lines = text.splitlines()
    assert lines[0] == "🎲 <b>New flip</b> — $50 a head"
    assert lines[1] == STRIP
    assert "\n\n<b>Players 3/8</b>\n" in text
    assert "1. Player 11\n2. Player 12\n3. Player 13" in text
    assert "Opened by Player 11" in text


def test_the_player_cap_is_forty_two_over_x() -> None:
    text = render.render_lobby(_lobby(2, request=FlipRequest(wager=50, x=21, y=0, z=1)))
    assert "<b>Players 2/2</b>" in text
    assert "21 private cards each · no extra cards · scoop pays the wager, no bonus" in text


def test_a_full_lobby_says_so_and_names_who_presses_start() -> None:
    view = _lobby(SETTINGS.max_players)
    assert view.full
    text = render.render_lobby(view)
    assert f"<b>Players {view.player_count}/{view.player_count}</b>" in text
    assert "The lobby is full. Player 11 opened it and presses Start." in text


def test_a_lobby_short_of_the_minimum_asks_for_more_players() -> None:
    assert MIN_PLAYERS == 2
    text = render.render_lobby(_lobby(1))
    assert f"A flip needs at least {MIN_PLAYERS} players" in text
    assert "press Join" in text


def test_a_lobby_with_enough_players_tells_the_starter_to_press_start() -> None:
    text = render.render_lobby(_lobby(MIN_PLAYERS))
    assert "presses Start when everyone is in" in text


@pytest.mark.parametrize(
    ("status", "heading"),
    [
        (LobbyStatus.CANCELLED, "Flip cancelled"),
        (LobbyStatus.TIMED_OUT, "Lobby timed out"),
        (LobbyStatus.VOIDED, "Flip voided"),
    ],
)
def test_a_final_lobby_has_no_buttons_and_says_nothing_was_recorded(
    status: LobbyStatus, heading: str
) -> None:
    view = _lobby(3, status=status)
    text = render.render_lobby(view)
    assert f"<b>{heading}</b> — $50 a head" in text.splitlines()[0]
    assert STRIP in text
    assert "<b>Players 3/8</b>" in text
    assert "Nothing was recorded and nobody owes anything." in text
    assert "press Join" not in text
    assert "presses Start" not in text
    assert keyboards.markup_for(view) is None


def test_an_open_lobby_carries_buttons() -> None:
    assert keyboards.markup_for(_lobby(3)) is not None


def test_an_explicit_request_renders_the_four_figures_exactly() -> None:
    text = render.render_lobby(_lobby(3, request=FlipRequest(wager=50, x=5, y=2, z=3)))
    assert "— $50 a head\n" + STRIP + "\n" in text
    assert "as many extra cards as fit" not in text
    assert "number of players" not in text


def test_a_lobby_for_a_flip_with_y_and_z_left_out_says_so_without_figures() -> None:
    text = render.render_lobby(_lobby(3, request=FlipRequest(wager=50, x=5)))
    assert "— $50 a head\n" + DEFAULTED_STRIP + "\n" in text
    assert "21" not in text
    assert "scoop pays 3x" not in text
    assert "<b>Players 3/8</b>" in text


def test_a_lobby_for_a_flip_with_only_z_left_out_keeps_the_y_figure() -> None:
    text = render.render_lobby(_lobby(3, request=FlipRequest(wager=50, x=5, y=2)))
    assert (
        "5 private cards each · up to 2 extra cards each · "
        "scoop pays the wager times the number of players"
    ) in text
    assert "as many extra cards as fit" not in text


def test_the_player_cap_of_a_defaulted_lobby_still_comes_from_x() -> None:
    text = render.render_lobby(_lobby(2, request=FlipRequest(wager=50, x=21)))
    assert "<b>Players 2/2</b>" in text


@pytest.mark.parametrize(
    "status", [LobbyStatus.CANCELLED, LobbyStatus.TIMED_OUT, LobbyStatus.VOIDED]
)
def test_a_closed_defaulted_lobby_still_says_what_was_asked_for(status: LobbyStatus) -> None:
    text = render.render_lobby(_lobby(3, status=status, request=FlipRequest(wager=50, x=5)))
    assert "— $50 a head\n" + DEFAULTED_STRIP + "\n" in text
    assert "21" not in text


# ------------------------------------------------------------------------ table


@pytest.mark.parametrize(
    ("stage", "board1", "board2"),
    [
        (TableStage.HOLE_CARDS, (), ()),
        (TableStage.BOARD1, BOARD1, ()),
        (TableStage.BOARD2, BOARD1, BOARD2),
        (TableStage.PEEKING, BOARD1, BOARD2),
    ],
)
def test_each_table_stage_shows_exactly_the_boards_that_are_out(
    stage: TableStage, board1: tuple[Card, ...], board2: tuple[Card, ...]
) -> None:
    text = render.render_table(_table(stage, board1=board1, board2=board2))
    for number, shown, cards in ((1, board1, BOARD1), (2, board2, BOARD2)):
        line = next(line for line in text.splitlines() if line.startswith(f"<b>Board {number}</b>"))
        if shown:
            assert format_cards(cards) in line
        else:
            assert "to come" in line


def test_the_table_counts_cards_per_seat_including_the_extra_ones() -> None:
    before = render.render_table(_table(TableStage.BOARD2, extra_count=0, board1=BOARD1))
    assert "1. Player 11 — 5 cards" in before
    after = render.render_table(_table(TableStage.PEEKING))
    assert "1. Player 11 — 7 cards" in after
    assert "3. Player 13 — 7 cards" in after
    assert "Extra cards: 2 each, so everybody holds 7. Press Peek again to see them." in after
    assert "Press Peek to see your own cards." in before


def test_a_single_card_is_not_pluralised() -> None:
    text = render.render_table(_table(TableStage.PEEKING, extra_count=1))
    assert "1. Player 11 — 6 cards" in text
    assert "Extra cards: 1 each, so everybody holds 6." in text
    assert "1 cards" not in text


def test_the_table_names_the_button_commitment_and_pot() -> None:
    text = render.render_table(_table())
    assert "$50 a head · pot $150 · button Player 12" in text
    assert text.endswith(f"Deck commitment <code>{COMMITMENT}</code>")


@pytest.mark.parametrize(
    ("stage", "title"),
    [
        pytest.param(TableStage.HOLE_CARDS, "🃏 <b>Private cards are out</b>", id="private-cards"),
        pytest.param(TableStage.BOARD1, "🃏 <b>Board 1 is out</b>", id="board-1"),
        pytest.param(TableStage.BOARD2, "🃏 <b>Both boards are out</b>", id="board-2"),
        pytest.param(TableStage.PEEKING, "🃏 <b>Every card is out</b>", id="peeking"),
    ],
)
def test_the_table_title_says_which_moment_of_the_deal_this_is(
    stage: TableStage, title: str
) -> None:
    assert render.render_table(_table(stage)).startswith(f"{title}\n")


def test_every_stage_of_the_deal_has_a_title_of_its_own() -> None:
    # Four edits, four first lines: somebody glancing at the message can tell
    # what has just happened without reading the rest of it.
    titles = [render.render_table(_table(stage)).splitlines()[0] for stage in TableStage]
    assert len(set(titles)) == len(list(TableStage))
    voided = render.render_table(_table(TableStage.BOARD1, voided=True)).splitlines()[0]
    assert voided == "⚠️ <b>Flip voided</b>"
    assert voided not in titles


def test_the_peek_stage_states_the_showdown_rule_in_the_engines_numbers() -> None:
    text = render.render_table(_table(peeked=2))
    window = f"{timing.PEEK_WINDOW_SECONDS:.0f} seconds"
    everyone = f"{timing.PEEK_ALL_SECONDS:.0f} seconds"
    assert f"Showdown in {window}, or {everyone} after everyone has peeked." in text
    assert "👀 2 of 3 have peeked." in text
    assert "👀 1 of 3 has peeked." in render.render_table(_table(peeked=1))


def test_the_table_has_no_ticking_countdown() -> None:
    first = render.render_table(_table(peeked=0))
    again = render.render_table(_table(peeked=0))
    assert first == again
    assert "left" not in first
    assert "remaining" not in first


def test_earlier_stages_do_not_state_the_showdown_rule() -> None:
    text = render.render_table(_table(TableStage.BOARD2, extra_count=0, board1=BOARD1))
    assert "Showdown in" not in text
    assert "Peeked" not in text


def test_a_deck_too_short_for_extra_cards_says_so() -> None:
    text = render.render_table(_table(extra_count=0))
    assert "No extra cards: the deck was too short to give everyone one." in text
    assert "Extra cards:" not in text


def test_a_flip_that_asked_for_no_extra_cards_does_not_blame_the_deck() -> None:
    no_extras = replace(SETTINGS, y=0)
    text = render.render_table(replace(_table(extra_count=0), settings=no_extras))
    assert "No extra cards: this flip asked for none." in text
    assert "the deck was too short" not in text
    assert "deck" not in text.replace("Deck commitment", "")
    assert "Extra cards:" not in text
    shortage = render.render_table(_table(extra_count=0))
    assert text != shortage


def test_a_table_that_defaulted_y_says_so_in_words_and_shows_the_resolved_z() -> None:
    resolved = Settings(wager=50, x=5, y=21, z=3)
    view = replace(_table(), settings=resolved, y_defaulted=True)
    text = render.render_table(view)
    assert "$50 a head · pot $150 · button Player 12" in text
    assert "5 private cards each · as many extra cards as fit · scoop pays 3x the wager" in text
    assert "21" not in text


def test_a_table_that_did_not_default_y_shows_the_number() -> None:
    view = replace(_table(), settings=Settings(wager=50, x=5, y=21, z=3))
    assert not view.y_defaulted
    text = render.render_table(view)
    assert "up to 21 extra cards each" in text
    assert "as many extra cards as fit" not in text


def test_a_defaulted_y_with_a_short_deck_blames_the_deck_not_the_request() -> None:
    view = replace(_table(extra_count=0), settings=Settings(wager=50, x=5, y=21, z=3))
    text = render.render_table(replace(view, y_defaulted=True))
    assert "No extra cards: the deck was too short to give everyone one." in text
    assert "this flip asked for none" not in text


def test_a_voided_table_keeps_its_cards_but_loses_the_rule_and_the_button() -> None:
    view = _table(voided=True, board1=BOARD1, board2=BOARD2)
    text = render.render_table(view)
    assert format_cards(BOARD1) in text
    assert "7 cards" in text
    assert text.splitlines()[0] == "⚠️ <b>Flip voided</b>"
    assert "Nothing was recorded and nobody owes anything." in text
    assert "Showdown in" not in text
    assert "Press Peek" not in text
    assert keyboards.markup_for(view) is None


def test_a_live_table_carries_the_peek_button() -> None:
    assert keyboards.markup_for(_table()) is not None


def test_the_table_shows_no_hole_card_faces() -> None:
    text = render.render_table(_table(board1=BOARD1, board2=BOARD2))
    assert "♠" in text
    assert text.count("♠") == format_cards(BOARD1 + BOARD2).count("♠")


# --------------------------------------------------------------------- showdown


def test_the_hands_step_shows_every_hand_and_no_board_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    first = showdowns[0]
    assert first.step is ShowdownStep.HANDS
    text = render.render_showdown(first)
    for hand in first.hands:
        assert f"{hand.seat + 1}. {hand.player.display_name}" in text
        for card in hand.hole:
            assert str(card) in text
        for card in hand.extra:
            assert f"{card}*" in text
    assert "* dealt after the boards" in text
    assert "<b>Hands</b>" in text
    assert "<b>Boards won</b>" not in text
    assert "<b>Points</b>" not in text


def test_each_later_step_adds_exactly_its_boards_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    names = {
        Board.BOARD1: "Board 1:",
        Board.BOARD2: "Board 2:",
        Board.HAND_STRENGTH: "Hand strength:",
    }
    assert [view.step for view in showdowns] == list(ShowdownStep)
    for view in showdowns:
        text = render.render_showdown(view)
        for board in Board:
            assert (names[board] in text) == view.step.reveals(board)


def test_the_points_step_adds_the_totals_and_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    final = showdowns[-1]
    assert final.step is ShowdownStep.POINTS
    text = render.render_showdown(final)
    assert "<b>Points</b>" in text
    for hand in final.hands:
        assert f"{hand.player.display_name}: {hand.points}" in text
    assert "<b>Player" in text.split("<b>Points</b>")[1].split("\n\n")[0]
    assert " scoops all three boards.</b>" in text
    assert "scoops" not in render.render_showdown(showdowns[-2])


def test_a_chopped_board_shows_the_share(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, _ = _played(monkeypatch, _chop)
    final = showdowns[-1]
    shares = {board.share for board in final.boards if board.split}
    assert shares
    text = render.render_showdown(final)
    for share in shares:
        assert f"{share.numerator}/{share.denominator} each" in text
    assert " win.</b>" in text or " wins.</b>" in text


def test_a_whole_point_has_no_denominator(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    text = render.render_showdown(showdowns[-1])
    scooper = showdowns[-1].scoop_seat
    assert scooper is not None
    assert f"{showdowns[-1].hands[scooper].player.display_name}: 3" in text
    assert "3/1" not in text


def test_a_table_tied_all_round_says_everyone_tied(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, _ = _played(monkeypatch, _tied)
    assert "<b>Everyone tied.</b>" in render.render_showdown(showdowns[-1])


# ----------------------------------------------------------------------- result


def test_a_result_reads_title_settle_up_players_boards_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    sections = text.split("\n\n")
    assert sections[0].startswith(f"🏁 <b>Flip #{result.flip_number} — ")
    headings = [
        line for line in text.splitlines() if line.startswith("<b>") and line.endswith("</b>")
    ]
    assert headings == ["<b>Settle up</b>", "<b>Players</b>", "<b>Boards</b>"]
    assert [label for label in ("Board 1:", "Board 2:", "Hand strength:") if label in text] == [
        "Board 1:",
        "Board 2:",
        "Hand strength:",
    ]
    assert "Hands:" not in text
    assert "<b>Points</b>" not in text
    proof = sections[-1].splitlines()
    assert proof == [
        f"$50 a head · pot {render._amount(result.pot)}",
        STRIP,
        f"Deck seed <code>{fairness.seed_to_hex(result.seed)}</code>",
        f"Check it with <code>/verify {result.flip_number}</code>",
    ]
    assert fairness.seed_to_hex(result.seed) == result.seed.hex()


def test_a_result_has_no_best_five_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    assert "(" not in text
    assert not any(str(card) in text for card in BOARD1 + BOARD2 if "♠" in str(card))


def test_a_result_has_one_line_per_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    assert result.payments
    for payment in result.payments:
        payer = result.hands[payment.payer].player.display_name
        receiver = result.hands[payment.receiver].player.display_name
        assert f"{payer} owes {receiver} ${payment.dollars:,}" in text
    assert text.count(" owes ") == len(result.payments)


def test_a_scoop_result_is_worth_wager_times_z_from_each_loser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    assert result.scoop_seat is not None
    owing = [line for line in text.splitlines() if " owes " in line]
    assert len(owing) == len(result.payments)
    assert all(line.endswith(" $150") for line in owing)
    assert "The scoop pays 3x the wager: $150 from each other player." in text


def test_a_chop_result_shows_the_split_board(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _chop)
    text = render.render_result(result)
    assert ", 1/2 each" in text
    assert "scoops" not in text
    assert "The scoop pays" not in text
    assert " and " in text.splitlines()[0]
    assert text.splitlines()[0].endswith(" win</b>")


def test_an_everyone_tied_result_sends_the_pot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _tied)
    text = render.render_result(result)
    assert text.splitlines()[0] == f"🏁 <b>Flip #{result.flip_number} — everyone tied</b>"
    assert "The pot goes back and nobody owes anything." in text
    assert " owes " not in text.replace("nobody owes anything", "")


def test_a_result_that_was_not_recorded_omits_the_number_and_the_verify_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(replace(result, flip_number=None))
    assert text.splitlines()[0].startswith("🏁 <b>Flip result — ")
    assert "#" not in text
    assert "/verify" not in text
    assert text.splitlines()[-1] == (
        "⚠️ This flip could not be saved, so it has no number, cannot be verified "
        "and does not count in stats."
    )
    assert "Deck seed <code>" in text


def test_a_showdown_says_y_in_words_when_it_was_defaulted_and_z_as_a_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    shown = showdowns[-1]
    assert not shown.y_defaulted
    plain = render.render_showdown(shown)
    assert STRIP in plain
    assert "as many extra cards as fit" not in plain
    resolved = replace(shown, settings=Settings(wager=50, x=5, y=21, z=3), y_defaulted=True)
    text = render.render_showdown(resolved)
    assert "5 private cards each · as many extra cards as fit · scoop pays 3x the wager" in text
    assert "21" not in text
    undefaulted = render.render_showdown(replace(resolved, y_defaulted=False))
    assert "up to 21 extra cards each · scoop pays 3x the wager" in undefaulted


def test_a_defaulted_result_says_as_many_as_fit_and_never_the_resolved_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    assert not result.y_defaulted
    text = render.render_result(replace(result, y_defaulted=True))
    assert "5 private cards each · as many extra cards as fit · scoop pays 3x the wager" in text
    assert "up to 2 extra cards" not in text
    assert "$50 a head · pot $150" in text
    resolved = replace(result, settings=Settings(wager=50, x=5, y=21, z=3))
    assert "up to 21 extra cards each" in render.render_result(resolved)
    assert (
        "21" not in render.render_result(replace(resolved, y_defaulted=True)).split("Deck seed")[0]
    )


@pytest.mark.parametrize(
    ("wanted", "verdict"),
    [
        (_scoop, " scoops all three boards</b>"),
        (_chop, " win</b>"),
        (_tied, " — everyone tied</b>"),
    ],
)
def test_the_title_carries_the_verdict(
    monkeypatch: pytest.MonkeyPatch, wanted: Callable[[FlipOutcome], bool], verdict: str
) -> None:
    _, result = _played(monkeypatch, wanted)
    title = render.render_result(result).splitlines()[0]
    assert title.startswith(f"🏁 <b>Flip #{result.flip_number} — ")
    assert title.endswith(verdict)


def test_the_title_names_a_single_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    def single(outcome: FlipOutcome) -> bool:
        return len(outcome.winners) == 1 and outcome.scoop_seat is None

    _, result = _played(monkeypatch, single)
    winner = result.hands[result.winners[0]].player.display_name
    title = render.render_result(result).splitlines()[0]
    assert title == f"🏁 <b>Flip #{result.flip_number} — {winner} wins</b>"


def test_an_unsaved_result_keeps_the_verdict_in_a_flip_result_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    saved = render.render_result(result).splitlines()[0]
    unsaved = render.render_result(replace(result, flip_number=None)).splitlines()[0]
    assert unsaved == saved.replace(f"Flip #{result.flip_number}", "Flip result")


def test_each_players_line_shows_points_and_net(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    players_block = text.split("<b>Players</b>\n")[1].split("\n\n")[0].splitlines()
    assert len(players_block) == len(result.hands)
    for line, hand in zip(players_block, result.hands, strict=True):
        net = f"+${hand.net_dollars:,}" if hand.net_dollars > 0 else f"-${-hand.net_dollars:,}"
        assert line == (
            f"{hand.seat + 1}. {hand.player.display_name} — "
            f"{render._points_with_unit(hand.points)} · {net}"
        )


def test_a_scoop_result_says_what_the_multiplier_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    assert text.split("\n\n")[0].splitlines()[1] == (
        "The scoop pays 3x the wager: $150 from each other player."
    )


def test_the_scoop_note_needs_a_multiplier_above_one(monkeypatch: pytest.MonkeyPatch) -> None:
    flat = replace(SETTINGS, z=1)
    _, result = _played(monkeypatch, _scoop, settings=flat)
    assert result.scoop_seat is not None
    text = render.render_result(result)
    assert "The scoop pays" not in text
    assert "scoop pays the wager, no bonus" in text
    assert text.count("$50") >= len(result.payments)


def test_the_note_is_for_a_scoop_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _chop)
    assert result.settings.z > 1
    assert "The scoop pays" not in render.render_result(result)


def test_the_table_and_showdown_headers_carry_the_money_and_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    texts = [render.render_table(_table()), *(render.render_showdown(v) for v in showdowns)]
    for text in texts:
        header = text.split("\n\n")[0].splitlines()
        assert header[1].startswith("$50 a head · pot $150 · button Player 1")
        assert header[2] == STRIP
        assert text.splitlines()[-1].startswith("Deck commitment <code>")
        assert text.split("\n\n")[-1].startswith("Deck commitment <code>")
        assert "Deck commitment" not in "\n".join(text.splitlines()[:-1])


def test_cards_are_written_with_suit_symbols() -> None:
    cards = _cards("As 10h")
    assert format_cards(cards) == "A♠ 10♥"
    text = render.render_table(_table(TableStage.BOARD1, extra_count=0, board1=cards + BOARD1[:3]))
    assert "A♠ 10♥" in text
    assert "As" not in text


# ------------------------------------------------------------------------- void


@pytest.mark.parametrize("reason", list(VoidReason))
def test_every_void_reason_has_wording_and_a_seed_when_one_was_dealt(reason: VoidReason) -> None:
    view = VoidView(token="tok", reason=reason, settings=SETTINGS, players=players(3), seed=SEED)
    text = render.render_void(view)
    assert "Flip voided" in text
    assert text.splitlines()[0] == (
        "⚠️ <b>Flip voided</b> — nothing was recorded and nobody owes anything."
    )
    assert text.splitlines()[1] == render._VOID_REASONS[reason]
    assert f"$50 a head · {STRIP}" in text
    assert "Players: Player 11, Player 12 and Player 13" in text
    assert text.endswith(
        f"Deck seed <code>{SEED.hex()}</code>\n"
        "The deal it would have made can still be checked against that seed."
    )


def test_a_void_with_no_seed_says_no_cards_had_been_dealt() -> None:
    view = VoidView(
        token="tok",
        reason=VoidReason.ERROR,
        settings=SETTINGS,
        players=players(2),
    )
    text = render.render_void(view)
    assert "No cards had been dealt." in text
    assert "Deck seed" not in text


@pytest.mark.parametrize("reason", list(VoidReason))
def test_a_notice_built_by_the_sweep_leaves_out_what_it_cannot_know(reason: VoidReason) -> None:
    view = VoidView(token="tok", reason=reason, settings=None, players=(), seed=SEED)
    text = render.render_void(view)
    assert "Flip voided" in text
    assert "a head" not in text
    assert "Players:" not in text
    assert "private cards" not in text
    assert f"Deck seed <code>{SEED.hex()}</code>" in text


def test_a_started_flips_notice_says_y_in_words_when_it_was_defaulted() -> None:
    view = VoidView(
        token="tok",
        reason=VoidReason.ERROR,
        settings=Settings(wager=50, x=5, y=21, z=3),
        request=FlipRequest(wager=50, x=5),
        players=players(3),
        seed=SEED,
    )
    text = render.render_void(view)
    assert "$50 a head · 5 private cards each · as many extra cards as fit · scoop pays 3x" in text
    assert "21" not in text.split("Deck seed")[0]
    assert "number of players" not in text


def test_a_started_flips_notice_with_an_explicit_request_shows_the_figures() -> None:
    view = VoidView(
        token="tok",
        reason=VoidReason.ERROR,
        settings=SETTINGS,
        request=REQUEST,
        players=players(3),
    )
    text = render.render_void(view)
    assert f"$50 a head · {STRIP}" in text
    assert "as many extra cards as fit" not in text


def test_a_lobby_voids_notice_shows_what_was_asked_for() -> None:
    view = VoidView(
        token="tok",
        reason=VoidReason.ERROR,
        settings=None,
        request=FlipRequest(wager=50, x=5),
        players=players(2),
    )
    text = render.render_void(view)
    assert f"$50 a head · {DEFAULTED_STRIP}" in text
    assert "21" not in text


def test_a_notice_with_neither_settings_nor_request_has_no_settings_line() -> None:
    text = render.render_void(VoidView(token="tok", reason=VoidReason.RESTART, seed=SEED))
    assert "a head" not in text
    assert "private cards" not in text


def test_the_watchdog_reason_quotes_the_engines_minutes() -> None:
    text = render.render_void(VoidView(token="tok", reason=VoidReason.TIMED_OUT))
    assert f"{timing.FLIP_WATCHDOG_SECONDS / 60:.0f} minutes" in text


def test_a_void_notice_carries_no_buttons() -> None:
    assert keyboards.markup_for(VoidView(token="tok", reason=VoidReason.RESTART)) is None


# ------------------------------------------------------------------ html safety


def _assert_escaped(text: str) -> None:
    assert "<script>" not in text
    assert "<b>Ev&il" not in text
    assert EVIL_ESCAPED in text


def test_names_are_escaped_in_the_lobby() -> None:
    roster = (Player(user_id=1, display_name=EVIL), *players(2, first_id=20))
    for status in LobbyStatus:
        _assert_escaped(render.render_lobby(_lobby(3, status=status, names=roster)))


def test_names_are_escaped_in_the_table() -> None:
    roster = (*players(2), Player(user_id=3, display_name=EVIL))
    view = _table(seated=roster)
    _assert_escaped(render.render_table(replace(view, button_seat=2)))
    _assert_escaped(render.render_table(view))


def test_names_are_escaped_in_the_showdown_and_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    showdowns, result = _played(monkeypatch, _scoop)
    for showdown in showdowns:
        renamed = _renamed(showdown, EVIL)
        assert isinstance(renamed, ShowdownView)
        _assert_escaped(render.render_showdown(renamed))
    renamed_result = _renamed(result, EVIL)
    assert isinstance(renamed_result, ResultView)
    text = render.render_result(renamed_result)
    _assert_escaped(text)
    owing = next(line for line in text.splitlines() if " owes " in line)
    assert "<script>" not in owing
    assert EVIL_ESCAPED in owing


def test_names_are_escaped_in_the_void_notice() -> None:
    view = VoidView(
        token="tok",
        reason=VoidReason.RESTART,
        settings=SETTINGS,
        players=(Player(user_id=1, display_name=EVIL), *players(1, first_id=20)),
    )
    _assert_escaped(render.render_void(view))


def test_a_long_name_is_clipped() -> None:
    long_name = "W" * (render.NAME_LIMIT * 3)
    roster = (Player(user_id=1, display_name=long_name), *players(1, first_id=20))
    text = render.render_lobby(_lobby(2, names=roster))
    assert long_name not in text
    assert "W" * (render.NAME_LIMIT - 1) + "…" in text
    assert "W" * render.NAME_LIMIT not in text


def test_a_name_at_the_limit_is_left_alone() -> None:
    name = "W" * render.NAME_LIMIT
    roster = (Player(user_id=1, display_name=name), *players(1, first_id=20))
    assert f"1. {name}\n" in render.render_lobby(_lobby(2, names=roster))


@pytest.mark.parametrize("hostile", ["<" * 200, EVIL * 10, "&" * 500])
def test_render_keeps_every_message_inside_telegrams_limit(hostile: str) -> None:
    roster = tuple(
        Player(user_id=index, display_name=hostile) for index in range(SETTINGS.max_players)
    )
    views: list[FlipView] = [
        _lobby(len(roster), names=roster),
        _table(seated=roster),
        VoidView(token="tok", reason=VoidReason.ERROR, settings=SETTINGS, players=roster),
        _lobby(len(roster), names=roster, request=FlipRequest(wager=50, x=5)),
        replace(_table(seated=roster), y_defaulted=True),
        VoidView(
            token="tok",
            reason=VoidReason.ERROR,
            request=FlipRequest(wager=50, x=5),
            players=roster,
        ),
    ]
    for view in views:
        assert len(render.render(view)) <= render.MESSAGE_LIMIT


@pytest.mark.parametrize("hostile", ["<" * 200, EVIL * 10, "&" * 500, "W" * 32])
def test_a_chop_among_eight_hostile_names_fits_a_message(
    monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    showdowns, result = _played(monkeypatch, _chop)
    crowd = tuple(range(SETTINGS.max_players))

    def eight(hands: tuple[HandView, ...]) -> tuple[HandView, ...]:
        return tuple(
            replace(
                hands[seat % len(hands)],
                seat=seat,
                player=Player(user_id=seat, display_name=hostile),
            )
            for seat in crowd
        )

    def chopped(boards: tuple[BoardView, ...]) -> tuple[BoardView, ...]:
        return tuple(replace(board, winners=crowd, share=Fraction(1, 8)) for board in boards)

    final = showdowns[-1]
    showdown = replace(final, hands=eight(final.hands), boards=chopped(final.boards), winners=crowd)
    settled = replace(
        result, hands=eight(result.hands), boards=chopped(result.boards), winners=crowd
    )
    for view in (showdown, settled):
        text = render.render(view)
        assert len(text) <= render.MESSAGE_LIMIT
        assert "<script>" not in text
        assert text.count("<b>") == text.count("</b>")
        assert text.count("<code>") == text.count("</code>")


def test_an_oversized_message_is_cut_on_a_line_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "NAME_LIMIT", 10_000)
    roster = tuple(
        Player(user_id=index, display_name="x" * 900) for index in range(SETTINGS.max_players)
    )
    text = render.render(_lobby(len(roster), names=roster))
    assert len(text) <= render.MESSAGE_LIMIT
    assert text.endswith("\n…")
    assert all(len(line) <= 900 + 4 for line in text.splitlines())


def test_render_dispatches_every_view_type(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, result = _played(monkeypatch, _scoop)
    assert render.render(showdowns[-1]) == render.render_showdown(showdowns[-1])
    assert render.render(result) == render.render_result(result)
    assert render.render(_table()) == render.render_table(_table())
    commitment = render.render(CommitmentView(token="tok", commitment=COMMITMENT))
    assert f"<code>{COMMITMENT}</code>" in commitment


# ------------------------------------------------------------------------- peek


def _widest_hand(hole_count: int, extra_count: int) -> tuple[tuple[Card, ...], tuple[Card, ...]]:
    """The longest-reading hand of this shape: as many tens as there are, then the rest.

    ``10♥`` is three characters where every other card is two, and a deck holds
    four tens, so this is the widest a real hand of a given size can read.
    """
    tens = [Card(Rank.TEN, suit) for suit in Suit]
    pool = tens + [card for card in FULL_DECK if card.rank is not Rank.TEN]
    held = pool[: hole_count + extra_count]
    return tuple(held[:hole_count]), tuple(held[hole_count:])


def _made(board: Board, description: str, cards: str) -> MadeHand:
    """A made hand built from real cards, so its wording is the evaluator's own."""
    five = _cards(cards)
    hand = BestHand(rank=rank_five(five), cards=five)
    assert hand.description == description
    return MadeHand(board=board, hand=hand)


_SHORTEST_RUNG_LINE: Final = 20
"""The longest line the shortest rung of the ladder can produce: ``HS: full house 10/10``.

Pinned here and proved exhaustively by
:func:`test_every_hand_the_evaluator_can_rank_has_a_short_name_that_fits_the_ladder`,
so the sweep over every shape of hand can bound a pop-up without having to find
the hand that would make the longest one.
"""

_WORST_POP_UP: Final = 189
"""The longest pop-up a flip can raise: 21 cards, 16 of them just dealt, three boards.

Eleven characters inside the limit, which is the ladder's whole margin. A change
that eats into it fails the sweep rather than quietly cutting somebody's cards.
"""

_BIG_READING: Final = (
    _made(Board.BOARD1, "full house, queens full of sevens", "Qs Qh Qd 7s 7h"),
    _made(Board.BOARD2, "four of a kind, queens", "Qs Qh Qd Qc 7s"),
    _made(Board.HAND_STRENGTH, "straight flush, nine high", "9s 8s 7s 6s 5s"),
)
"""Three of the longest readings there are, for the cases where nothing else fits."""


def _big_reading(boards: int) -> tuple[MadeHand, ...]:
    """:data:`_BIG_READING` as a pop-up would hold it with ``boards`` community boards out."""
    return _BIG_READING[:boards] + _BIG_READING[-1:]


def test_a_peek_lists_only_the_hole_cards_before_the_extras_are_out() -> None:
    hole = _cards("As 10h 2c 3d 4h")
    text = render.render_peek(PeekAnswer(seat=1, hole=hole))
    assert text.splitlines() == ["Your cards · seat 2 · 5 cards", "A♠ 10♥ 4♥ 3♦ 2♣"]
    assert "*" not in text


def test_a_peek_marks_the_extra_cards_wherever_they_sort() -> None:
    hole = _cards("As 10h 2c 3d 4h")
    extra = _cards("Kd Qs")
    text = render.render_peek(PeekAnswer(seat=0, hole=hole, extra=extra))
    assert text.splitlines() == [
        "Your cards · seat 1 · 7 cards",
        "A♠ K♦* Q♠* 10♥ 4♥ 3♦ 2♣",
        "* just dealt",
    ]


def test_a_hand_is_sorted_by_rank_and_then_by_suit_downwards() -> None:
    hand = _cards("2c 2s 2h 2d Ac 10s 10c Kh")
    text = render.render_peek(PeekAnswer(seat=0, hole=hand))
    assert text.splitlines()[1] == "A♣ K♥ 10♠ 10♣ 2♠ 2♥ 2♦ 2♣"


def test_the_ace_of_a_five_high_straight_still_sorts_at_the_top() -> None:
    wheel = _cards("5c 4d 3h 2s Ac")
    answer = PeekAnswer(
        seat=0,
        hole=wheel,
        made=(_made(Board.HAND_STRENGTH, "straight, five high", "5c 4d 3h 2s Ac"),),
    )
    text = render.render_peek(answer)
    assert text.splitlines()[1] == "A♣ 5♣ 4♦ 3♥ 2♠"
    assert "Hand strength: straight, five high" in text


def test_a_single_card_peek_is_not_pluralised() -> None:
    assert render.render_peek(PeekAnswer(seat=0, hole=_cards("As"))).startswith(
        "Your cards · seat 1 · 1 card\n"
    )


def test_the_largest_hand_fits_in_the_popup() -> None:
    settings = Settings(wager=50, x=16, y=5, z=3)
    assert settings.max_players == 2
    hole, extra = _widest_hand(settings.x, settings.y)
    answer = PeekAnswer(seat=1, hole=hole, extra=extra)
    assert len(answer.cards) == 21
    text = render.render_peek(answer)
    assert text.count("10") == 4
    assert text.count("*") == settings.y + 1
    assert len(text) < render.ALERT_LIMIT
    assert render.ALERT_LIMIT == 200


# -------------------------------------------------------- what a hand has made


def test_a_peek_names_what_the_hand_has_on_each_board_that_is_out() -> None:
    answer = PeekAnswer(
        seat=0,
        hole=_cards("As Ks Qs 9s 7d"),
        made=(
            _made(Board.BOARD1, "flush, ace high", "As Ks Qs 9s 7s"),
            _made(Board.BOARD2, "pair of nines", "9s 9c Ks Qs 7d"),
            _made(Board.HAND_STRENGTH, "ace high", "As Ks Qs 9s 7d"),
        ),
    )
    assert render.render_peek(answer).splitlines() == [
        "Your cards · seat 1 · 5 cards",
        "A♠ K♠ Q♠ 9♠ 7♦",
        "Board 1: flush, ace high · A♠ K♠ Q♠ 9♠ 7♠",
        "Board 2: pair of nines · 9♠ 9♣ K♠ Q♠ 7♦",
        "Hand strength: ace high · A♠ K♠ Q♠ 9♠ 7♦",
    ]


def test_a_peek_before_any_board_is_out_says_only_what_the_hand_is_worth() -> None:
    answer = PeekAnswer(
        seat=2,
        hole=_cards("As Ks Qs 9s 7d"),
        made=(_made(Board.HAND_STRENGTH, "ace high", "As Ks Qs 9s 7d"),),
    )
    text = render.render_peek(answer)
    assert text.splitlines()[-1] == "Hand strength: ace high · A♠ K♠ Q♠ 9♠ 7♦"
    assert "Board 1" not in text
    assert "Board 2" not in text


def test_a_peek_that_could_not_be_read_still_shows_the_cards() -> None:
    text = render.render_peek(PeekAnswer(seat=0, hole=_cards("As Ks Qs 9s 7d")))
    assert text.splitlines() == ["Your cards · seat 1 · 5 cards", "A♠ K♠ Q♠ 9♠ 7♦"]


def test_a_hand_too_big_for_the_cards_drops_to_the_names_alone() -> None:
    hole, extra = _widest_hand(5, 4)
    answer = PeekAnswer(seat=0, hole=hole, extra=extra, made=_BIG_READING)
    lines = render.render_peek(answer).splitlines()
    assert lines[-3:] == [
        "Board 1: full house, queens full of sevens",
        "Board 2: four of a kind, queens",
        "Hand strength: straight flush, nine high",
    ]
    assert len(render.render_peek(answer)) <= render.ALERT_LIMIT


def test_the_biggest_hand_of_all_drops_to_the_shortest_names() -> None:
    hole, extra = _widest_hand(5, 16)
    answer = PeekAnswer(seat=0, hole=hole, extra=extra, made=_BIG_READING)
    text = render.render_peek(answer)
    assert len(answer.cards) == 21
    assert text.splitlines()[-3:] == [
        "B1: full house Q/7",
        "B2: quads Q",
        "HS: str flush 9",
    ]
    assert len(text) <= render.ALERT_LIMIT
    # The cards are never what gives way: all 21 of them are still there.
    assert len(text.splitlines()[1].split()) == 21


def test_a_royal_flush_is_named_as_one_and_shortens_to_a_word() -> None:
    royal = _made(Board.BOARD1, "royal flush", "As Ks Qs Js 10s")
    assert render._made_named(royal) == "Board 1: royal flush"
    assert render._made_short(royal) == "B1: royal"


@pytest.mark.parametrize(
    ("cards", "long_form", "short_form"),
    [
        pytest.param("As Ks Qs Js 10s", "royal flush", "royal", id="royal"),
        pytest.param("9s 8s 7s 6s 5s", "straight flush, nine high", "str flush 9", id="str-flush"),
        pytest.param(
            "5s 4s 3s 2s As", "straight flush, five high", "str flush 5", id="steel-wheel"
        ),
        pytest.param("Qs Qh Qd Qc 7s", "four of a kind, queens", "quads Q", id="quads"),
        pytest.param(
            "Qs Qh Qd 7s 7h", "full house, queens full of sevens", "full house Q/7", id="full-house"
        ),
        pytest.param("As Ks Qs 9s 7s", "flush, ace high", "flush A", id="flush"),
        pytest.param("10s 9h 8d 7c 6s", "straight, ten high", "straight 10", id="straight"),
        pytest.param("Qs Qh Qd 9s 7h", "three of a kind, queens", "trips Q", id="trips"),
        pytest.param(
            "Qs Qh 7d 7c 9s", "two pair, queens and sevens", "two pair Q/7", id="two-pair"
        ),
        pytest.param("Qs Qh 9d 7c 5s", "pair of queens", "pair Q", id="pair"),
        pytest.param("As Kh 9d 7c 5s", "ace high", "A high", id="high-card"),
        pytest.param("5s 4h 3d 2c As", "straight, five high", "straight 5", id="wheel"),
    ],
)
def test_every_kind_of_hand_is_named_in_both_forms(
    cards: str, long_form: str, short_form: str
) -> None:
    made = _made(Board.BOARD2, long_form, cards)
    assert render._made_named(made) == f"Board 2: {long_form}"
    assert render._made_short(made) == f"B2: {short_form}"


def _legal_shapes() -> list[tuple[int, int, int]]:
    """Every ``(players, private cards, extra cards)`` a flip can really deal.

    Every private-card count in its limits, every player count that fits it, and
    the extra cards each asking for 0 to :data:`~esgbot.game.rules.Y_MAX` of them
    comes to -- which the deck decides, so many requests give the same hand.
    """
    shapes = {
        (count, x, extra_rounds_for(player_count=count, x=x, y=y))
        for x in range(X_MIN, X_MAX + 1)
        for count in range(MIN_PLAYERS, max_players(x) + 1)
        for y in range(Y_MIN, Y_MAX + 1)
    }
    return sorted(shapes)


def test_every_shape_of_hand_a_flip_can_deal_fits_the_pop_up() -> None:
    """The proof that the ladder has a bottom, over every hand a flip can deal.

    Exhaustive over the shapes rather than the deals: every private-card count,
    every player count that fits it, every extra-card count the deck allows,
    every seat and every number of boards out, each with the longest-reading
    hand of that size. Two things are checked of each. The pop-up as it really
    renders fits and is never cut, with the longest readings there are; and the
    arithmetic bound -- the head as it really renders, plus the longest line the
    shortest rung can produce for *any* hand -- fits too, which is what says the
    ladder never runs out of rungs whatever the cards come to.
    """
    worst = 0
    for count, x, extra in _legal_shapes():
        hole, held = _widest_hand(x, extra)
        for seat in range(count):
            bare = render.render_peek(PeekAnswer(seat=seat, hole=hole, extra=held))
            for boards in range(BOARD_COUNT + 1):
                made = _big_reading(boards)
                where = f"{count} players, x={x}, extra={extra}, seat={seat}, boards={boards}"
                text = render.render_peek(PeekAnswer(seat=seat, hole=hole, extra=held, made=made))
                assert len(text) <= render.ALERT_LIMIT, f"{where}: {len(text)}"
                assert not text.endswith("…"), f"{where}: no rung fitted"
                assert len(text.splitlines()[1].split()) == x + extra, where
                bound = len(bare) + len(made) * (1 + _SHORTEST_RUNG_LINE)
                assert bound <= render.ALERT_LIMIT, f"{where}: {bound}"
                worst = max(worst, bound)
    assert worst == _WORST_POP_UP


@pytest.mark.parametrize("sweep", range(8))
def test_real_deals_of_every_size_fit_the_pop_up(sweep: int) -> None:
    """The same limit over deals that were really dealt and really read.

    The sweep above bounds the renderer; this one walks the whole path --
    :func:`~esgbot.engine.read_hand` on a dealt hand, rendered -- across the
    extremes of the deck: the biggest hands two players can hold, and the
    smallest a full table can.
    """
    shapes = [(2, 5, Y_MAX), (2, 21, 0), (2, 16, 5), (3, 14, Y_MAX), (8, 5, 0), (4, 5, Y_MAX)]
    count, x, y = shapes[sweep % len(shapes)]
    extra = extra_rounds_for(player_count=count, x=x, y=y)
    seed = (sweep * 7919 + 13).to_bytes(fairness.SEED_SIZE, "big")
    deal = deal_cards(seed=seed, player_ids=list(range(count)), x=x, y=y)
    assert deal.extra_count == extra
    for hand in deal.hands:
        for shown in range(len(deal.boards) + 1):
            final = shown == len(deal.boards)
            held = hand.extra if final else ()
            answer = PeekAnswer(
                seat=hand.seat,
                hole=hand.hole,
                extra=held,
                made=read_hand(hand.hole + held, deal.boards[:shown]),
            )
            text = render.render_peek(answer)
            assert len(text) <= render.ALERT_LIMIT, f"{count}p x={x} extra={extra}: {len(text)}"
            assert len(text.splitlines()) == 2 + bool(held) + shown + 1


def test_every_hand_the_evaluator_can_rank_has_a_short_name_that_fits_the_ladder() -> None:
    """The shortest rung's worst line, over every rank the describer can be handed.

    Exhaustive over the function's whole domain -- every category against every
    pair of ranks, legal or not -- rather than over the hands a deck can make, so
    the bound holds however the evaluator's tiebreaks are arranged next.
    """
    lines = [
        render._made_short(MadeHand(board=board, hand=BestHand(rank=rank, cards=())))
        for board in Board
        for category in Category
        for high in Rank
        for low in Rank
        for rank in [HandRank(category=category, tiebreak=(high, low, high, low, high))]
    ]
    longest = max(len(line) for line in lines)
    assert longest == _SHORTEST_RUNG_LINE
    assert {line for line in lines if len(line) == longest} == {
        f"{mark}: full house 10/10" for mark in ("B1", "B2", "HS")
    }


# ---------------------------------------------------------------------- notices


@pytest.mark.parametrize("notice", list(Notice))
def test_every_notice_has_wording_inside_the_popup_limit(notice: Notice) -> None:
    text = render.render_notice(notice)
    assert text.strip()
    assert len(text) <= render.ALERT_LIMIT


def test_the_notice_wording_covers_exactly_the_notices() -> None:
    assert set(render._NOTICES) == set(Notice)


# ------------------------------------------------------------- command replies


def test_a_settings_error_is_escaped() -> None:
    with pytest.raises(SettingsError) as caught:
        parse_flip_request(["<b>50</b>", "5", "2", "3"])
    text = render.render_settings_error(caught.value)
    complaint = text.split("\n\n")[0]
    assert complaint.startswith("⚠️ Wager must be")
    assert "<b>" not in complaint
    assert "&lt;b&gt;50&lt;/b&gt;" in complaint


def test_a_field_error_carries_the_short_usage() -> None:
    for tokens in (["50", "99"], ["50", "5", "2", "0"], ["$50", "5"], ["50", "x", "2", "3"]):
        with pytest.raises(SettingsError) as caught:
            parse_flip_request(tokens)
        assert caught.value.field
        text = render.render_settings_error(caught.value)
        assert text.startswith("⚠️ ")
        assert text.endswith(render.USAGE_SHORT)
        assert text == f"⚠️ {render.settings_error_text(caught.value)}\n\n{render.USAGE_SHORT}"
        assert "What each number means" not in text
    assert render.USAGE_SHORT.endswith(
        "Send <code>/flip</code> on its own for what each number means."
    )


def test_a_count_error_carries_the_full_usage() -> None:
    for tokens, got in ((["50"], "Got 1."), (["50", "5", "2", "3", "4"], "Got 5.")):
        with pytest.raises(SettingsError) as caught:
            parse_flip_request(tokens)
        assert caught.value.field == ""
        text = render.render_settings_error(caught.value)
        assert text.startswith("⚠️ ")
        assert got in text.split("\n\n")[0]
        assert text.endswith(render.USAGE)
        assert "<b>What each number means</b>" in text


def test_a_settings_error_names_the_setting_in_words() -> None:
    with pytest.raises(SettingsError) as caught:
        parse_flip_request(["50", "99"])
    text = render.render_settings_error(caught.value)
    assert text.startswith("⚠️ Private cards must be a whole number from 5 to 21. Got 99.")
    assert "X" not in text.split("\n\n")[0]


def test_the_usage_teaches_the_syntax_the_examples_the_limits_and_the_letters() -> None:
    usage = render.USAGE
    assert usage.splitlines()[0] == "<b>Start a flip</b>"
    assert (
        "<code>/flip &lt;wager&gt; &lt;private cards&gt; [extra cards] [scoop multiplier]</code>"
        in usage
    )
    assert "<code>/flip 50 5 2 3</code>" in usage
    assert "<code>/flip 50 5</code>" in usage
    legend_heading = "<b>What each number means</b>"
    assert legend_heading in usage
    before, legend = usage.split(legend_heading)
    for limit in (WAGER_MIN, WAGER_MAX, X_MIN, X_MAX, Y_MIN, Y_MAX, Z_MIN, Z_MAX):
        assert f"{limit:,}" in legend
    assert "private cards (X)" in legend
    assert "extra cards (Y)" in legend
    assert "scoop multiplier (Z)" in legend
    # The legend is the one place the letters appear.
    for letter in ("(X)", "(Y)", "(Z)"):
        assert usage.count(letter) == 1
        assert letter not in before
    assert not re.search(
        r"\b[XYZ]\b", before + legend.replace("(X)", "").replace("(Y)", "").replace("(Z)", "")
    )
    assert "as many extra cards as fit" in usage
    assert "the number of players" in usage
    assert "needs an extra-cards number in front of it" in usage


def test_the_new_command_replies_name_their_commands_and_fit_a_message() -> None:
    assert "<b>/again</b>" in render.AGAIN_USAGE
    assert "<code>/flip</code>" in render.AGAIN_USAGE
    assert "<code>/flip 50 5 2 3</code>" in render.NO_FLIP_TO_REPEAT
    assert "nothing to repeat" in render.NO_FLIP_TO_REPEAT
    assert "/flip" in render.NO_FLIP_TO_REPEAT
    for text in (render.USAGE, render.USAGE_SHORT, render.AGAIN_USAGE, render.NO_FLIP_TO_REPEAT):
        assert len(text) <= render.MESSAGE_LIMIT


def test_the_new_constants_carry_no_markup_but_the_deliberate_kind() -> None:
    for text in (render.USAGE, render.USAGE_SHORT, render.AGAIN_USAGE, render.NO_FLIP_TO_REPEAT):
        bare = text.replace("<code>", "").replace("</code>", "").replace("<b>", "")
        bare = bare.replace("</b>", "").replace("&lt;", "").replace("&gt;", "")
        assert "<" not in bare
        assert ">" not in bare


def test_the_settings_error_text_alone_is_escaped() -> None:
    error = SettingsError("bad <i>input</i> & more")
    assert render.settings_error_text(error) == "bad &lt;i&gt;input&lt;/i&gt; &amp; more"


def test_already_open_names_the_open_flips_settings() -> None:
    text = render.render_already_open(FlipRequest(wager=75, x=6, y=1, z=4))
    assert "already open" in text
    assert text.splitlines()[1] == (
        "$75 a head · 6 private cards each · up to 1 extra card each · scoop pays 4x the wager"
    )


def test_already_open_for_a_defaulted_flip_invents_no_figures() -> None:
    text = render.render_already_open(FlipRequest(wager=75, x=6))
    assert text.splitlines()[1] == (
        "$75 a head · 6 private cards each · as many extra cards as fit · "
        "scoop pays the wager times the number of players"
    )
    assert "21" not in text


def test_already_open_for_a_started_flip_names_the_figures_start_settled() -> None:
    """Once Start has fixed the player list the scoop multiplier is a number.

    The group is being sent to a flip that is already paying that number, so
    saying "the number of players" would be describing a question it has
    answered.
    """
    text = render.render_already_open(
        FlipRequest(wager=75, x=6), Settings(wager=75, x=6, y=Y_DEFAULT, z=3)
    )
    assert text.splitlines()[1] == (
        "$75 a head · 6 private cards each · as many extra cards as fit · scoop pays 3x the wager"
    )
    # A defaulted Y stays in words, because 21 would promise cards the deck has not got.
    assert "21" not in text


def test_a_big_wager_gets_thousands_separators() -> None:
    text = render.render_already_open(FlipRequest(wager=1_000_000, x=6, y=1, z=4))
    assert "$1,000,000 a head" in text


def test_fractions_render_exactly() -> None:
    assert render._points(Fraction(3, 2)) == "3/2"
    assert render._points(Fraction(2)) == "2"


def test_a_board_that_is_the_nuts_says_so_in_every_form_a_pop_up_can_take() -> None:
    flush = _made(Board.BOARD1, "flush, ace high", "As Ks Qs 9s 2s")
    nuts = MadeHand(board=flush.board, hand=flush.hand, nuts=True)

    assert render._made_in_full(nuts).startswith("Board 1: flush, ace high (NUTS) · ")
    assert render._made_named(nuts) == "Board 1: flush, ace high (NUTS)"
    assert render._made_short(nuts) == "B1: flush A (NUTS)"
    assert render._made_tersest(nuts) == "B1: flush A NUTS"
    for form in (render._made_named, render._made_short, render._made_tersest):
        assert "NUTS" not in form(flush)


def test_the_widest_hand_with_the_nuts_on_both_boards_still_fits_the_pop_up() -> None:
    reading = tuple(
        MadeHand(board=made.board, hand=made.hand, nuts=made.board is not Board.HAND_STRENGTH)
        for made in _big_reading(2)
    )
    for count, x, extra in _legal_shapes():
        hole, held = _widest_hand(x, extra)
        text = render.render_peek(PeekAnswer(seat=count - 1, hole=hole, extra=held, made=reading))
        assert len(text) <= render.ALERT_LIMIT, f"{count} players, x={x}, extra={extra}"
        assert not text.endswith("…")
        assert len(text.splitlines()[1].split()) == x + extra
        assert text.count("NUTS") == 2

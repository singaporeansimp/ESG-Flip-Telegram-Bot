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
from collections.abc import Callable
from dataclasses import replace
from fractions import Fraction

import pytest

from esgbot.engine import (
    CommitmentView,
    FlipView,
    LobbyStatus,
    LobbyView,
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
    timing,
)
from esgbot.game import fairness
from esgbot.game.cards import Card, Rank, Suit, format_cards
from esgbot.game.rules import (
    MIN_PLAYERS,
    Settings,
    SettingsError,
    parse_settings_tokens,
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
PLAYER_IDS = [11, 12, 13]
EVIL = "<b>Ev&il</b> \"Bob\" <script>alert('x')</script>"
EVIL_ESCAPED = "&lt;b&gt;Ev&amp;il&lt;/b&gt;"
SEED = bytes(range(32))
COMMITMENT = fairness.commitment(SEED)


def _starter() -> Player:
    return players(1)[0]


def _lobby(
    count: int,
    *,
    status: LobbyStatus = LobbyStatus.OPEN,
    settings: Settings = SETTINGS,
    names: tuple[Player, ...] | None = None,
) -> LobbyView:
    roster = names if names is not None else players(count)
    return LobbyView(
        token="tok", settings=settings, status=status, starter=roster[0], players=roster
    )


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
    assert "New ESG flip" in text
    assert "$50 a head · X 5 · Y 2 · Z 3" in text
    assert "Up to 8 players." in text
    assert "<b>Players 3/8</b>" in text
    assert "1. Player 11\n2. Player 12\n3. Player 13" in text
    assert "Opened by Player 11" in text


def test_the_player_cap_is_forty_two_over_x() -> None:
    text = render.render_lobby(_lobby(2, settings=Settings(wager=50, x=21, y=0, z=1)))
    assert "Up to 2 players." in text
    assert "<b>Players 2/2</b>" in text


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
    assert heading in text
    assert "ecorded" in text
    assert "Up to" not in text
    assert keyboards.markup_for(view) is None


def test_an_open_lobby_carries_buttons() -> None:
    assert keyboards.markup_for(_lobby(3)) is not None


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
    assert "Extra cards dealt: 2 cards each" in after


def test_a_single_card_is_not_pluralised() -> None:
    text = render.render_table(_table(TableStage.PEEKING, extra_count=1))
    assert "Extra cards dealt: 1 card each" in text


def test_the_table_names_the_button_commitment_and_pot() -> None:
    text = render.render_table(_table())
    assert "button Player 12" in text
    assert f"<code>{COMMITMENT}</code>" in text
    assert "Pot $150" in text


def test_the_peek_stage_states_the_showdown_rule_in_the_engines_numbers() -> None:
    text = render.render_table(_table(peeked=2))
    window = f"{timing.PEEK_WINDOW_SECONDS:.0f} seconds"
    everyone = f"{timing.PEEK_ALL_SECONDS:.0f} seconds"
    assert f"Showdown in {window}, or {everyone} after everyone has peeked." in text
    assert "Peeked: 2 of 3." in text


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
    assert "Extra cards dealt" not in text


def test_a_flip_that_asked_for_no_extra_cards_does_not_blame_the_deck() -> None:
    no_extras = replace(SETTINGS, y=0)
    text = render.render_table(replace(_table(extra_count=0), settings=no_extras))
    assert "No extra cards: this flip asked for none." in text
    assert "the deck was too short" not in text
    assert "deck" not in text
    assert "Extra cards dealt" not in text
    shortage = render.render_table(_table(extra_count=0))
    assert text != shortage


def test_a_voided_table_keeps_its_cards_but_loses_the_rule_and_the_button() -> None:
    view = _table(voided=True, board1=BOARD1, board2=BOARD2)
    text = render.render_table(view)
    assert format_cards(BOARD1) in text
    assert "7 cards" in text
    assert "voided" in text
    assert "Showdown in" not in text
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
    assert "<b>Boards</b>" not in text
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
        assert f"{hand.player.display_name} {hand.points}" in text
    assert "scoops all three boards" in text
    assert "scoops" not in render.render_showdown(showdowns[-2])


def test_a_chopped_board_shows_the_share(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, _ = _played(monkeypatch, _chop)
    final = showdowns[-1]
    shares = {board.share for board in final.boards if board.split}
    assert shares
    text = render.render_showdown(final)
    for share in shares:
        assert f"{share.numerator}/{share.denominator} each" in text
    assert " win." in text or " wins." in text


def test_a_whole_point_has_no_denominator(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, _ = _played(monkeypatch, _scoop)
    text = render.render_showdown(showdowns[-1])
    scooper = showdowns[-1].scoop_seat
    assert scooper is not None
    assert f"{showdowns[-1].hands[scooper].player.display_name} 3" in text
    assert "3/1" not in text


def test_a_table_tied_all_round_says_everyone_tied(monkeypatch: pytest.MonkeyPatch) -> None:
    showdowns, _ = _played(monkeypatch, _tied)
    assert "Everyone tied." in render.render_showdown(showdowns[-1])


# ----------------------------------------------------------------------- result


def test_a_result_reads_flip_boards_points_verdict_and_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(result)
    assert f"Flip #{result.flip_number} result" in text
    assert "$50 wager" in text
    assert [label for label in ("Board 1:", "Board 2:", "Hands:") if label in text] == [
        "Board 1:",
        "Board 2:",
        "Hands:",
    ]
    assert "Points: " in text
    assert "scoops all three boards" in text
    assert f"Deck seed: <code>{fairness.seed_to_hex(result.seed)}</code>" in text
    assert fairness.seed_to_hex(result.seed) == result.seed.hex()
    assert f"/verify {result.flip_number}" in text


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
    assert text.count("$150") == len(result.payments)


def test_a_chop_result_shows_the_split_board(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _chop)
    text = render.render_result(result)
    assert " each)" in text
    assert "scoops" not in text
    assert " and " in text


def test_an_everyone_tied_result_sends_the_pot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _, result = _played(monkeypatch, _tied)
    text = render.render_result(result)
    assert "Everyone tied." in text
    assert "The pot goes back and nobody owes anything." in text
    assert " owes " not in text.replace("nobody owes anything", "")


def test_a_result_that_was_not_recorded_omits_the_number_and_the_verify_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _played(monkeypatch, _scoop)
    text = render.render_result(replace(result, flip_number=None))
    assert "<b>Flip result</b>" in text
    assert "#" not in text
    assert "/verify" not in text
    assert "could not be saved" in text
    assert "does not count in stats" in text


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
    assert render._VOID_REASONS[reason] in text
    assert "Nothing was recorded and nobody owes anything." in text
    assert f"Deck seed: <code>{SEED.hex()}</code>" in text
    assert "$50 a head" in text
    assert "Players: Player 11, Player 12 and Player 13" in text


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
    assert f"Deck seed: <code>{SEED.hex()}</code>" in text


def test_the_watchdog_reason_quotes_the_engines_minutes() -> None:
    text = render.render_void(VoidView(token="tok", reason=VoidReason.TIMED_OUT))
    assert f"{timing.FLIP_WATCHDOG_SECONDS / 60:.0f} minutes" in text


def test_a_void_notice_carries_no_buttons() -> None:
    assert keyboards.markup_for(VoidView(token="tok", reason=VoidReason.RESTART)) is None


# ------------------------------------------------------------------ html safety


def _assert_escaped(text: str) -> None:
    assert "<script>" not in text
    assert "<b>Ev" not in text
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
    ]
    for view in views:
        assert len(render.render(view)) <= render.MESSAGE_LIMIT


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
    tens = [Card(Rank.TEN, suit) for suit in Suit]
    pool = [tens[index % 4] for index in range(hole_count + extra_count)]
    return tuple(pool[:hole_count]), tuple(pool[hole_count:])


def test_a_peek_lists_only_the_hole_cards_before_the_extras_are_out() -> None:
    hole = _cards("As 10h 2c 3d 4h")
    text = render.render_peek(PeekAnswer(seat=1, hole=hole))
    assert text.splitlines() == ["Seat 2 · 5 cards", "A♠ 10♥ 2♣ 3♦ 4♥"]
    assert "*" not in text


def test_a_peek_marks_the_extra_cards() -> None:
    hole = _cards("As 10h 2c 3d 4h")
    extra = _cards("Kd Qs")
    text = render.render_peek(PeekAnswer(seat=0, hole=hole, extra=extra))
    assert text.splitlines() == ["Seat 1 · 7 cards", "A♠ 10♥ 2♣ 3♦ 4♥ K♦* Q♠*", "* new"]


def test_a_single_card_peek_is_not_pluralised() -> None:
    assert render.render_peek(PeekAnswer(seat=0, hole=_cards("As"))).startswith("Seat 1 · 1 card\n")


def test_the_largest_hand_fits_in_the_popup() -> None:
    settings = Settings(wager=50, x=16, y=5, z=3)
    assert settings.max_players == 2
    hole, extra = _widest_hand(settings.x, settings.y)
    answer = PeekAnswer(seat=1, hole=hole, extra=extra)
    assert len(answer.cards) == 21
    text = render.render_peek(answer)
    assert text.count("10") == 21
    assert text.count("*") == settings.y + 1
    assert len(text) < render.ALERT_LIMIT
    assert render.ALERT_LIMIT == 200


# ---------------------------------------------------------------------- notices


@pytest.mark.parametrize("notice", list(Notice))
def test_every_notice_has_wording_inside_the_popup_limit(notice: Notice) -> None:
    text = render.render_notice(notice)
    assert text.strip()
    assert len(text) <= render.ALERT_LIMIT


def test_the_notice_wording_covers_exactly_the_notices() -> None:
    assert set(render._NOTICES) == set(Notice)


# ------------------------------------------------------------- command replies


def test_a_settings_error_is_escaped_and_comes_with_the_usage_example() -> None:
    with pytest.raises(SettingsError) as caught:
        parse_settings_tokens(["<b>50</b>", "5", "2", "3"])
    text = render.render_settings_error(caught.value)
    complaint = text.split("\n\n")[0]
    assert "<b>" not in complaint
    assert "&lt;b&gt;" in complaint
    assert text.endswith(render.USAGE)
    assert "<code>/flip 50 5 2 3</code>" in text


def test_a_settings_error_about_the_count_still_carries_the_usage() -> None:
    with pytest.raises(SettingsError) as caught:
        parse_settings_tokens(["50"])
    text = render.render_settings_error(caught.value)
    assert "Got 1." in text
    assert "/flip" in text


def test_the_settings_error_text_alone_is_escaped() -> None:
    error = SettingsError("bad <i>input</i> & more")
    assert render.settings_error_text(error) == "bad &lt;i&gt;input&lt;/i&gt; &amp; more"


def test_already_open_names_the_open_flips_settings() -> None:
    text = render.render_already_open(Settings(wager=75, x=6, y=1, z=4))
    assert "already open" in text
    assert "$75 a head · X 6 · Y 1 · Z 4" in text


def test_a_big_wager_gets_thousands_separators() -> None:
    text = render.render_already_open(Settings(wager=1_000_000, x=6, y=1, z=4))
    assert "$1,000,000 a head" in text


def test_fractions_render_exactly() -> None:
    assert render._points(Fraction(3, 2)) == "3/2"
    assert render._points(Fraction(2)) == "2"

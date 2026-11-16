"""Every word the bot says, and the only place that knows how a flip reads.

The engine hands out :mod:`view data <esgbot.engine.views>` and never text, so
this module is where a flip becomes a message. It is pure: give it a view and it
gives you a string, which is why the wording can be read off in a test without a
Telegram client, a clock or a database anywhere near it.

The replies to the commands that read flips back -- ``/leaderboard``, ``/stats``
and ``/verify`` -- are here too, and they are the same kind of function: stats
rows or a :class:`~esgbot.verify.Verification` in, a finished message out. They
are sent straight to the group rather than through :func:`render`, so each one
fits itself.

**HTML, and the two limits.** Messages go out in Telegram's HTML parse mode, so
every name a person chose for themselves is escaped with :func:`escape` and
clipped to :data:`NAME_LIMIT` characters. That clip is what keeps a message
inside Telegram's :data:`MESSAGE_LIMIT`: everything else in a flip -- cards,
settings, point totals -- is bounded by the rules of the game, and a leaderboard
is twenty rows of them. :func:`render` still checks, and shortens on a line
boundary if it ever has to.

Every tag opened on a line is closed on the same line. That is what makes
shortening at a newline safe, and it is worth keeping.

**The pop-up a Peek raises** is the one thing that is not HTML:
:func:`render_peek` writes plain text, because a callback answer is plain text,
and keeps it inside Telegram's :data:`ALERT_LIMIT` of 200 characters. The largest
hand in any flip is 21 cards, which fits with room to spare.

The numbers in the showdown rule on the table come from
:mod:`esgbot.engine.timing` rather than being written out here, so the pacing is
stated in one place and quoted in another.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fractions import Fraction
from html import escape as _escape_html
from typing import Final, assert_never

from esgbot.engine import timing
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
    ResultView,
    SeatView,
    ShowdownStep,
    ShowdownView,
    TableStage,
    TableView,
    VoidReason,
    VoidView,
)
from esgbot.game.cards import Card, format_cards
from esgbot.game.fairness import seed_to_hex
from esgbot.game.rules import (
    MIN_PLAYERS,
    WAGER_MAX,
    WAGER_MIN,
    X_MAX,
    X_MIN,
    Y_MAX,
    Y_MIN,
    Z_MAX,
    Z_MIN,
    FlipRequest,
    Settings,
    SettingsError,
)
from esgbot.game.scoring import Board
from esgbot.game.settlement import Payment
from esgbot.storage import PlayerStats
from esgbot.verify import PART_ORDER, Verification, VerifyPart

__all__ = [
    "AGAIN_USAGE",
    "ALERT_LIMIT",
    "MESSAGE_LIMIT",
    "NAME_LIMIT",
    "NO_FINISHED_FLIPS",
    "NO_FLIPS_OF_YOUR_OWN",
    "NO_FLIP_IN_PRIVATE",
    "NO_FLIP_TO_REPEAT",
    "NO_STATS_IN_PRIVATE",
    "STATS_TROUBLE",
    "STATS_USAGE",
    "STORAGE_TROUBLE",
    "USAGE",
    "VERIFY_USAGE",
    "escape",
    "render",
    "render_already_open",
    "render_commitment",
    "render_leaderboard",
    "render_lobby",
    "render_no_stats_for",
    "render_no_such_flip",
    "render_notice",
    "render_peek",
    "render_result",
    "render_settings_error",
    "render_showdown",
    "render_stats",
    "render_table",
    "render_unknown_player",
    "render_unreadable_flip",
    "render_verification",
    "render_void",
    "settings_error_text",
]

MESSAGE_LIMIT: Final = 4096
"""Characters Telegram allows in one message."""

ALERT_LIMIT: Final = 200
"""Characters Telegram allows in the answer to a button press."""

NAME_LIMIT: Final = 32
"""Characters of a display name that are shown. Long names are clipped, not refused."""

_EXTRA_MARK: Final = "*"
"""What marks a card dealt after the boards, in the group and in the pop-up."""

_AS_MANY_AS_FIT: Final = "as many extra cards as fit"
"""What a ``Y`` the bot filled in asks for, in words.

:data:`~esgbot.game.rules.Y_DEFAULT` is 21, but the deal only sends out a round
of extra cards it can give every seat, so the figure would promise cards the
deck does not have. The settings line says this instead of a number.
"""

_Y_AS_FITS: Final = "Y as many as fit"
"""The same, in the space a settings line has for it."""

_Z_PER_PLAYER: Final = "Z number of players"
"""A ``Z`` left out of the command, while the player list can still change."""

_TO_COME: Final = "— to come —"
_SHORTENED: Final = "\n…"
_TOO_LONG: Final = "This message came out too long to show."

_BOARD_NAMES: Final[dict[Board, str]] = {
    Board.BOARD1: "Board 1",
    Board.BOARD2: "Board 2",
    Board.HAND_STRENGTH: "Hand strength",
}

_RESULT_BOARD_NAMES: Final[dict[Board, str]] = {
    Board.BOARD1: "Board 1",
    Board.BOARD2: "Board 2",
    Board.HAND_STRENGTH: "Hands",
}

_NOTICES: Final[dict[Notice, str]] = {
    Notice.JOINED: "You are in.",
    Notice.LEFT: "You have left the lobby.",
    Notice.STARTED: "Dealing now.",
    Notice.CANCELLED: "Flip cancelled. Nothing was recorded.",
    Notice.ALREADY_JOINED: "You are already in this flip.",
    Notice.NOT_IN_LOBBY: "You are not in this lobby.",
    Notice.LOBBY_FULL: "This lobby is full.",
    Notice.NOT_THE_STARTER: "Only whoever opened the flip can do that.",
    Notice.NEED_MORE_PLAYERS: f"A flip needs at least {MIN_PLAYERS} players.",
    Notice.ALREADY_STARTED: "The round has already started.",
    Notice.NOT_A_PLAYER: "You are not in this flip, so there are no cards to show you.",
    Notice.EXPIRED: "This flip has expired.",
}

_VERIFY_PARTS: Final[dict[VerifyPart, str]] = {
    VerifyPart.COMMITMENT: "the commitment",
    VerifyPart.DEAL: "the deal",
    VerifyPart.POINTS: "the points",
    VerifyPart.NETS: "the net results",
    VerifyPart.SCOOP: "the scoop",
}

_VOID_REASONS: Final[dict[VoidReason, str]] = {
    VoidReason.RESTART: "The bot restarted while it was open.",
    VoidReason.TIMED_OUT: (
        f"It had not finished {timing.FLIP_WATCHDOG_SECONDS / 60:.0f} minutes after it started."
    ),
    VoidReason.ERROR: "Something went wrong while it was running.",
    VoidReason.SEND_FAILED: "The bot could not post to this group.",
}

USAGE: Final = (
    "Usage: <code>/flip &lt;wager&gt; &lt;X&gt; [&lt;Y&gt; [&lt;Z&gt;]]</code> — "
    "for example <code>/flip 50 5 2 3</code>, or <code>/flip 50 5</code>.\n"
    f"Wager {WAGER_MIN:,} to {WAGER_MAX:,} · X {X_MIN} to {X_MAX} · "
    f"Y {Y_MIN} to {Y_MAX} · Z {Z_MIN} to {Z_MAX}, all plain whole numbers.\n"
    f"Leave Y out for {_AS_MANY_AS_FIT}, and Z out for the number of players. "
    "They go in that order, so Z needs a Y in front of it."
)
"""How to write the command, shown with every complaint about its settings."""

NO_FLIP_IN_PRIVATE: Final = (
    "ESG flips are played in a group. Add me to a group chat and run <code>/flip</code> there."
)

AGAIN_USAGE: Final = (
    "Usage: <code>/again</code> on its own — it repeats this group's last flip with the "
    "same settings. To play different ones, give them to <code>/flip</code>."
)
"""Shown when ``/again`` is given settings, which it has no use for."""


STORAGE_TROUBLE: Final = (
    "I could not open the flip: the database is not available right now. Try again in a moment."
)

_PLAY_ONE: Final = "Run <code>/flip 50 5 2 3</code> to play one."
"""How every empty stats answer ends, so there is always something to do next."""

NO_STATS_IN_PRIVATE: Final = (
    "Flips and their stats belong to a group. Add me to a group chat and ask there."
)

STATS_TROUBLE: Final = (
    "I could not read that: the database is not available right now. Try again in a moment."
)

STATS_USAGE: Final = (
    "Usage: <code>/stats</code> for your own figures, <code>/stats @name</code> for somebody "
    "else's, or <code>/stats</code> as a reply to their message."
)
"""How to ask for somebody's stats, shown when the command cannot be read."""

VERIFY_USAGE: Final = (
    "Usage: <code>/verify &lt;flip number&gt;</code> — for example <code>/verify 42</code>. "
    "The number is in the flip's result message."
)
"""How to write the command, shown when the flip number cannot be read."""

NO_FINISHED_FLIPS: Final = f"No flips have finished in this group yet. {_PLAY_ONE}"

NO_FLIPS_OF_YOUR_OWN: Final = f"You have not finished a flip in this group yet. {_PLAY_ONE}"

NO_FLIP_TO_REPEAT: Final = (
    f"No flips have finished in this group yet, so there is nothing to repeat. {_PLAY_ONE}"
)
"""``/again`` in a group with no history. A flip that was cancelled or voided was
never recorded, so it is not a flip this group has played."""


# --------------------------------------------------------------------- plumbing


def escape(text: str) -> str:
    """Make text safe to drop into an HTML-parse-mode message."""
    return _escape_html(text, quote=False)


def _display(name: str) -> str:
    """Any name a person chose for themselves, clipped to a sane length and escaped.

    The clip is what keeps a message with many names in it -- a leaderboard, a
    showdown -- inside :data:`MESSAGE_LIMIT`.
    """
    name = name.strip()
    if len(name) > NAME_LIMIT:
        name = name[: NAME_LIMIT - 1].rstrip() + "…"
    return escape(name)


def _name(player: Player) -> str:
    """One player's name, clipped to a sane length and escaped."""
    return _display(player.display_name)


def _join(items: Sequence[str]) -> str:
    """``"a, b and c"``. The items are already escaped."""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _names(players: Iterable[Player]) -> str:
    """``"Sam, Dana and Alex"``, every name escaped."""
    shown = [_name(player) for player in players]
    if not shown:
        return "nobody"
    return _join(shown)


def _count(value: int, singular: str) -> str:
    """``"1 flip"``, ``"3 flips"``: a figure with its unit, pluralised the easy way."""
    return f"{value} {singular if value == 1 else singular + 's'}"


def _seat_names(seats: Sequence[int], hands: Sequence[HandView]) -> str:
    """The names at these seats, in the order given."""
    return _names(hands[seat].player for seat in seats)


def _cards(cards: Sequence[Card]) -> str:
    """Cards as they are shown: ``A♠ 10♥``. Nothing here needs escaping."""
    return format_cards(cards)


def _hand_cards(hole: Sequence[Card], extra: Sequence[Card]) -> str:
    """A whole hand, with the cards dealt after the boards marked."""
    shown = list(format_cards(hole).split()) if hole else []
    shown += [f"{card}{_EXTRA_MARK}" for card in extra]
    return " ".join(shown)


def _amount(dollars: int) -> str:
    """Whole dollars, with thousands separators: ``$1,000``."""
    return f"${dollars:,}"


def _net(dollars: int) -> str:
    """A net result, with its sign where there is one: ``+$200``, ``-$50``, ``$0``."""
    if dollars > 0:
        return f"+{_amount(dollars)}"
    if dollars < 0:
        return f"-{_amount(-dollars)}"
    return _amount(0)


def _points(points: Fraction) -> str:
    """A point total, exactly: ``2``, ``1/2``, ``3/2``."""
    if points.denominator == 1:
        return str(points.numerator)
    return f"{points.numerator}/{points.denominator}"


def _settings_line(settings: Settings, *, y_defaulted: bool = False) -> str:
    """``$50 a head · X 5 · Y 2 · Z 3``, for a flip whose settings are settled.

    A ``Y`` the bot filled in is said in words rather than as the 21 it is, for
    the reason :data:`_AS_MANY_AS_FIT` gives. ``Z`` is a figure here however it
    was arrived at: Start has fixed the player list, so the number is exact.
    """
    return " · ".join(
        (
            f"{_amount(settings.wager)} a head",
            f"X {settings.x}",
            _Y_AS_FITS if y_defaulted else f"Y {settings.y}",
            f"Z {settings.z}",
        )
    )


def _request_line(request: FlipRequest) -> str:
    """The same line for a flip that has not started: what was asked for.

    Nothing is settled in a lobby, so a value the command left out is named
    rather than guessed at -- above all ``Z``, which is the number of players and
    would change with the next Join.
    """
    return " · ".join(
        (
            f"{_amount(request.wager)} a head",
            f"X {request.x}",
            _Y_AS_FITS if request.y is None else f"Y {request.y}",
            _Z_PER_PLAYER if request.z is None else f"Z {request.z}",
        )
    )


def _clip(text: str, limit: int) -> str:
    """Keep plain text inside a limit, marking it if anything was dropped."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _fit(text: str) -> str:
    """Keep a message inside :data:`MESSAGE_LIMIT`, cutting on a line boundary.

    Every tag this module opens is closed on the same line, so a cut at a newline
    always leaves valid HTML. Nothing a flip renders comes close to the limit --
    names are clipped and everything else is bounded by the rules -- so this is a
    guarantee rather than a routine.
    """
    if len(text) <= MESSAGE_LIMIT:
        return text
    keep = text[: MESSAGE_LIMIT - len(_SHORTENED)]
    cut = keep.rfind("\n")
    if cut <= 0:
        return _TOO_LONG
    return keep[:cut] + _SHORTENED


# ----------------------------------------------------------------------- lobby


def render_lobby(view: LobbyView) -> str:
    """The lobby message: the settings asked for, who is in, and what became of it."""
    cap = view.request.max_players
    lines = [_lobby_heading(view.status), _request_line(view.request)]
    if view.status is LobbyStatus.OPEN:
        lines.append(f"Up to {cap} players.")
    lines.append("")
    lines.append(f"<b>Players {view.player_count}/{cap}</b>")
    if view.players:
        lines += [f"{index}. {_name(player)}" for index, player in enumerate(view.players, start=1)]
    else:
        lines.append("Nobody yet.")
    lines.append("")
    lines.append(_lobby_footer(view))
    return "\n".join(lines)


def _lobby_heading(status: LobbyStatus) -> str:
    match status:
        case LobbyStatus.OPEN:
            return "🎲 <b>New ESG flip</b>"
        case LobbyStatus.CANCELLED:
            return "❌ <b>Flip cancelled</b>"
        case LobbyStatus.TIMED_OUT:
            return "⌛ <b>Lobby timed out</b>"
        case LobbyStatus.VOIDED:
            return "⚠️ <b>Flip voided</b>"
    assert_never(status)


def _lobby_footer(view: LobbyView) -> str:
    starter = _name(view.starter)
    match view.status:
        case LobbyStatus.OPEN:
            if view.full:
                return f"The lobby is full. {starter} opened it and presses Start."
            if view.player_count < MIN_PLAYERS:
                return (
                    f"Opened by {starter}. A flip needs at least {MIN_PLAYERS} players — "
                    f"press Join."
                )
            return f"Opened by {starter}, who presses Start when everyone is in."
        case LobbyStatus.CANCELLED:
            return f"{starter} cancelled it. Nothing was recorded."
        case LobbyStatus.TIMED_OUT:
            return "Nobody joined or left for long enough that it closed itself. Nothing recorded."
        case LobbyStatus.VOIDED:
            return "Nothing was recorded and nobody owes anything."
    assert_never(view.status)


# ------------------------------------------------------------------ commitment


def render_commitment(view: CommitmentView) -> str:
    """The deck commitment: its own message, posted before any card is shown."""
    return (
        "🔒 <b>Deck commitment</b>\n"
        f"<code>{view.commitment}</code>\n"
        "The deck was fixed before a single card was dealt. The seed comes out with the "
        "result, so anyone can check it."
    )


# ----------------------------------------------------------------------- table


def render_table(view: TableView) -> str:
    """The table message, through the deal: boards, card counts and the peek rule."""
    lines = [
        f"🃏 <b>ESG flip</b> — {_settings_line(view.settings, y_defaulted=view.y_defaulted)}",
        f"Pot {_amount(view.settings.pot(view.player_count))} · "
        f"button {_name(view.seats[view.button_seat].player)}",
        f"Commitment <code>{view.commitment}</code>",
        "",
        _board_line(1, view.board1),
        _board_line(2, view.board2),
        "",
        "<b>Players</b>",
    ]
    lines += [_seat_line(seat) for seat in view.seats]
    lines.append("")
    lines += _table_footer(view)
    return "\n".join(lines)


def _board_line(number: int, cards: Sequence[Card]) -> str:
    return f"<b>Board {number}</b>  {_cards(cards) if cards else _TO_COME}"


def _seat_line(seat: SeatView) -> str:
    cards = "card" if seat.card_count == 1 else "cards"
    return f"{seat.seat + 1}. {_name(seat.player)} — {seat.card_count} {cards}"


def _table_footer(view: TableView) -> list[str]:
    if view.voided:
        return ["⚠️ This flip was voided. Nothing was recorded and nobody owes anything."]
    match view.stage:
        case TableStage.HOLE_CARDS:
            return ["Hole cards are out. Press Peek to see yours."]
        case TableStage.BOARD1 | TableStage.BOARD2:
            return ["Press Peek to see your cards."]
        case TableStage.PEEKING:
            return [_extra_line(view), _showdown_rule(), _peeked_line(view)]
    assert_never(view.stage)


def _extra_line(view: TableView) -> str:
    if view.extra_count == 0:
        # Two different reasons for no extra cards, and the group can tell them
        # apart from the settings line anyway: Y of zero asked for none, and
        # anything else means the deck ran out before a full round could go out.
        if view.settings.y == 0:
            return "No extra cards: this flip asked for none."
        return "No extra cards: the deck was too short to give everyone one."
    each = "card" if view.extra_count == 1 else "cards"
    return f"Extra cards dealt: {view.extra_count} {each} each. Peek again to see them."


def _showdown_rule() -> str:
    """The rule the table states, in the engine's own numbers."""
    return (
        f"Showdown in {timing.PEEK_WINDOW_SECONDS:.0f} seconds, or "
        f"{timing.PEEK_ALL_SECONDS:.0f} seconds after everyone has peeked."
    )


def _peeked_line(view: TableView) -> str:
    return f"Peeked: {view.peeked} of {view.player_count}."


# -------------------------------------------------------------------- showdown


def render_showdown(view: ShowdownView) -> str:
    """The table at the showdown, showing everything up to ``view.step``."""
    lines = [
        f"🃏 <b>Showdown</b> — {_settings_line(view.settings, y_defaulted=view.y_defaulted)}",
        f"Pot {_amount(view.settings.pot(view.player_count))} · "
        f"button {_name(view.hands[view.button_seat].player)}",
        f"Commitment <code>{view.commitment}</code>",
        "",
    ]
    lines += _board_lines(view.boards)
    lines.append("")
    lines.append("<b>Hands</b>")
    lines += [_showdown_hand(hand) for hand in view.hands]
    if any(hand.extra for hand in view.hands):
        lines.append(f"{_EXTRA_MARK} dealt after the boards")
    revealed = [board for board in view.boards if view.step.reveals(board.board)]
    if revealed:
        lines.append("")
        lines.append("<b>Boards</b>")
        lines += [_board_result(board, view.hands) for board in revealed]
    if view.step >= ShowdownStep.POINTS:
        lines.append("")
        lines.append("<b>Points</b>")
        lines.append(_points_line(view.hands))
        lines.append(_verdict(view.hands, view.winners, view.scoop_seat, view.all_tied))
    return "\n".join(lines)


def _board_lines(boards: Sequence[BoardView]) -> list[str]:
    """The two community boards, as they were dealt."""
    community = {board.board: board.cards for board in boards}
    return [
        _board_line(1, community.get(Board.BOARD1, ())),
        _board_line(2, community.get(Board.BOARD2, ())),
    ]


def _showdown_hand(hand: HandView) -> str:
    return f"{hand.seat + 1}. {_name(hand.player)}  {_hand_cards(hand.hole, hand.extra)}"


def _board_result(board: BoardView, hands: Sequence[HandView]) -> str:
    """``Board 1: Sam — two pair (A♦ A♠ K♦ K♠ Q♣)``, or who split it."""
    who = _seat_names(board.winners, hands)
    share = f" · {_points(board.share)} each" if board.split else ""
    return (
        f"{_BOARD_NAMES[board.board]}: {who} — {escape(board.description)}{share} "
        f"({_cards(board.best_cards)})"
    )


def _points_line(hands: Sequence[HandView]) -> str:
    return " · ".join(f"{_name(hand.player)} {_points(hand.points)}" for hand in hands)


def _verdict(
    hands: Sequence[HandView],
    winners: Sequence[int],
    scoop_seat: int | None,
    all_tied: bool,
) -> str:
    """Who won, in one bold line."""
    if all_tied:
        return "<b>Everyone tied.</b>"
    if scoop_seat is not None:
        return f"<b>{_name(hands[scoop_seat].player)} scoops all three boards.</b>"
    who = _seat_names(winners, hands)
    verb = "win" if len(winners) > 1 else "wins"
    return f"<b>{who} {verb}.</b>"


# ---------------------------------------------------------------------- result


def render_result(view: ResultView) -> str:
    """The result, posted as a new message so it lands at the bottom of the chat."""
    number = f" #{view.flip_number}" if view.recorded else ""
    lines = [
        f"<b>Flip{number} result</b> — ESG, {_amount(view.settings.wager)} wager",
        " · ".join(_result_board(board, view.hands) for board in view.boards),
        "Points: "
        + ", ".join(f"{_name(hand.player)} {_points(hand.points)}" for hand in view.hands),
        _verdict(view.hands, view.winners, view.scoop_seat, view.all_tied),
    ]
    lines += _payment_lines(view.payments, view.hands)
    lines.append(f"Deck seed: <code>{seed_to_hex(view.seed)}</code>")
    if view.recorded:
        lines.append(f"Check it with <code>/verify {view.flip_number}</code>.")
    else:
        lines.append(
            "⚠️ This flip could not be saved, so it has no number, cannot be verified "
            "and does not count in stats."
        )
    return "\n".join(lines)


def _result_board(board: BoardView, hands: Sequence[HandView]) -> str:
    """``Board 1: Sam (two pair)``, or ``Board 1: Sam and Dana (two pair, 1/2 each)``."""
    who = _seat_names(board.winners, hands)
    share = f", {_points(board.share)} each" if board.split else ""
    return f"{_RESULT_BOARD_NAMES[board.board]}: {who} ({escape(board.description)}{share})"


def _payment_lines(payments: Sequence[Payment], hands: Sequence[HandView]) -> list[str]:
    """One line per payment, or the line for a table that owes nothing."""
    if not payments:
        return ["The pot goes back and nobody owes anything."]
    return [
        f"{_name(hands[payment.payer].player)} owes "
        f"{_name(hands[payment.receiver].player)} {_amount(payment.dollars)}"
        for payment in payments
    ]


# ------------------------------------------------------------------------ void


def render_void(view: VoidView) -> str:
    """The notice that a flip was abandoned, with its seed if it had dealt.

    A flip Start had settled says its four settings. One that never started has
    only what it was asked for, so that is what its notice says -- a ``Z`` left
    out of the command never became a number, and this flip is not going to give
    it one. A notice the startup sweep built knows only the token, the reason and
    the seed, so it says neither.
    """
    lines = ["⚠️ <b>Flip voided</b>"]
    if view.settings is not None:
        y_defaulted = view.request is not None and view.request.y_defaulted
        lines.append(_settings_line(view.settings, y_defaulted=y_defaulted))
    elif view.request is not None:
        lines.append(_request_line(view.request))
    if view.players:
        lines.append(f"Players: {_names(view.players)}")
    lines.append(_VOID_REASONS[view.reason])
    lines.append("Nothing was recorded and nobody owes anything.")
    if view.seed is not None:
        lines.append(f"Deck seed: <code>{seed_to_hex(view.seed)}</code>")
        lines.append("The deal it would have made can still be checked against that seed.")
    else:
        lines.append("No cards had been dealt.")
    return "\n".join(lines)


# --------------------------------------------------------------- press answers


def render_peek(answer: PeekAnswer) -> str:
    """One player's own cards, as the plain text of a pop-up.

    Plain text, not HTML: a callback answer is never parsed. The longest hand a
    flip can deal is 21 cards, which leaves this well inside
    :data:`ALERT_LIMIT`.
    """
    cards = answer.cards
    count = "card" if len(cards) == 1 else "cards"
    lines = [
        f"Seat {answer.seat + 1} · {len(cards)} {count}",
        _hand_cards(answer.hole, answer.extra),
    ]
    if answer.extra:
        lines.append(f"{_EXTRA_MARK} new")
    return _clip("\n".join(lines), ALERT_LIMIT)


def render_notice(notice: Notice) -> str:
    """The short private answer a button press gets."""
    return _clip(_NOTICES[notice], ALERT_LIMIT)


# ------------------------------------------------------------ command replies


def settings_error_text(error: SettingsError) -> str:
    """The complaint itself, escaped, because it echoes what the user typed."""
    return escape(error.message)


def render_settings_error(error: SettingsError) -> str:
    """What is wrong with a ``/flip`` command, and how to write it."""
    return f"⚠️ {settings_error_text(error)}\n\n{USAGE}"


def render_already_open(request: FlipRequest) -> str:
    """The refusal when the group already has a flip open.

    The flip that is open may not have started, so what it is described by is
    what it was asked for -- the one thing that is true of it at either point.
    """
    return (
        f"A flip is already open here ({_request_line(request)}). "
        "Join it, or wait for it to finish, before starting another."
    )


# ----------------------------------------------------------------------- stats


def _figures(stats: PlayerStats) -> str:
    """One player's four figures: ``+$200 · 3 flips · 2 wins (67%) · 1 scoop``."""
    return " · ".join(
        (
            _net(stats.net_dollars),
            _count(stats.flips_played, "flip"),
            f"{_count(stats.wins, 'win')} ({stats.win_rate:.0%})",
            _count(stats.scoops, "scoop"),
        )
    )


def render_leaderboard(rows: Sequence[PlayerStats]) -> str:
    """The group's ranking, best first, or the line for a group with no flips yet.

    The rows come ranked and already cut to the top twenty by
    :meth:`esgbot.storage.Database.leaderboard`; this only numbers them.
    """
    if not rows:
        return NO_FINISHED_FLIPS
    lines = ["🏆 <b>Leaderboard</b> — net winnings in this group"]
    lines += [
        f"{position}. {_display(stats.display_name)} — {_figures(stats)}"
        for position, stats in enumerate(rows, start=1)
    ]
    lines.append("")
    lines.append("A win is a flip whose net came out above $0. Only finished flips count.")
    return _fit("\n".join(lines))


def render_stats(stats: PlayerStats) -> str:
    """One player's figures in this group, under the name they last played under."""
    return _fit(
        f"📊 <b>{_display(stats.display_name)}</b> — stats in this group\n{_figures(stats)}"
    )


def render_no_stats_for(name: str) -> str:
    """Somebody who is in the group but has not finished a flip in it."""
    return _fit(f"{_display(name)} has not finished a flip in this group yet. {_PLAY_ONE}")


def render_unknown_player(username: str) -> str:
    """A ``@name`` no flip in this group was played under."""
    return _fit(
        f"I have no player by that name in this group ({_display(username)}). "
        "The name has to be one they played a flip under here."
    )


# ---------------------------------------------------------------------- verify


def render_no_such_flip(flip_number: int) -> str:
    """A flip number this group does not have, including another group's."""
    return (
        f"There is no flip #{flip_number} in this group. Every group numbers its own flips, "
        "and only a flip that finished and was saved has a number at all."
    )


def render_unreadable_flip(flip_number: int) -> str:
    """A flip whose stored row will not read back, so nothing in it can be checked."""
    return (
        f"❌ <b>Flip #{flip_number} does not verify</b>\n"
        "Its stored record cannot be read back at all, so there is nothing to check it against. "
        "The database has been changed or damaged since the flip was saved."
    )


def render_verification(report: Verification) -> str:
    """The pass or fail, the commitment, and for a fail what did not match."""
    lines = [
        f"{'✅' if report.passed else '❌'} <b>Flip #{report.flip_number} "
        f"{'verifies' if report.passed else 'does not verify'}</b>",
        f"Commitment <code>{report.commitment}</code>",
    ]
    if report.passed:
        lines.append(
            "The deal, the points and every net result follow from the seed that was "
            "committed before a single card was shown."
        )
        lines.append(
            "This is the bot checking its own records. For independent proof, recompute the "
            "deal from the seed in the flip's result message."
        )
    else:
        parts = _join([_VERIFY_PARTS[part] for part in PART_ORDER if report.differs(part)])
        lines.append(f"These do not match what the stored seed produces: {parts}.")
        if report.differs(VerifyPart.COMMITMENT):
            lines.append(f"The stored seed hashes to <code>{report.recomputed_commitment}</code>.")
        lines.append("The stored flip has been changed since it was saved, or is damaged.")
    return _fit("\n".join(lines))


# -------------------------------------------------------------------- dispatch


def render(view: FlipView) -> str:
    """The message for any view the engine posts or edits, inside every limit."""
    match view:
        case LobbyView():
            text = render_lobby(view)
        case CommitmentView():
            text = render_commitment(view)
        case TableView():
            text = render_table(view)
        case ShowdownView():
            text = render_showdown(view)
        case ResultView():
            text = render_result(view)
        case VoidView():
            text = render_void(view)
        case _:
            assert_never(view)
    return _fit(text)

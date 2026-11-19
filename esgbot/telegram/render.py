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

**Nobody has to know what X, Y and Z are.** The documents call the settings by
letter; the bot calls them by name -- ``private cards``, ``extra cards`` and the
``scoop multiplier`` -- in every message, usage line and complaint, and
:data:`~esgbot.game.rules.SETTING_FIELDS` is where those names live. The letters
appear in exactly one place a player can see, the legend in :data:`USAGE`, so
somebody reading the published spec can line the two up.

**Every message is laid out the same way.** Title first, saying what has
happened, with the money on the same line or just under it; then the sections a
flip has, in the order they matter to somebody holding a phone in a busy group:

===========================  ==================================================
:func:`render_lobby`         what it is · who is in · what the settings are
:func:`render_table`         what is out · the boards · the players · how to peek
:func:`render_showdown`      the same, plus the hands, each board, the points
:func:`render_result`        who won · who owes whom · players · boards · proof
:func:`render_void`          that it is void · why · the seed to check it with
===========================  ==================================================

Inside a section it is one fact per line, lists are numbered by seat, and the
provenance -- the commitment, the seed, the ``/verify`` line -- comes last,
because it is what somebody checks afterwards rather than what they read now.

**HTML, and the two limits.** Messages go out in Telegram's HTML parse mode, so
every name a person chose for themselves is escaped with :func:`escape` and
clipped to :data:`NAME_LIMIT` characters. That clip is what keeps a message
inside Telegram's :data:`MESSAGE_LIMIT`: everything else in a flip -- cards,
settings, point totals -- is bounded by the rules of the game, and a leaderboard
is twenty rows of them. :func:`render` still checks, and shortens on a line
boundary if it ever has to.

Every tag opened on a line is closed on the same line. That is what makes
shortening at a newline safe, and it is worth keeping. Only ``<b>`` and
``<code>`` are used: bold for titles and section headings, code for the things
somebody copies -- a commitment, a seed, a command -- and for nothing else, so
the formatting means something wherever it appears. The handful of emoji mark
status and nothing else, and no message leans on colour, so it reads the same in
either theme.

**The pop-up a Peek raises** is the one thing that is not HTML:
:func:`render_peek` writes plain text, because a callback answer is plain text,
and keeps it inside Telegram's :data:`ALERT_LIMIT` of 200 characters. It is also
the one message with no room to spare: a 21-card hand and three made hands do
not fit in 200 characters however they are worded, so the hand is always written
out in full and the made hands are written in the roomiest of three forms that
fits. :func:`render_peek` says what those are.

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
)
from esgbot.game.cards import Card, Rank, format_cards
from esgbot.game.evaluator import Category, HandRank
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
    "USAGE_SHORT",
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
"""What an extra-cards setting the bot filled in asks for, in words.

:data:`~esgbot.game.rules.Y_DEFAULT` is 21, but the deal only sends out a round
of extra cards it can give every seat, so the figure would promise cards the
deck does not have. Every settings line says this instead of a number.
"""

_SCOOP_PER_PLAYER: Final = "scoop pays the wager times the number of players"
"""A scoop multiplier left out of the command, while the player list can still change."""

_EXTRA_NOTE: Final = f"{_EXTRA_MARK} dealt after the boards"
"""What the mark on a card means, said once under the hands that carry it."""

_TO_COME: Final = "— to come —"
_SHORTENED: Final = "\n…"
_TOO_LONG: Final = "This message came out too long to show."

_BOARD_NAMES: Final[dict[Board, str]] = {
    Board.BOARD1: "Board 1",
    Board.BOARD2: "Board 2",
    Board.HAND_STRENGTH: "Hand strength",
}

_BOARD_MARKS: Final[dict[Board, str]] = {
    Board.BOARD1: "B1",
    Board.BOARD2: "B2",
    Board.HAND_STRENGTH: "HS",
}
"""The boards' names cut to two letters, for the shortest rung of a pop-up.

Only the biggest hands ever see these: see :func:`render_peek`. They are in the
same order as the full names, one line under the next, which is what makes them
readable at all.
"""

_SHORT_HANDS: Final[dict[Category, str]] = {
    Category.STRAIGHT_FLUSH: "str flush {0}",
    Category.FOUR_OF_A_KIND: "quads {0}",
    Category.FULL_HOUSE: "full house {0}/{1}",
    Category.FLUSH: "flush {0}",
    Category.STRAIGHT: "straight {0}",
    Category.THREE_OF_A_KIND: "trips {0}",
    Category.TWO_PAIR: "two pair {0}/{1}",
    Category.PAIR: "pair {0}",
    Category.HIGH_CARD: "{0} high",
}
"""What each hand is called where there is no room for words: ``full house Q/7``.

The slots take the ranks that name the hand, in the order
:attr:`~esgbot.game.evaluator.HandRank.tiebreak` gives them, written as they are
on a card. This is the same hand the evaluator named, said shorter -- a pop-up
holding a 21-card hand has around twenty characters a line for three boards --
and it keeps the rank, which is the half of "what have I got" that changes from
one deal to the next.
"""

_ROYAL_SHORT: Final = "royal"
"""The shortest rung's name for the one hand the long form calls a royal flush."""

_NOTICES: Final[dict[Notice, str]] = {
    Notice.JOINED: "You are in. You will get your cards when the round starts.",
    Notice.LEFT: "You have left the lobby.",
    Notice.STARTED: "Dealing now.",
    Notice.CANCELLED: "Flip cancelled. Nothing was recorded.",
    Notice.ALREADY_JOINED: "You are already in this flip.",
    Notice.NOT_IN_LOBBY: "You are not in this lobby.",
    Notice.LOBBY_FULL: "This lobby is full. Nobody else fits at this many private cards.",
    Notice.NOT_THE_STARTER: "Only whoever opened the flip can do that.",
    Notice.NEED_MORE_PLAYERS: f"A flip needs at least {MIN_PLAYERS} players.",
    Notice.ALREADY_STARTED: "The round has already started.",
    Notice.NOT_A_PLAYER: "You are not in this flip, so there are no cards to show you.",
    Notice.AGAIN: "Opening another flip with the same settings.",
    Notice.TROUBLE: "The database is not available right now. Try again in a moment.",
    Notice.EXPIRED: "This flip has expired, so its buttons do nothing now.",
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

_NOTHING_RECORDED: Final = "Nothing was recorded and nobody owes anything."
"""What every ending but a finished flip says, in the same words every time."""

USAGE: Final = (
    "<b>Start a flip</b>\n"
    "<code>/flip &lt;wager&gt; &lt;private cards&gt; [extra cards] [scoop multiplier]</code>\n"
    "For example <code>/flip 50 5 2 3</code> — $50 a head, 5 private cards each, "
    "up to 2 extra cards each, a scoop pays 3x the wager.\n"
    "\n"
    "<b>What each number means</b>\n"
    f"wager — dollars each player puts in · {WAGER_MIN:,} to {WAGER_MAX:,}\n"
    f"private cards (X) — dealt to each player before the boards · {X_MIN} to {X_MAX}\n"
    f"extra cards (Y) — dealt after the boards, as many as fit · {Y_MIN} to {Y_MAX}\n"
    "scoop multiplier (Z) — what taking all three boards pays, as a multiple of the "
    f"wager · {Z_MIN} to {Z_MAX}, where 1x is no bonus\n"
    "\n"
    "The last two are optional: <code>/flip 50 5</code> deals as many extra cards as fit and "
    "makes the scoop multiplier the number of players. They go in that order, so a scoop "
    "multiplier needs an extra-cards number in front of it. All of them are plain whole numbers."
)
"""How to write the command, and what each number is for.

The one place a player sees the letters, so that somebody holding
``docs/fairness-spec.md`` can line its ``X``, ``Y`` and ``Z`` up with the names
the rest of the bot uses. It answers a ``/flip`` that gave the wrong number of
settings -- somebody who does not know the shape of the command -- while a
complaint about one setting gets :data:`USAGE_SHORT`, because the complaint has
already said which setting it is about and what it allows.
"""

USAGE_SHORT: Final = (
    "<code>/flip &lt;wager&gt; &lt;private cards&gt; [extra cards] [scoop multiplier]</code>\n"
    "For example <code>/flip 50 5 2 3</code>, or <code>/flip 50 5</code>.\n"
    "Send <code>/flip</code> on its own for what each number means."
)
"""The command's shape and an example, for a complaint about one setting."""

NO_FLIP_IN_PRIVATE: Final = (
    "ESG flips are played in a group.\nAdd me to a group chat and run <code>/flip</code> there."
)

AGAIN_USAGE: Final = (
    "<b>/again</b> takes no numbers.\n"
    "It repeats this group's last flip, written the same way: a setting that was left out "
    "is left out again.\n"
    "To play different settings, give them to <code>/flip</code>."
)
"""Shown when ``/again`` is given settings, which it has no use for."""

STORAGE_TROUBLE: Final = (
    "I could not open the flip: the database is not available right now.\nTry again in a moment."
)

_PLAY_ONE: Final = "Run <code>/flip 50 5 2 3</code> to play one."
"""How every empty stats answer ends, so there is always something to do next."""

NO_STATS_IN_PRIVATE: Final = (
    "Flips and their stats belong to a group.\nAdd me to a group chat and ask there."
)

STATS_TROUBLE: Final = (
    "I could not read that: the database is not available right now.\nTry again in a moment."
)

STATS_USAGE: Final = (
    "<b>Stats in this group</b>\n"
    "<code>/stats</code> — your own figures\n"
    "<code>/stats @name</code> — somebody else's\n"
    "<code>/stats</code> as a reply to their message — the same, for somebody with no username"
)
"""How to ask for somebody's stats, shown when the command cannot be read."""

VERIFY_USAGE: Final = (
    "<b>Check a finished flip</b>\n"
    "<code>/verify &lt;flip number&gt;</code> — for example <code>/verify 42</code>.\n"
    "The number is in the flip's result message."
)
"""How to write the command, shown when the flip number cannot be read."""

NO_FINISHED_FLIPS: Final = f"No flips have finished in this group yet.\n{_PLAY_ONE}"

NO_FLIPS_OF_YOUR_OWN: Final = f"You have not finished a flip in this group yet.\n{_PLAY_ONE}"

NO_FLIP_TO_REPEAT: Final = (
    f"No flips have finished in this group yet, so there is nothing to repeat.\n{_PLAY_ONE}"
)
"""``/again`` in a group with no history. A flip that was cancelled or voided was
never recorded, so it is not a flip this group has played."""


AUTOJOIN_USAGE: Final = (
    "<b>Join this group's flips automatically</b>\n"
    "<code>/autojoin &lt;wager&gt;</code> — for example <code>/autojoin 50</code> puts you "
    "into every flip here with a wager of $50 or less\n"
    "<code>/autojoin off</code> — stop\n"
    "<code>/autojoin</code> — what you have set"
)
"""How to write ``/autojoin``, shown when what came after it cannot be read."""

AUTOJOIN_OFF: Final = "Auto-join is off. You will only be in flips you join yourself."

AUTOJOIN_NOT_ON: Final = f"You have auto-join off in this group.\n\n{AUTOJOIN_USAGE}"

AUTOPEEK_USAGE: Final = (
    "<b>Count yourself as having peeked automatically</b>\n"
    "<code>/autopeek on</code> — in every flip you play in this group, you are counted as having "
    "peeked as soon as the extra cards are out\n"
    "<code>/autopeek off</code> — stop\n"
    "<code>/autopeek</code> — what you have set"
)
"""How to write ``/autopeek``, shown when what came after it cannot be read."""

AUTOPEEK_ON: Final = (
    "👀 <b>Auto-peek is on</b>\n"
    "In this group you will be counted as having peeked as soon as the extra cards are out, "
    "so the table does not wait for you. Press Peek to see your cards; nothing is shown to you "
    "automatically.\n"
    "<code>/autopeek off</code> stops it."
)

AUTOPEEK_OFF: Final = "Auto-peek is off. You are counted as having peeked when you press Peek."

AUTOJOIN_TROUBLE: Final = (
    "I could not do that: the database is not available right now.\nTry again in a moment."
)


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
    """A whole hand as it was dealt, with the cards dealt after the boards marked."""
    shown = list(format_cards(hole).split()) if hole else []
    shown += [f"{card}{_EXTRA_MARK}" for card in extra]
    return " ".join(shown)


def _sorted_hand(hole: Sequence[Card], extra: Sequence[Card]) -> str:
    """The same hand sorted, highest first, for the player looking at their own cards.

    Rank first, ace high, and then suit -- spades, hearts, diamonds, clubs, which
    is the order the card index puts them in, read backwards. It is a reading
    order and nothing more: suits never decide a hand, and a rank is a rank
    whatever it goes on to make, so the ace of a five-high straight sorts at the
    top of the hand all the same.

    The mark follows the card rather than its place, so a card dealt after the
    boards is still marked wherever it lands.
    """
    marked = [(card, "") for card in hole] + [(card, _EXTRA_MARK) for card in extra]
    marked.sort(key=lambda held: (held[0].rank, held[0].suit), reverse=True)
    return " ".join(f"{card}{mark}" for card, mark in marked)


def _amount(dollars: int) -> str:
    """Whole dollars, with thousands separators: ``$1,000``."""
    return f"${dollars:,}"


def _a_head(wager: int) -> str:
    """The wager, as the group reads it on every message: ``$50 a head``."""
    return f"{_amount(wager)} a head"


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


def _points_with_unit(points: Fraction) -> str:
    """``2 points``, ``1 point``, ``1/2 points``: a total with its unit."""
    return f"{_points(points)} {'point' if points == 1 else 'points'}"


# -------------------------------------------------------- the settings in words


def _private_cards(x: int) -> str:
    """``5 private cards each``: what every player is dealt before the boards."""
    return f"{x} private cards each"


def _extra_cards(y: int | None) -> str:
    """The extra-cards setting in words. ``None`` is one the bot filled in.

    A number is a ceiling rather than a promise -- the deal only sends out a
    round it can give every seat -- so it is always "up to".
    """
    if y is None:
        return _AS_MANY_AS_FIT
    if y == 0:
        return "no extra cards"
    return f"up to {_count(y, 'extra card')} each"


def _scoop(z: int | None) -> str:
    """The scoop multiplier in words. ``None`` is one the bot has yet to fill in.

    A multiplier of one pays what an ordinary win pays, which is worth saying:
    it is the setting doing nothing rather than the setting being missing.
    """
    if z is None:
        return _SCOOP_PER_PLAYER
    if z == 1:
        return "scoop pays the wager, no bonus"
    return f"scoop pays {z}x the wager"


def _settings_strip(settings: Settings, *, y_defaulted: bool = False) -> str:
    """The three game settings of a flip whose settings are settled, in words.

    ``5 private cards each · up to 2 extra cards each · scoop pays 3x the wager``.
    An extra-cards setting the bot filled in is said in words rather than as the
    21 it is, for
    the reason :data:`_AS_MANY_AS_FIT` gives; the scoop multiplier is a figure
    here however it was arrived at, because Start has fixed the player list.

    The wager is not in it: every message that shows this shows the money in its
    own first lines, where somebody looks for it.
    """
    return " · ".join(
        (
            _private_cards(settings.x),
            _extra_cards(None if y_defaulted else settings.y),
            _scoop(settings.z),
        )
    )


def _request_strip(request: FlipRequest) -> str:
    """The same three settings for a flip that has not started: what was asked for.

    Nothing is settled in a lobby, so a value the command left out is named
    rather than guessed at -- above all the scoop multiplier, which is the number
    of players and would change with the next Join.
    """
    return " · ".join((_private_cards(request.x), _extra_cards(request.y), _scoop(request.z)))


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


def _block(*sections: Sequence[str]) -> str:
    """Join sections with a blank line between them, dropping the empty ones.

    Every message is a handful of these, which is what keeps the blank lines
    even: a section that has nothing to say leaves no gap behind it.
    """
    return "\n\n".join("\n".join(lines) for lines in sections if lines)


# ----------------------------------------------------------------------- lobby


def render_lobby(view: LobbyView) -> str:
    """The lobby message: what the flip is, who is in, and what became of it."""
    cap = view.request.max_players
    roster = (
        [f"{index}. {_name(player)}" for index, player in enumerate(view.players, start=1)]
        if view.players
        else ["Nobody yet."]
    )
    return _block(
        [_lobby_title(view), _request_strip(view.request)],
        [f"<b>Players {view.player_count}/{cap}</b>", *roster],
        [_lobby_footer(view)],
    )


def _lobby_title(view: LobbyView) -> str:
    """What this lobby is, and the money, on the line a notification shows."""
    wager = _a_head(view.request.wager)
    match view.status:
        case LobbyStatus.OPEN:
            return f"🎲 <b>New flip</b> — {wager}"
        case LobbyStatus.CANCELLED:
            return f"❌ <b>Flip cancelled</b> — {wager}"
        case LobbyStatus.TIMED_OUT:
            return f"⌛ <b>Lobby timed out</b> — {wager}"
        case LobbyStatus.VOIDED:
            return f"⚠️ <b>Flip voided</b> — {wager}"
    assert_never(view.status)


def _lobby_footer(view: LobbyView) -> str:
    """The line that says what happens next, or what happened instead."""
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
            return f"{starter} cancelled it. {_NOTHING_RECORDED}"
        case LobbyStatus.TIMED_OUT:
            return (
                f"Nobody joined or left for long enough that it closed itself. {_NOTHING_RECORDED}"
            )
        case LobbyStatus.VOIDED:
            return _NOTHING_RECORDED
    assert_never(view.status)


# ------------------------------------------------------------------ commitment


def render_commitment(view: CommitmentView) -> str:
    """The deck commitment: its own message, posted before any card is shown."""
    return _block(
        ["🔒 <b>The deck is sealed</b>", "This is a fingerprint of the shuffled deck."],
        [f"<code>{view.commitment}</code>"],
        [
            "It was taken before a single card was dealt. The seed behind it comes out with "
            "the result, so anyone can check that the deck never changed."
        ],
    )


# ----------------------------------------------------------------------- table


def render_table(view: TableView) -> str:
    """The table message, through the deal: boards, card counts and the peek rule."""
    return _block(
        [
            _table_title(view),
            _table_money(view),
            _settings_strip(view.settings, y_defaulted=view.y_defaulted),
        ],
        [_board_line(1, view.board1), _board_line(2, view.board2)],
        ["<b>Players</b>", *(_seat_line(seat) for seat in view.seats)],
        _table_footer(view),
        [f"Deck commitment <code>{view.commitment}</code>"],
    )


def _table_title(view: TableView) -> str:
    """What has just happened, which is what somebody glancing at it wants."""
    if view.voided:
        return "⚠️ <b>Flip voided</b>"
    match view.stage:
        case TableStage.HOLE_CARDS:
            return "🃏 <b>Private cards are out</b>"
        case TableStage.BOARD1:
            return "🃏 <b>Board 1 is out</b>"
        case TableStage.BOARD2:
            return "🃏 <b>Both boards are out</b>"
        case TableStage.PEEKING:
            return "🃏 <b>Every card is out</b>"
    assert_never(view.stage)


def _table_money(view: TableView) -> str:
    """The pot, the wager and who has the button, in one line."""
    return (
        f"{_a_head(view.settings.wager)} · pot {_amount(view.settings.pot(view.player_count))} · "
        f"button {_name(view.seats[view.button_seat].player)}"
    )


def _board_line(number: int, cards: Sequence[Card]) -> str:
    return f"<b>Board {number}</b>  {_cards(cards) if cards else _TO_COME}"


def _seat_line(seat: SeatView) -> str:
    return f"{seat.seat + 1}. {_name(seat.player)} — {_count(seat.card_count, 'card')}"


def _table_footer(view: TableView) -> list[str]:
    """What to do now, or what became of the flip."""
    if view.voided:
        return [_NOTHING_RECORDED, "The cards above are the deal as far as it got."]
    match view.stage:
        case TableStage.HOLE_CARDS | TableStage.BOARD1 | TableStage.BOARD2:
            return ["Press Peek to see your own cards."]
        case TableStage.PEEKING:
            return [_extra_line(view), _peeked_line(view), _showdown_rule()]
    assert_never(view.stage)


def _extra_line(view: TableView) -> str:
    """Whether the extra cards went out, and why not when they did not."""
    if view.extra_count == 0:
        # Two different reasons for no extra cards: a setting of zero asked for
        # none, and anything else means the deck ran out before a full round
        # could go out. The settings line above says which, but not plainly.
        if view.settings.y == 0:
            return "No extra cards: this flip asked for none."
        return "No extra cards: the deck was too short to give everyone one."
    return (
        f"Extra cards: {view.extra_count} each, so everybody holds "
        f"{view.settings.x + view.extra_count}. Press Peek again to see them."
    )


def _showdown_rule() -> str:
    """The rule the table states, in the engine's own numbers."""
    return (
        f"Showdown in {timing.PEEK_WINDOW_SECONDS:.0f} seconds, or "
        f"{timing.PEEK_ALL_SECONDS:.0f} seconds after everyone has peeked."
    )


def _peeked_line(view: TableView) -> str:
    """How many players have looked at their final hand."""
    verb = "has" if view.peeked == 1 else "have"
    return f"👀 {view.peeked} of {view.player_count} {verb} peeked."


# -------------------------------------------------------------------- showdown


def render_showdown(view: ShowdownView) -> str:
    """The table at the showdown, showing everything up to ``view.step``."""
    hands = ["<b>Hands</b>", *(_showdown_hand(hand) for hand in view.hands)]
    if any(hand.extra for hand in view.hands):
        hands.append(_EXTRA_NOTE)
    revealed = [board for board in view.boards if view.step.reveals(board.board)]
    boards_won = (
        ["<b>Boards won</b>", *(_board_result(board, view.hands) for board in revealed)]
        if revealed
        else []
    )
    points = (
        [
            "<b>Points</b>",
            _points_line(view.hands),
            _verdict(view.hands, view.winners, view.scoop_seat, view.all_tied),
        ]
        if view.step >= ShowdownStep.POINTS
        else []
    )
    return _block(
        [
            "🃏 <b>Showdown</b>",
            _showdown_money(view),
            _settings_strip(view.settings, y_defaulted=view.y_defaulted),
        ],
        _board_lines(view.boards),
        hands,
        boards_won,
        points,
        [f"Deck commitment <code>{view.commitment}</code>"],
    )


def _showdown_money(view: ShowdownView) -> str:
    """The pot, the wager and who had the button, as the table said it."""
    return (
        f"{_a_head(view.settings.wager)} · pot {_amount(view.settings.pot(view.player_count))} · "
        f"button {_name(view.hands[view.button_seat].player)}"
    )


def _board_lines(boards: Sequence[BoardView]) -> list[str]:
    """The two community boards, as they were dealt."""
    community = {board.board: board.cards for board in boards}
    return [
        _board_line(1, community.get(Board.BOARD1, ())),
        _board_line(2, community.get(Board.BOARD2, ())),
    ]


def _showdown_hand(hand: HandView) -> str:
    return f"{hand.seat + 1}. {_name(hand.player)} — {_hand_cards(hand.hole, hand.extra)}"


def _board_result(board: BoardView, hands: Sequence[HandView]) -> str:
    """``Board 1: Sam — two pair (A♦ A♠ K♦ K♠ Q♣)``, or who split it."""
    who = _seat_names(board.winners, hands)
    share = f", {_points(board.share)} each" if board.split else ""
    return (
        f"{_BOARD_NAMES[board.board]}: {who} — {escape(board.description)}{share} "
        f"({_cards(board.best_cards)})"
    )


def _points_line(hands: Sequence[HandView]) -> str:
    """``Sam: 2 · Dana: 1 · Alex: 0``, with the colon keeping a name off its total."""
    return " · ".join(f"{_name(hand.player)}: {_points(hand.points)}" for hand in hands)


def _verdict_phrase(
    hands: Sequence[HandView],
    winners: Sequence[int],
    scoop_seat: int | None,
    all_tied: bool,
) -> str:
    """Who won, in a few words and no markup, for a title or a sentence."""
    if all_tied:
        return "everyone tied"
    if scoop_seat is not None:
        return f"{_name(hands[scoop_seat].player)} scoops all three boards"
    who = _seat_names(winners, hands)
    return f"{who} {'win' if len(winners) > 1 else 'wins'}"


def _verdict(
    hands: Sequence[HandView],
    winners: Sequence[int],
    scoop_seat: int | None,
    all_tied: bool,
) -> str:
    """The same, as the one bold sentence the showdown ends on."""
    phrase = _verdict_phrase(hands, winners, scoop_seat, all_tied)
    return f"<b>{phrase[:1].upper()}{phrase[1:]}.</b>"


# ---------------------------------------------------------------------- result


def render_result(view: ResultView) -> str:
    """The result, posted as a new message so it lands at the bottom of the chat.

    Who won is in the title and who owes whom is the section under it, because
    those are the two things a player opens the message for. The hands are not
    repeated: they are on the table message, which stays as the record.
    """
    scoop = [_scoop_note(view)] if view.scoop_seat is not None and view.settings.z > 1 else []
    return _block(
        [_result_title(view), *scoop],
        ["<b>Settle up</b>", *_payment_lines(view.payments, view.hands)],
        ["<b>Players</b>", *(_result_player(hand) for hand in view.hands)],
        ["<b>Boards</b>", *(_result_board(board, view.hands) for board in view.boards)],
        _result_proof(view),
    )


def _result_title(view: ResultView) -> str:
    """``🏁 <b>Flip #42 — Sam wins</b>``, which is the whole result in one line."""
    what = f"Flip #{view.flip_number}" if view.recorded else "Flip result"
    phrase = _verdict_phrase(view.hands, view.winners, view.scoop_seat, view.all_tied)
    return f"🏁 <b>{what} — {phrase}</b>"


def _scoop_note(view: ResultView) -> str:
    """What the scoop multiplier came to, for the one result it decides."""
    return (
        f"The scoop pays {view.settings.z}x the wager: "
        f"{_amount(view.settings.wager * view.settings.z)} from each other player."
    )


def _result_player(hand: HandView) -> str:
    """``1. Sam — 2 points · +$100``: how one seat did, in points and in money."""
    return (
        f"{hand.seat + 1}. {_name(hand.player)} — "
        f"{_points_with_unit(hand.points)} · {_net(hand.net_dollars)}"
    )


def _result_board(board: BoardView, hands: Sequence[HandView]) -> str:
    """``Board 1: Sam — two pair``, or who split it and for how much each."""
    who = _seat_names(board.winners, hands)
    share = f", {_points(board.share)} each" if board.split else ""
    return f"{_BOARD_NAMES[board.board]}: {who} — {escape(board.description)}{share}"


def _payment_lines(payments: Sequence[Payment], hands: Sequence[HandView]) -> list[str]:
    """One line per payment, or the line for a table that owes nothing."""
    if not payments:
        return ["The pot goes back and nobody owes anything."]
    return [
        f"{_name(hands[payment.payer].player)} owes "
        f"{_name(hands[payment.receiver].player)} {_amount(payment.dollars)}"
        for payment in payments
    ]


def _result_proof(view: ResultView) -> list[str]:
    """The settings, the seed and how to check the flip: the provenance, last."""
    lines = [
        f"{_a_head(view.settings.wager)} · pot {_amount(view.pot)}",
        _settings_strip(view.settings, y_defaulted=view.y_defaulted),
        f"Deck seed <code>{seed_to_hex(view.seed)}</code>",
    ]
    if view.recorded:
        lines.append(f"Check it with <code>/verify {view.flip_number}</code>")
    else:
        lines.append(
            "⚠️ This flip could not be saved, so it has no number, cannot be verified "
            "and does not count in stats."
        )
    return lines


# ------------------------------------------------------------------------ void


def render_void(view: VoidView) -> str:
    """The notice that a flip was abandoned, with its seed if it had dealt.

    A flip Start had settled says its settled settings. One that never started
    has only what it was asked for, so that is what its notice says -- a scoop
    multiplier left out of the command never became a number, and this flip is
    not going to give it one. A notice the startup sweep built knows only the
    token, the reason and the seed, so it says neither.
    """
    what: list[str] = []
    if view.settings is not None:
        y_defaulted = view.request is not None and view.request.y_defaulted
        what.append(
            f"{_a_head(view.settings.wager)} · "
            f"{_settings_strip(view.settings, y_defaulted=y_defaulted)}"
        )
    elif view.request is not None:
        what.append(f"{_a_head(view.request.wager)} · {_request_strip(view.request)}")
    if view.players:
        what.append(f"Players: {_names(view.players)}")
    if view.seed is None:
        seed = ["No cards had been dealt."]
    else:
        seed = [
            f"Deck seed <code>{seed_to_hex(view.seed)}</code>",
            "The deal it would have made can still be checked against that seed.",
        ]
    return _block(
        [f"⚠️ <b>Flip voided</b> — {_NOTHING_RECORDED.lower()}", _VOID_REASONS[view.reason]],
        what,
        seed,
    )


# --------------------------------------------------------------- press answers


_NUTS_MARK: Final = " (NUTS)"
"""What follows the name of a hand nobody else can beat. See :func:`_made_in_full`."""


def _made_in_full(made: MadeHand) -> str:
    """``Board 1: flush, jack high (NUTS) · A♠ K♠ Q♠ J♠ 9♠``: the hand named, and the five cards."""
    nuts = _NUTS_MARK if made.nuts else ""
    return f"{_BOARD_NAMES[made.board]}: {made.hand.description}{nuts} · {_cards(made.hand.cards)}"


def _made_named(made: MadeHand) -> str:
    """``Board 1: flush, jack high (NUTS)``: the hand named, without saying which cards."""
    nuts = _NUTS_MARK if made.nuts else ""
    return f"{_BOARD_NAMES[made.board]}: {made.hand.description}{nuts}"


def _made_short(made: MadeHand) -> str:
    """``B1: flush J (NUTS)``: as little as still names the hand. See :data:`_SHORT_HANDS`."""
    nuts = _NUTS_MARK if made.nuts else ""
    return f"{_BOARD_MARKS[made.board]}: {_short_hand(made.hand.rank)}{nuts}"


def _made_tersest(made: MadeHand) -> str:
    """``B1: flush J NUTS``: the short form with the nuts' brackets taken off.

    The last rung, for the one pop-up the third cannot hold: the widest hand a
    flip can deal with the nuts on both community boards, two characters over.
    """
    nuts = " NUTS" if made.nuts else ""
    return f"{_BOARD_MARKS[made.board]}: {_short_hand(made.hand.rank)}{nuts}"


def _short_hand(rank: HandRank) -> str:
    """One hand in the fewest characters that still name it: ``full house Q/7``."""
    if rank.category is Category.STRAIGHT_FLUSH and rank.tiebreak[0] is Rank.ACE:
        return _ROYAL_SHORT
    return _SHORT_HANDS[rank.category].format(*(held.label for held in rank.tiebreak))


_MADE_FORMS: Final = (_made_in_full, _made_named, _made_short, _made_tersest)
"""How a made hand may be written, roomiest first. See :func:`render_peek`."""


def render_peek(answer: PeekAnswer) -> str:
    """One player's own cards and what they have made, as the plain text of a pop-up.

    Plain text, not HTML: a callback answer is never parsed. The cards come
    highest first, as a player holds them, with the ones just dealt marked; then
    one line per board the answer carries, which is the hand-strength board
    alone before any community board is out, and all three once both are.

    **The cards always fit, so the readings are what give way.** A pop-up has
    :data:`ALERT_LIMIT` characters, and the biggest hand in a flip -- 21 cards,
    sixteen of them marked -- takes most of them, leaving around twenty
    characters a line for three boards. So the readings are written in the
    roomiest of three forms that fits the whole pop-up, all three lines in the
    same one:

    1. the hand named, with the five cards it is made of;
    2. the hand named;
    3. :data:`_BOARD_MARKS` and :data:`_SHORT_HANDS` -- ``B1: full house Q/7``;
    4. the same with ``NUTS`` for ``(NUTS)``, which only the widest hand with
       the nuts on both boards ever needs.

    A hand that is the nuts on a board says so in every rung, after its name.
    The last rung fits every hand a flip can deal, which the tests prove by
    sweeping every legal shape of hand, so a line is never dropped and the
    cards are never cut. :func:`_clip` is the backstop under all
    of it and nothing reaches it.
    """
    cards = answer.cards
    head = [
        f"Your cards · seat {answer.seat + 1} · {_count(len(cards), 'card')}",
        _sorted_hand(answer.hole, answer.extra),
    ]
    if answer.extra:
        head.append(f"{_EXTRA_MARK} just dealt")
    laid_out = ["\n".join([*head, *(form(made) for made in answer.made)]) for form in _MADE_FORMS]
    for text in laid_out:
        if len(text) <= ALERT_LIMIT:
            return text
    # Nothing a flip can deal gets here; the clip is the guarantee all the same.
    return _clip(laid_out[-1], ALERT_LIMIT)


def render_notice(notice: Notice) -> str:
    """The short private answer a button press gets."""
    return _clip(_NOTICES[notice], ALERT_LIMIT)


# ------------------------------------------------------------ command replies


def settings_error_text(error: SettingsError) -> str:
    """The complaint itself, escaped, because it echoes what the user typed."""
    return escape(error.message)


def render_settings_error(error: SettingsError) -> str:
    """What is wrong with a ``/flip`` command, and how to write it.

    A complaint about one setting comes with the command's shape and an example:
    it has already named the setting and the numbers it takes, so the full legend
    would be a lecture. A complaint about the *set* of settings -- too few, too
    many -- comes with the whole of :data:`USAGE`, because somebody who got the
    shape wrong is the one person who needs it.
    """
    return f"⚠️ {settings_error_text(error)}\n\n{USAGE_SHORT if error.field else USAGE}"


def render_already_open(request: FlipRequest, settings: Settings | None = None) -> str:
    """The refusal when the group already has a flip open.

    ``settings`` is that flip's, once Start has settled them, and is what the
    refusal describes: a scoop multiplier the command left out is a figure by
    then, so the flip the group is being sent to can be named by what it is
    paying rather than by the question it has already answered. A lobby that has
    not started has only what it was asked for, where that ``Z`` is still
    whatever the next Join makes it.
    """
    if settings is None:
        what = f"{_a_head(request.wager)} · {_request_strip(request)}"
    else:
        what = (
            f"{_a_head(settings.wager)} · "
            f"{_settings_strip(settings, y_defaulted=request.y_defaulted)}"
        )
    return _block(
        ["⚠️ <b>A flip is already open here</b>", what],
        ["Join that one, or wait for it to finish, before starting another."],
    )


def render_autojoin(max_wager: int) -> str:
    """What a player has asked for with ``/autojoin``, and what it lets them in for.

    The same words answer setting it and asking what is set. The wager is not
    the most a flip can cost, and somebody who is seated without pressing
    anything is the one person who has to be told so.
    """
    return _block(
        [
            "✅ <b>Auto-join is on</b>",
            f"You will be put into every flip in this group with a wager of "
            f"{_amount(max_wager)} or less, as long as there is a seat.",
        ],
        [
            "You can still press Leave before a flip starts. A scoop can cost you more "
            "than the wager.",
            "<code>/autojoin off</code> stops it.",
        ],
    )


def render_autojoin_error(error: SettingsError) -> str:
    """What is wrong with the wager ``/autojoin`` was given, and how to write it."""
    return f"⚠️ {settings_error_text(error)}\n\n{AUTOJOIN_USAGE}"


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


_RANKS: Final = ("🥇", "🥈", "🥉")
"""What the top three places are marked with. Everybody else gets their number."""


def render_leaderboard(rows: Sequence[PlayerStats]) -> str:
    """The group's ranking, best first, or the line for a group with no flips yet.

    The rows come ranked and already cut to the top twenty by
    :meth:`esgbot.storage.Database.leaderboard`; this only numbers them.
    """
    if not rows:
        return NO_FINISHED_FLIPS
    return _fit(
        _block(
            ["🏆 <b>Leaderboard</b> — net winnings in this group"],
            [
                f"{_RANKS[position - 1] if position <= len(_RANKS) else f'{position}.'} "
                f"{_display(stats.display_name)} — {_figures(stats)}"
                for position, stats in enumerate(rows, start=1)
            ],
            ["A win is a flip whose net came out above $0. Only finished flips count."],
        )
    )


def render_stats(stats: PlayerStats) -> str:
    """One player's figures in this group, under the name they last played under."""
    return _fit(
        _block(
            [f"📊 <b>{_display(stats.display_name)}</b> — stats in this group"],
            [
                f"Net winnings: {_net(stats.net_dollars)}",
                f"Flips played: {stats.flips_played}",
                f"Wins: {stats.wins} ({stats.win_rate:.0%})",
                f"Scoops: {stats.scoops}",
            ],
        )
    )


def render_no_stats_for(name: str) -> str:
    """Somebody who is in the group but has not finished a flip in it."""
    return _fit(f"{_display(name)} has not finished a flip in this group yet.\n{_PLAY_ONE}")


def render_unknown_player(username: str) -> str:
    """A ``@name`` no flip in this group was played under."""
    return _fit(
        f"I have no player called {_display(username)} in this group.\n"
        "A name only finds somebody who has played a flip here under it."
    )


# ---------------------------------------------------------------------- verify


def render_no_such_flip(flip_number: int) -> str:
    """A flip number this group does not have, including another group's."""
    return _fit(
        f"There is no flip #{flip_number} in this group.\n"
        "Every group numbers its own flips, and only a flip that finished and was saved has a "
        "number at all."
    )


def render_unreadable_flip(flip_number: int) -> str:
    """A flip whose stored row will not read back, so nothing in it can be checked."""
    return _fit(
        _block(
            [
                f"❌ <b>Flip #{flip_number} does not verify</b>",
                "Its stored record cannot be read back at all, so there is nothing to check it "
                "against.",
            ],
            ["The database has been changed or damaged since the flip was saved."],
        )
    )


def render_verification(report: Verification) -> str:
    """The pass or fail first, then the commitment and what did not match."""
    if report.passed:
        verdict = [
            f"✅ <b>Flip #{report.flip_number} verifies</b>",
            "The deal, the points and every net result follow from the seed that was "
            "committed before a single card was shown.",
        ]
        detail = [
            "This is the bot checking its own records. For independent proof, recompute the "
            "deal from the seed in the flip's result message."
        ]
    else:
        parts = _join([_VERIFY_PARTS[part] for part in PART_ORDER if report.differs(part)])
        verdict = [
            f"❌ <b>Flip #{report.flip_number} does not verify</b>",
            f"These do not match what the stored seed produces: {parts}.",
        ]
        detail = ["The stored flip has been changed since it was saved, or is damaged."]
        if report.differs(VerifyPart.COMMITMENT):
            detail.insert(
                0, f"The stored seed hashes to <code>{report.recomputed_commitment}</code>."
            )
    return _fit(_block(verdict, [f"Commitment <code>{report.commitment}</code>"], detail))


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

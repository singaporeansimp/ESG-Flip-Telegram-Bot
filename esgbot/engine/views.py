"""View data: what the engine says, before anyone decides how to word it.

The engine never writes a message. It hands the gateway one of these records and
the Telegram layer renders it, so the pacing, the state machine and the wording
can be worked on and tested apart from each other.

One message each:

===========================  ============================================================
:class:`LobbyView`           The lobby message, edited as players come and go, and once
                             more when it is started, cancelled or timed out.
:class:`CommitmentView`      The deck commitment, posted at Start as its own message and
                             never edited, because an edited message is no record.
:class:`TableView`           The table: the same message as the lobby was, edited through
                             the deal. Shows card counts, never card faces.
:class:`ShowdownView`        The same message again, revealed in
                             :class:`ShowdownStep` steps once the hands are public.
:class:`ResultView`          The result, posted as a new message so it lands at the
                             bottom of the chat.
:class:`VoidView`            The notice that a flip was abandoned, with its seed if it
                             had started.
===========================  ============================================================

**No view carries a player's cards before the showdown.** :class:`TableView` has
a card count per seat and nothing else, and the engine's own tests walk every
view of a full flip to prove it. The one piece of private data the engine hands
out is a :class:`PeekAnswer`, which goes to one player as the answer to their own
button press and never into the group.

Seats are the players' join order, as everywhere in :mod:`esgbot.game`, and
``seat`` fields index :attr:`TableView.seats`, :attr:`ShowdownView.hands` and
:attr:`ResultView.hands`. :class:`~esgbot.game.settlement.Payment` is reused as
it stands, so ``payer`` and ``receiver`` are seats too.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from fractions import Fraction

from esgbot.game.cards import Card
from esgbot.game.rules import Settings
from esgbot.game.scoring import Board
from esgbot.game.settlement import Payment

__all__ = [
    "BoardView",
    "CommitmentView",
    "FlipView",
    "HandView",
    "LobbyStatus",
    "LobbyView",
    "Notice",
    "PeekAnswer",
    "Player",
    "PressAnswer",
    "ResultView",
    "SeatView",
    "ShowdownStep",
    "ShowdownView",
    "TableStage",
    "TableView",
    "VoidReason",
    "VoidView",
]


@dataclass(frozen=True, slots=True)
class Player:
    """One person, as the flip knows them.

    ``display_name`` is what the group sees and ``username`` is stored with a
    finished flip for ``/stats @name`` to find later, so both are kept as they
    were when the flip was played.

    Raises:
        ValueError: if the display name is blank.
    """

    user_id: int
    display_name: str
    username: str | None = None

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise ValueError(f"player {self.user_id} needs a display name")


class Notice(StrEnum):
    """What a button press did, for the private pop-up that answers it.

    Every press gets exactly one of these, including the ones that worked. The
    first four say the press changed something and the group message that
    follows shows what; the rest say it changed nothing and why. The Telegram
    layer picks the words.
    """

    JOINED = "joined"
    """Added to the player list."""

    LEFT = "left"
    """Taken off the player list. The starter keeps control of the lobby."""

    STARTED = "started"
    """The round is under way."""

    CANCELLED = "cancelled"
    """The lobby is closed and nothing was recorded."""

    ALREADY_JOINED = "already_joined"
    """Join pressed by somebody already on the list."""

    NOT_IN_LOBBY = "not_in_lobby"
    """Leave pressed by somebody who is not on the list."""

    LOBBY_FULL = "lobby_full"
    """Join pressed at the player cap for this flip's X."""

    NOT_THE_STARTER = "not_the_starter"
    """Start or Cancel pressed by anybody but the starter."""

    NEED_MORE_PLAYERS = "need_more_players"
    """Start pressed with fewer than :data:`~esgbot.game.rules.MIN_PLAYERS` players."""

    ALREADY_STARTED = "already_started"
    """A lobby button pressed after the round began. A started flip always finishes."""

    NOT_A_PLAYER = "not_a_player"
    """Peek pressed by somebody who is not in this flip. They see no cards."""

    EXPIRED = "expired"
    """A press on a flip that is over, or on a button whose state has moved on."""


@dataclass(frozen=True, slots=True)
class PeekAnswer:
    """One player's own cards, for the pop-up their Peek press raises.

    This is the only private data the engine hands out, and it goes to the player
    whose cards they are. Before the extra cards are dealt ``extra`` is empty,
    which is how the Telegram layer knows to show the hole cards alone; once they
    are out it holds them, and they are the cards to mark as new.
    """

    seat: int
    hole: tuple[Card, ...]
    extra: tuple[Card, ...] = ()

    @property
    def cards(self) -> tuple[Card, ...]:
        """The whole hand as it stands, hole cards first."""
        return self.hole + self.extra


class LobbyStatus(StrEnum):
    """What a lobby message is showing.

    Anything but :attr:`OPEN` is final and carries no buttons.
    """

    OPEN = "open"
    """Taking players: Join, Leave, Start and Cancel."""

    CANCELLED = "cancelled"
    """The starter cancelled it. Nothing was recorded."""

    TIMED_OUT = "timed_out"
    """It went ten minutes without a join or leave, or reached thirty minutes old."""

    VOIDED = "voided"
    """It was abandoned, by a restart or a failure, rather than closed."""


@dataclass(frozen=True, slots=True)
class LobbyView:
    """The lobby message: the settings, who is in, and what became of it.

    The player cap is ``settings.max_players``, and ``players`` is in join order.
    ``starter`` keeps control of the lobby even after leaving the player list, so
    they are not always one of ``players``.
    """

    token: str
    settings: Settings
    status: LobbyStatus
    starter: Player
    players: tuple[Player, ...]

    @property
    def player_count(self) -> int:
        """How many players are in so far."""
        return len(self.players)

    @property
    def full(self) -> bool:
        """Whether the lobby is at the cap for this flip's X."""
        return len(self.players) >= self.settings.max_players


@dataclass(frozen=True, slots=True)
class CommitmentView:
    """The deck commitment, posted at Start as its own message and never edited.

    ``commitment`` is lowercase hex SHA-256 of the seed. It is posted before any
    card is shown; the seed itself comes out with the result or the void notice,
    and the two together are what ``/verify`` checks.
    """

    token: str
    commitment: str


class TableStage(StrEnum):
    """How far the deal has got, which decides what the table message shows."""

    HOLE_CARDS = "hole_cards"
    """Everyone has their hole cards. Both boards are still empty."""

    BOARD1 = "board1"
    """Board 1 is out."""

    BOARD2 = "board2"
    """Board 2 is out too."""

    PEEKING = "peeking"
    """The extra cards are dealt, if there were any, and the peek window is open."""


@dataclass(frozen=True, slots=True)
class SeatView:
    """One seat at the table: who, and how many cards they are holding.

    A count, never the cards. The faces stay private until the showdown.
    """

    seat: int
    player: Player
    card_count: int


@dataclass(frozen=True, slots=True)
class TableView:
    """The table message, edited through the deal.

    ``board1`` and ``board2`` are empty until the stage that reveals them and
    stay on the table from then on, so the message is a record of the whole deal.
    ``extra_count`` is how many extra cards each player has been dealt, so a
    :attr:`TableStage.PEEKING` view with ``extra_count`` of zero means the deck
    was too short for a full round and none went out.

    ``peeked`` counts the players who have looked at their **final** hand, which
    is what the showdown rule in :mod:`esgbot.engine.timing` is about: a peek
    taken before the extra cards came out does not count towards it. The engine
    updates this count at most once every
    :data:`~esgbot.engine.timing.PEEK_EDIT_INTERVAL_SECONDS` seconds.

    ``voided`` marks a flip that was abandoned: show the table as it stands,
    without the Peek button.
    """

    token: str
    settings: Settings
    commitment: str
    stage: TableStage
    button_seat: int
    seats: tuple[SeatView, ...]
    board1: tuple[Card, ...] = ()
    board2: tuple[Card, ...] = ()
    extra_count: int = 0
    peeked: int = 0
    voided: bool = False

    @property
    def player_count(self) -> int:
        """How many players are in the flip."""
        return len(self.seats)

    @property
    def peek_window_open(self) -> bool:
        """Whether players can still bring the showdown forward by peeking."""
        return self.stage is TableStage.PEEKING and not self.voided


@dataclass(frozen=True, slots=True)
class BoardView:
    """One board's result: who took the point, and with what.

    ``cards`` holds the five community cards, and is empty for
    :attr:`~esgbot.game.scoring.Board.HAND_STRENGTH`, which is played out of a
    player's own hand. ``winners`` are the seats that tied for the point and
    ``share`` is what each of them scored, so a two-way chop shows ``1/2``.
    ``best_cards`` and ``description`` are the winning hand: every winner's hand
    ranks exactly the same, so one set of cards stands for all of them.
    """

    board: Board
    winners: tuple[int, ...]
    best_cards: tuple[Card, ...]
    description: str
    share: Fraction
    cards: tuple[Card, ...] = ()

    @property
    def split(self) -> bool:
        """Whether the point was split rather than won outright."""
        return len(self.winners) > 1


@dataclass(frozen=True, slots=True)
class HandView:
    """One seat at the showdown: the cards, the points and the money.

    ``extra`` is the cards dealt after the boards, worth marking as such.
    ``winner`` means the seat tied for the highest point total, which is not the
    same as making money: a chop can leave a winner at ``0`` and a table that
    ties all round leaves everybody there. Stats count a win as ``net_dollars``
    above zero.
    """

    seat: int
    player: Player
    hole: tuple[Card, ...]
    extra: tuple[Card, ...]
    points: Fraction
    net_dollars: int
    winner: bool
    scooped: bool

    @property
    def cards(self) -> tuple[Card, ...]:
        """The whole hand, hole cards first."""
        return self.hole + self.extra


class ShowdownStep(IntEnum):
    """The showdown, one step at a time, three seconds apart.

    The steps run in this order and each one adds to the table message, so a
    renderer shows everything up to the step it is given.
    """

    HANDS = 0
    """Every player's cards."""

    BOARD1 = 1
    """Board 1's winner."""

    BOARD2 = 2
    """Board 2's winner."""

    HAND_STRENGTH = 3
    """The hand-strength board's winner."""

    POINTS = 4
    """The point totals and who won the flip."""

    def reveals(self, board: Board) -> bool:
        """Whether this step has reached that board's winner."""
        return self >= board + 1


@dataclass(frozen=True, slots=True)
class ShowdownView:
    """The table message at the showdown, revealed step by step.

    Everything is here from the first step; ``step`` says how much of it to show.
    ``boards`` holds one :class:`BoardView` per :class:`~esgbot.game.scoring.Board`
    in that order, and ``hands`` one :class:`HandView` per seat, in seat order.
    """

    token: str
    settings: Settings
    commitment: str
    step: ShowdownStep
    button_seat: int
    boards: tuple[BoardView, ...]
    hands: tuple[HandView, ...]
    winners: tuple[int, ...]
    scoop_seat: int | None = None

    @property
    def player_count(self) -> int:
        """How many players were in the flip."""
        return len(self.hands)

    @property
    def all_tied(self) -> bool:
        """Whether every seat tied on points, in which case every net is zero."""
        return len(self.winners) == len(self.hands)


@dataclass(frozen=True, slots=True)
class ResultView:
    """The result, posted as a new message once the flip is over.

    ``flip_number`` is the public number storage gave the flip, and is ``None``
    when the write failed: the result is still posted, but it says the flip was
    not recorded, cannot be verified and does not count in stats.

    ``payments`` is the settle-up list, in the order the engine paired it off,
    with seats for ``payer`` and ``receiver``. It is empty when nobody owes
    anybody, which is what a table that ties all round gives. ``seed`` is the
    seed the commitment was made over, revealed now.
    """

    token: str
    flip_number: int | None
    settings: Settings
    commitment: str
    seed: bytes
    button_seat: int
    boards: tuple[BoardView, ...]
    hands: tuple[HandView, ...]
    winners: tuple[int, ...]
    payments: tuple[Payment, ...]
    scoop_seat: int | None = None

    @property
    def recorded(self) -> bool:
        """Whether the flip was saved, and so has a number and can be verified."""
        return self.flip_number is not None

    @property
    def player_count(self) -> int:
        """How many players were in the flip."""
        return len(self.hands)

    @property
    def pot(self) -> int:
        """What the players put in: ``wager * players``."""
        return self.settings.pot(len(self.hands))

    @property
    def all_tied(self) -> bool:
        """Whether every seat tied on points, so the pot went back and nobody owes."""
        return len(self.winners) == len(self.hands)


class VoidReason(StrEnum):
    """Why a flip was abandoned rather than finished.

    A voided flip records nothing, so nobody owes anybody anything.
    """

    TIMED_OUT = "timed_out"
    """Still unfinished thirty minutes after Start."""

    SEND_FAILED = "send_failed"
    """The gateway gave up on sending. Nothing more is posted, because nothing can be."""

    ERROR = "error"
    """Something unforeseen broke mid-flip. The error is logged."""

    RESTART = "restart"
    """The bot restarted while the flip was open, and the sweep found its row."""


@dataclass(frozen=True, slots=True)
class VoidView:
    """The notice that a flip was abandoned.

    ``seed`` is set when the flip had started, so the group can still check what
    the deal would have been, and is ``None`` for a lobby that never dealt a
    card. ``settings`` and ``players`` say what the flip was and who was in it.

    They are optional because of the one case where nobody knows: the sweep that
    runs after a restart has only the flip's leftover row -- its token, its
    message and its seed -- and no memory of the flip itself. A notice built from
    a row says the flip was voided and reveals the seed, and leaves out what it
    cannot say.
    """

    token: str
    reason: VoidReason
    settings: Settings | None = None
    players: tuple[Player, ...] = ()
    seed: bytes | None = None
    commitment: str | None = None

    @property
    def started(self) -> bool:
        """Whether the flip had dealt its cards, and so has a seed to reveal."""
        return self.seed is not None


FlipView = CommitmentView | LobbyView | ResultView | ShowdownView | TableView | VoidView
"""Anything the engine asks the gateway to post or edit."""

PressAnswer = Notice | PeekAnswer
"""Anything the engine answers a button press with: a short notice, or your cards."""

"""Points: what the three boards are worth, and who takes them.

A flip scores three boards, each worth one point:

* :attr:`Board.BOARD1` and :attr:`Board.BOARD2` -- the two community boards, played
  with exactly two of the player's cards and three of the board's five.
* :attr:`Board.HAND_STRENGTH` -- the best five of a player's own cards.

Players tied for a board split its point evenly, so points are exact
:class:`~fractions.Fraction` values and never floats: a three-way split of a board
is a third of a point each, and three of those still add up to one. Every flip
hands out :data:`POINTS_IN_PLAY` points in total, however many players there are.

The two halves of a showdown are deliberately separate. :func:`score_deal` is the
expensive one, because it runs the evaluator over every player on all three boards,
and the engine calls it in a worker thread. Everything after that is arithmetic on
the points it produces, and lives in :mod:`esgbot.game.settlement`.

Nothing here knows about money, players' names or seats' identities: a seat is just
its index, the same index :attr:`esgbot.game.deal.Deal.hands` uses.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from fractions import Fraction
from typing import Final

from esgbot.game.deal import Deal
from esgbot.game.evaluator import BestHand, HandRank, best_hand, best_hand_for_board

__all__ = [
    "BOARD_POINT",
    "POINTS_IN_PLAY",
    "SCORED_BOARDS",
    "Board",
    "BoardScore",
    "Scorecard",
    "score_board",
    "score_deal",
]


class Board(IntEnum):
    """The three boards a flip scores, numbered in the order the showdown reveals them."""

    BOARD1 = 0
    BOARD2 = 1
    HAND_STRENGTH = 2

    @property
    def community(self) -> bool:
        """Whether this is one of the two community boards, played two cards plus three."""
        return self is not Board.HAND_STRENGTH


SCORED_BOARDS: Final = len(Board)
"""Boards that award a point: the two community boards plus the hand-strength board."""

BOARD_POINT: Final = Fraction(1)
"""What one board is worth, split evenly among the players tied for it."""

POINTS_IN_PLAY: Final = BOARD_POINT * SCORED_BOARDS
"""Points a flip hands out in total. Every seat's points add up to exactly this."""


@dataclass(frozen=True, slots=True)
class BoardScore:
    """One board's result: every player's best hand on it, and who took the point.

    ``hands`` is indexed by seat, like :attr:`esgbot.game.deal.Deal.hands`, and is
    what the showdown shows. ``winners`` holds the seats whose hands tie for best,
    in ascending seat order, and they split the point evenly.

    Raises:
        ValueError: if there are no hands, or ``winners`` is not exactly the distinct
            seats holding the best hand, in seat order.
    """

    board: Board
    hands: tuple[BestHand, ...]
    winners: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.hands:
            raise ValueError(f"{self.board.name} needs at least one player")
        if not self.winners:
            raise ValueError(f"{self.board.name} has no winner")
        if list(self.winners) != sorted(set(self.winners)):
            raise ValueError(
                f"{self.board.name} winners must be distinct seats in seat order, "
                f"got {self.winners}"
            )
        for seat in self.winners:
            if not 0 <= seat < len(self.hands):
                raise ValueError(
                    f"{self.board.name} names seat {seat}, "
                    f"which is not a seat in a {len(self.hands)}-player flip"
                )
        best = max(hand.rank for hand in self.hands)
        tied = tuple(seat for seat, hand in enumerate(self.hands) if hand.rank == best)
        if self.winners != tied:
            raise ValueError(
                f"{self.board.name} winners {self.winners} are not the seats holding "
                f"the best hand, which are {tied}"
            )

    @property
    def player_count(self) -> int:
        """How many players were scored on this board."""
        return len(self.hands)

    @property
    def rank(self) -> HandRank:
        """What the winning hand is worth. Every winner's hand ranks exactly this."""
        return self.hands[self.winners[0]].rank

    @property
    def split(self) -> bool:
        """Whether more than one player tied for this board's point."""
        return len(self.winners) > 1

    @property
    def share(self) -> Fraction:
        """The points each winner takes: the whole point, or an even split of it."""
        return BOARD_POINT / len(self.winners)

    def hand_for_seat(self, seat: int) -> BestHand:
        """The best hand this seat made on this board.

        Raises:
            IndexError: if there is no such seat.
        """
        if not 0 <= seat < self.player_count:
            raise IndexError(f"no seat {seat} in a {self.player_count}-player flip")
        return self.hands[seat]

    def points_for(self, seat: int) -> Fraction:
        """What this seat scored on this board: a share of the point, or zero.

        Raises:
            IndexError: if there is no such seat.
        """
        if not 0 <= seat < self.player_count:
            raise IndexError(f"no seat {seat} in a {self.player_count}-player flip")
        return self.share if seat in self.winners else Fraction(0)


@dataclass(frozen=True, slots=True)
class Scorecard:
    """All three boards scored: the point totals, the winners and the scoop.

    ``boards`` holds one :class:`BoardScore` per :class:`Board`, in ``Board`` order,
    all for the same seats. Points are exact and always add up to
    :data:`POINTS_IN_PLAY`, so the sole holder of all of them has scooped.

    Raises:
        ValueError: if there is not one board per :class:`Board`, in order, or the
            boards disagree about how many players there are.
    """

    boards: tuple[BoardScore, ...]

    def __post_init__(self) -> None:
        if len(self.boards) != SCORED_BOARDS:
            raise ValueError(f"a scorecard holds {SCORED_BOARDS} boards, got {len(self.boards)}")
        for position, score in enumerate(self.boards):
            expected = Board(position)
            if score.board is not expected:
                raise ValueError(
                    f"boards must be in Board order: position {position} should be "
                    f"{expected.name} but holds {score.board.name}"
                )
            if score.player_count != self.boards[0].player_count:
                raise ValueError(
                    f"{score.board.name} scored {score.player_count} players but "
                    f"{self.boards[0].board.name} scored {self.boards[0].player_count}"
                )

    @property
    def player_count(self) -> int:
        """How many players, and so how many seats, were scored."""
        return self.boards[0].player_count

    def board(self, board: Board) -> BoardScore:
        """This flip's result on one board."""
        return self.boards[board]

    @property
    def points(self) -> tuple[Fraction, ...]:
        """Each seat's point total, indexed by seat. They add up to :data:`POINTS_IN_PLAY`."""
        return tuple(
            sum((score.points_for(seat) for score in self.boards), Fraction(0))
            for seat in range(self.player_count)
        )

    def points_for(self, seat: int) -> Fraction:
        """This seat's point total across all three boards.

        Raises:
            IndexError: if there is no such seat.
        """
        if not 0 <= seat < self.player_count:
            raise IndexError(f"no seat {seat} in a {self.player_count}-player flip")
        return sum((score.points_for(seat) for score in self.boards), Fraction(0))

    @property
    def winners(self) -> tuple[int, ...]:
        """The seats with the highest point total, in ascending seat order."""
        points = self.points
        best = max(points)
        return tuple(seat for seat, value in enumerate(points) if value == best)

    @property
    def winning_points(self) -> Fraction:
        """The highest point total any seat reached."""
        return max(self.points)

    @property
    def scoop_seat(self) -> int | None:
        """The seat that took all three points, or ``None`` if nobody did."""
        winners = self.winners
        if len(winners) == 1 and self.points_for(winners[0]) == POINTS_IN_PLAY:
            return winners[0]
        return None

    @property
    def scooped(self) -> bool:
        """Whether one player took all three points."""
        return self.scoop_seat is not None

    @property
    def all_tied(self) -> bool:
        """Whether every seat finished on the same point total."""
        return len(self.winners) == self.player_count


def score_board(board: Board, hands: Sequence[BestHand]) -> BoardScore:
    """Score one board from hands the evaluator has already found.

    Args:
        board: which board these hands were made on.
        hands: each seat's best hand on that board, indexed by seat.

    Returns:
        The board's result: the hands, and the seats tied for best.

    Raises:
        ValueError: if ``hands`` is empty.
    """
    if not hands:
        raise ValueError(f"{board.name} needs at least one player")
    best = max(hand.rank for hand in hands)
    winners = tuple(seat for seat, hand in enumerate(hands) if hand.rank == best)
    return BoardScore(board=board, hands=tuple(hands), winners=winners)


def score_deal(deal: Deal) -> Scorecard:
    """Evaluate every player on all three boards and score them.

    This is the expensive half of a showdown: it searches every player's best hand
    three times, which is why the engine runs it in a worker thread. The money
    arithmetic needs only the points it produces, so
    :func:`esgbot.game.settlement.settle` can run anywhere.

    Args:
        deal: the completed deal. Every player plays the cards they were dealt,
            whether they are still around or not.

    Returns:
        The three boards, scored.
    """
    hands = [hand.cards for hand in deal.hands]
    scores = [
        score_board(board, [best_hand_for_board(cards, community) for cards in hands])
        for board, community in ((Board.BOARD1, deal.board1), (Board.BOARD2, deal.board2))
    ]
    scores.append(score_board(Board.HAND_STRENGTH, [best_hand(cards) for cards in hands]))
    return Scorecard(boards=tuple(scores))

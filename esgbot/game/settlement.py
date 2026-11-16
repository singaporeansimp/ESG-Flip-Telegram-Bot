"""Money: point totals in, net dollars and a "who owes whom" list out.

Everything here is whole-dollar integer arithmetic on the points
:mod:`esgbot.game.scoring` produced. There are no cards, no floats and no rounding
beyond the one place the rules call for it, so the same inputs always give the same
outputs -- which is what lets ``/verify`` re-run a stored flip and compare.

The rules, in the order they apply:

**Scoop.** If exactly one player holds all three points, that player collects
``wager * Z`` from every other player and each of them is out ``wager * Z``. With
``Z = 1`` that is the same money as an ordinary win, which is why ``Z`` is the whole
multiplier and not a bonus on top.

**Everything else, one formula.** The pot is ``wager * players``. The winners -- the
seats tied for the highest point total -- share it equally, rounded down to whole
dollars, and the leftover dollars go one each to the winners starting from the seat
after the button and moving in seat order, as the odd chip does in hold'em. A
winner's net is their share less their own wager, and every other player is out
their wager. A single winner shares with nobody, so they end up ``wager *
(players - 1)`` ahead; if the whole table ties, everybody takes their own wager back
and every net is zero.

Nets always add up to zero, and :func:`payments_for` turns them into a settle-up
list by repeatedly pairing whoever owes the most with whoever is owed the most,
breaking ties by seat number. :class:`Settlement` checks both of those invariants
when it is built, so a wrong net or a wrong payment cannot be returned quietly.

Seats are indexes, as everywhere in :mod:`esgbot.game`: turning them into players is
the engine's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from esgbot.game.deal import Deal, seat_order
from esgbot.game.rules import Settings
from esgbot.game.scoring import POINTS_IN_PLAY, Board, BoardScore, Scorecard, score_deal

__all__ = [
    "FlipOutcome",
    "Payment",
    "SeatResult",
    "Settlement",
    "payments_for",
    "settle",
    "settle_deal",
    "settle_points",
]


@dataclass(frozen=True, slots=True)
class SeatResult:
    """One seat's line in the result: what it scored, and what that was worth.

    ``net_dollars`` is the whole-dollar swing for the flip, negative for a loss, and
    is the figure a finished flip stores. ``winner`` marks a seat that tied for the
    highest point total; it does not mean the seat made money, because a chop can
    leave a winner at ``0`` and a tie across the whole table leaves everyone there.

    Raises:
        ValueError: if the seat or the points are negative, or a seat scooped
            without winning.
    """

    seat: int
    points: Fraction
    net_dollars: int
    winner: bool
    scooped: bool

    def __post_init__(self) -> None:
        if self.seat < 0:
            raise ValueError(f"seat must be 0 or more, got {self.seat}")
        if self.points < 0:
            raise ValueError(f"points cannot be negative, got {self.points}")
        if self.scooped and not self.winner:
            raise ValueError(f"seat {self.seat} scooped without winning")


@dataclass(frozen=True, slots=True)
class Payment:
    """One line of the settle-up: ``payer`` hands ``dollars`` to ``receiver``.

    Players settle outside Telegram, so this is a statement of who owes whom and
    nothing more.

    Raises:
        ValueError: if a seat is negative, a seat pays itself, or the amount is not
            more than zero.
    """

    payer: int
    receiver: int
    dollars: int

    def __post_init__(self) -> None:
        if self.payer < 0 or self.receiver < 0:
            raise ValueError(f"seats must be 0 or more, got {self.payer} and {self.receiver}")
        if self.payer == self.receiver:
            raise ValueError(f"seat {self.payer} cannot pay itself")
        if self.dollars <= 0:
            raise ValueError(f"a payment must be more than $0, got {self.dollars}")


@dataclass(frozen=True, slots=True)
class Settlement:
    """The money side of a flip: every seat's net, and who pays whom.

    ``seats`` holds one :class:`SeatResult` per seat, in seat order. ``pot`` is what
    the players put in, ``wager * players``; a scoop with ``Z > 1`` moves more than
    that, because it is paid per player rather than out of the pot.

    Raises:
        ValueError: if the seats are not in seat order, the pot is negative, the nets
            do not add up to zero, or the payments do not reproduce the nets exactly.
    """

    pot: int
    seats: tuple[SeatResult, ...]
    payments: tuple[Payment, ...]

    def __post_init__(self) -> None:
        if not self.seats:
            raise ValueError("a settlement needs at least one seat")
        if self.pot < 0:
            raise ValueError(f"the pot cannot be negative, got {self.pot}")
        for seat, result in enumerate(self.seats):
            if result.seat != seat:
                raise ValueError(f"result {seat} has seat {result.seat}; results go in seat order")
        total = sum(result.net_dollars for result in self.seats)
        if total != 0:
            raise ValueError(f"net results must add up to $0, got ${total}")
        moved = [0] * len(self.seats)
        for payment in self.payments:
            for seat in (payment.payer, payment.receiver):
                if seat >= len(self.seats):
                    raise ValueError(
                        f"payment names seat {seat}, which is not a seat in a "
                        f"{len(self.seats)}-player flip"
                    )
            moved[payment.payer] -= payment.dollars
            moved[payment.receiver] += payment.dollars
        for seat, result in enumerate(self.seats):
            if moved[seat] != result.net_dollars:
                raise ValueError(
                    f"the payments move ${moved[seat]} to seat {seat}, "
                    f"but its net is ${result.net_dollars}"
                )

    @property
    def player_count(self) -> int:
        """How many seats this settlement covers."""
        return len(self.seats)

    @property
    def nets(self) -> tuple[int, ...]:
        """Each seat's net dollars, indexed by seat. They add up to zero."""
        return tuple(result.net_dollars for result in self.seats)

    @property
    def winners(self) -> tuple[int, ...]:
        """The seats that tied for the highest point total, in ascending seat order."""
        return tuple(result.seat for result in self.seats if result.winner)

    @property
    def scoop_seat(self) -> int | None:
        """The seat that scooped, or ``None`` if nobody did."""
        for result in self.seats:
            if result.scooped:
                return result.seat
        return None

    @property
    def scooped(self) -> bool:
        """Whether one player took all three points."""
        return self.scoop_seat is not None

    @property
    def all_tied(self) -> bool:
        """Whether every seat tied on points, in which case every net is zero."""
        return len(self.winners) == self.player_count

    def result_for(self, seat: int) -> SeatResult:
        """This seat's line in the result.

        Raises:
            IndexError: if there is no such seat.
        """
        if not 0 <= seat < self.player_count:
            raise IndexError(f"no seat {seat} in a {self.player_count}-player flip")
        return self.seats[seat]

    def net_for(self, seat: int) -> int:
        """This seat's net dollars.

        Raises:
            IndexError: if there is no such seat.
        """
        return self.result_for(seat).net_dollars


def payments_for(nets: Sequence[int]) -> tuple[Payment, ...]:
    """Turn net results into a settle-up list.

    Whoever owes the most is paired with whoever is owed the most, for as much as
    the smaller of the two, until nobody owes anything. Equal amounts go to the
    lower seat first, so the same nets always produce the same list.

    Args:
        nets: each seat's net dollars, indexed by seat. They must add up to zero.

    Returns:
        The payments, in the order they were paired off. Empty when every net is
        zero, which is what a tie across the whole table gives.

    Raises:
        ValueError: if the nets do not add up to zero.
    """
    total = sum(nets)
    if total != 0:
        raise ValueError(f"net results must add up to $0, got ${total}")
    owed = {seat: -net for seat, net in enumerate(nets) if net < 0}
    due = {seat: net for seat, net in enumerate(nets) if net > 0}
    payments: list[Payment] = []
    while owed:
        payer = max(owed, key=lambda seat: (owed[seat], -seat))
        receiver = max(due, key=lambda seat: (due[seat], -seat))
        dollars = min(owed[payer], due[receiver])
        payments.append(Payment(payer=payer, receiver=receiver, dollars=dollars))
        owed[payer] -= dollars
        due[receiver] -= dollars
        if owed[payer] == 0:
            del owed[payer]
        if due[receiver] == 0:
            del due[receiver]
    return tuple(payments)


def settle_points(
    points: Sequence[Fraction | int], settings: Settings, *, button_seat: int
) -> Settlement:
    """Turn point totals into net dollars and a settle-up list.

    This is the whole money rule, and the only part of a showdown that has to be
    right to the dollar. See this module's own documentation for the rules it
    applies.

    Args:
        points: each seat's point total, indexed by seat. Exact fractions; whole
            points may be given as plain integers. They must add up to
            :data:`~esgbot.game.scoring.POINTS_IN_PLAY`.
        settings: the flip's settings, for the wager and the scoop multiplier ``Z``.
        button_seat: the seat with the button. Odd dollars in a chop start at the
            seat after it.

    Returns:
        Every seat's result and the payments that produce it.

    Raises:
        ValueError: if there are no points, a total is negative, the totals do not
            add up to :data:`~esgbot.game.scoring.POINTS_IN_PLAY`, or ``button_seat``
            is not one of the seats.
    """
    player_count = len(points)
    if player_count < 1:
        raise ValueError("a flip needs at least one player")
    totals = tuple(Fraction(value) for value in points)
    for seat, value in enumerate(totals):
        if value < 0:
            raise ValueError(f"seat {seat} has {value} points; points cannot be negative")
    scored = sum(totals, Fraction(0))
    if scored != POINTS_IN_PLAY:
        raise ValueError(f"points must add up to {POINTS_IN_PLAY}, got {scored}")
    order = seat_order(button_seat, player_count)

    best = max(totals)
    winners = tuple(seat for seat, value in enumerate(totals) if value == best)
    scoop_seat = winners[0] if len(winners) == 1 and best == POINTS_IN_PLAY else None

    pot = settings.pot(player_count)
    if scoop_seat is None:
        nets = _chop_nets(pot=pot, wager=settings.wager, winners=winners, order=order)
    else:
        nets = _scoop_nets(
            collected=settings.wager * settings.z, scoop_seat=scoop_seat, player_count=player_count
        )

    results = tuple(
        SeatResult(
            seat=seat,
            points=totals[seat],
            net_dollars=nets[seat],
            winner=seat in winners,
            scooped=seat == scoop_seat,
        )
        for seat in range(player_count)
    )
    return Settlement(pot=pot, seats=results, payments=payments_for(nets))


def _scoop_nets(*, collected: int, scoop_seat: int, player_count: int) -> list[int]:
    """Nets for a scoop: ``wager * Z`` from every other player to the one who scooped."""
    nets = [-collected] * player_count
    nets[scoop_seat] = collected * (player_count - 1)
    return nets


def _chop_nets(*, pot: int, wager: int, winners: Sequence[int], order: Sequence[int]) -> list[int]:
    """Nets for every outcome but a scoop, including a single winner and a full tie.

    The winners share the pot, rounded down, and the leftover dollars go one each to
    the winners in acting order, starting at the seat after the button.
    """
    share, leftover = divmod(pot, len(winners))
    winner_seats = set(winners)
    in_order = [seat for seat in order if seat in winner_seats]
    odd_dollar = set(in_order[:leftover])
    return [
        share + (1 if seat in odd_dollar else 0) - wager if seat in winner_seats else -wager
        for seat in range(len(order))
    ]


def settle(scorecard: Scorecard, settings: Settings, *, button_seat: int) -> Settlement:
    """Settle a flip that has already been scored.

    The scoring half is the expensive one, so the engine can run it in a worker
    thread and call this with the result.

    Args:
        scorecard: the three boards, scored.
        settings: the flip's settings.
        button_seat: the seat with the button, which decides where odd dollars start.

    Returns:
        Every seat's result and the payments that produce it.

    Raises:
        ValueError: if ``button_seat`` is not one of the scorecard's seats.
    """
    return settle_points(scorecard.points, settings, button_seat=button_seat)


@dataclass(frozen=True, slots=True)
class FlipOutcome:
    """A finished flip's result: the three boards, the points and the money.

    This is what the engine shows at showdown and records, and what ``/verify``
    recomputes and compares against the stored flip.

    Raises:
        ValueError: if the two halves disagree about the seats or their points.
    """

    scorecard: Scorecard
    settlement: Settlement

    def __post_init__(self) -> None:
        if self.scorecard.player_count != self.settlement.player_count:
            raise ValueError(
                f"{self.scorecard.player_count} players were scored but "
                f"{self.settlement.player_count} were settled"
            )
        for seat, (scored, result) in enumerate(
            zip(self.scorecard.points, self.settlement.seats, strict=True)
        ):
            if result.points != scored:
                raise ValueError(
                    f"seat {seat} scored {scored} points but was settled for {result.points}"
                )

    @property
    def player_count(self) -> int:
        """How many players were in the flip."""
        return self.scorecard.player_count

    @property
    def boards(self) -> tuple[BoardScore, ...]:
        """The three boards' results, in :class:`~esgbot.game.scoring.Board` order."""
        return self.scorecard.boards

    def board(self, board: Board) -> BoardScore:
        """One board's result."""
        return self.scorecard.board(board)

    @property
    def points(self) -> tuple[Fraction, ...]:
        """Each seat's point total, indexed by seat."""
        return self.scorecard.points

    @property
    def seats(self) -> tuple[SeatResult, ...]:
        """Each seat's result line, in seat order."""
        return self.settlement.seats

    @property
    def payments(self) -> tuple[Payment, ...]:
        """Who owes whom, in the order the payments were paired off."""
        return self.settlement.payments

    @property
    def pot(self) -> int:
        """What the players put in: ``wager * players``."""
        return self.settlement.pot

    @property
    def winners(self) -> tuple[int, ...]:
        """The seats with the highest point total, in ascending seat order."""
        return self.settlement.winners

    @property
    def scoop_seat(self) -> int | None:
        """The seat that took all three points, or ``None`` if nobody did."""
        return self.settlement.scoop_seat

    @property
    def all_tied(self) -> bool:
        """Whether every seat tied on points, in which case every net is zero."""
        return self.settlement.all_tied

    def result_for(self, seat: int) -> SeatResult:
        """One seat's result line.

        Raises:
            IndexError: if there is no such seat.
        """
        return self.settlement.result_for(seat)


def settle_deal(deal: Deal, settings: Settings) -> FlipOutcome:
    """Score a deal and settle it: the one call a showdown makes.

    This runs the evaluator over every player on all three boards, so the engine
    calls it in a worker thread. ``/verify`` calls it again on a stored deal and
    compares what comes back.

    Args:
        deal: the completed deal.
        settings: the flip's settings. Their ``X`` and ``Y`` must match the deal's.

    Returns:
        The boards, the points, every seat's net dollars and the payment list.

    Raises:
        ValueError: if the settings disagree with the deal's ``X`` or ``Y``.
    """
    if settings.x != deal.x or settings.y != deal.y:
        raise ValueError(
            f"settings X = {settings.x}, Y = {settings.y} disagree with the deal's "
            f"X = {deal.x}, Y = {deal.y}"
        )
    scorecard = score_deal(deal)
    settlement = settle(scorecard, settings, button_seat=deal.button_seat)
    return FlipOutcome(scorecard=scorecard, settlement=settlement)

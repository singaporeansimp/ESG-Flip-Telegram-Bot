"""What a hand has made, for the pop-up a Peek raises, worked out off the event loop.

A pop-up tells a player what their cards come to on each board, which is the
question the showdown asks of every hand and is answered here by the same two
searches in :mod:`esgbot.game.evaluator`: the Omaha rule on a community board --
exactly two of the player's cards and three of the board's five -- and the best
five of the player's own cards on the hand-strength board. Nothing here ranks a
hand itself, so a pop-up and the showdown can never disagree about one.

**Only the boards the group has already seen are read.** Every card is fixed at
Start, so a hand read against a board still to come would say something about
cards nobody has been shown. :meth:`esgbot.engine.flip.Flip.peek` decides which
boards those are and passes them in; this module reads what it is given, and the
hand-strength board needs no board at all.

**The nuts** are the best hand anybody else could hold on a board, given what this
player can see: the boards the table has shown and their own cards. Every other
card is unseen to them, so any two of those could be in another player's hand,
and a hand is the nuts when none of those pairs makes a better one. It is worked
out from what the player can see and nothing else, so it tells them nothing
about the cards still to come. Only the community boards get it: another
player's hand-strength board is out of as many cards as they hold, which is
more than anyone can enumerate.

**It runs in a worker thread.** The hand-strength search over a 21-card hand
checks 20,349 five-card hands, which is tens of milliseconds, and each community
board another 2,100; a Peek is answered while a flip's timers and every other
group carry on, so the work goes off the event loop exactly as the showdown's
does. :data:`HandReader` is the seam and :func:`read_hand_in_thread` is the real
one, which is what lets the tests read inline and keep a flip on a fake clock
deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from itertools import chain, combinations
from typing import Final

from esgbot.engine.views import MadeHand
from esgbot.game.cards import FULL_DECK, Card
from esgbot.game.evaluator import BestHand, best_hand, best_hand_for_board
from esgbot.game.scoring import Board

__all__ = ["HandReader", "read_hand", "read_hand_in_thread"]

HandReader = Callable[[Sequence[Card], Sequence[Sequence[Card]]], Awaitable[tuple[MadeHand, ...]]]
"""How a flip reads a hand for a pop-up. :func:`read_hand_in_thread` is the real one."""

_COMMUNITY_BOARDS: Final = (Board.BOARD1, Board.BOARD2)
"""The community boards, in the order a flip deals them and shows them."""


def read_hand(cards: Sequence[Card], boards: Sequence[Sequence[Card]]) -> tuple[MadeHand, ...]:
    """What these cards have made on each board given, and then on their own.

    Args:
        cards: the player's cards as they hold them at this moment -- their
            private cards, and the extra cards too once those are out -- five or
            more of them, all distinct.
        boards: the community boards the table has already shown, Board 1 first:
            none of them, Board 1 alone, or both.

    Returns:
        One :class:`~esgbot.engine.views.MadeHand` per board given, in
        :class:`~esgbot.game.scoring.Board` order, with the hand-strength board
        last and always there.

    Raises:
        ValueError: if more boards are given than a flip has, or the cards are
            not a hand the evaluator can read: too few, or a card twice.
    """
    if len(boards) > len(_COMMUNITY_BOARDS):
        raise ValueError(f"a flip has {len(_COMMUNITY_BOARDS)} community boards, got {len(boards)}")
    unseen = [card for card in FULL_DECK if card not in {*cards, *chain.from_iterable(boards)}]
    made = []
    # The boards a table has shown are the first of its boards, in this order.
    for board, shown in zip(_COMMUNITY_BOARDS, boards, strict=False):
        hand = best_hand_for_board(cards, shown)
        made.append(MadeHand(board=board, hand=hand, nuts=_is_nuts(hand, shown, unseen)))
    made.append(MadeHand(board=Board.HAND_STRENGTH, hand=best_hand(cards)))
    return tuple(made)


def _is_nuts(hand: BestHand, board: Sequence[Card], unseen: Sequence[Card]) -> bool:
    """Whether no two of the unseen cards make a better hand on this board. A tie is the nuts."""
    return all(
        best_hand_for_board(pair, board).rank <= hand.rank for pair in combinations(unseen, 2)
    )


async def read_hand_in_thread(
    cards: Sequence[Card], boards: Sequence[Sequence[Card]]
) -> tuple[MadeHand, ...]:
    """Read a hand in a worker thread, so a Peek costs the event loop nothing.

    It still shares the interpreter lock, so this buys responsiveness rather
    than parallelism -- the same trade
    :func:`~esgbot.engine.flip.settle_in_thread` makes for the showdown.
    """
    return await asyncio.to_thread(read_hand, cards, boards)

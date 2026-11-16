"""Hand-built boards and scorecards, for tests that care about points, not cards.

A seat's hand is named by its *tier*, an index into :data:`LADDER`: a higher tier
always beats a lower one, and two seats on the same tier tie exactly. So a board is
a list of tiers, one per seat, and a scorecard is three of those lists.
"""

from __future__ import annotations

from esgbot.game.cards import parse_cards
from esgbot.game.evaluator import BestHand, rank_five
from esgbot.game.scoring import Board, BoardScore, Scorecard, score_board

LADDER = (
    "Q♠ J♥ 9♣ 5♦ 2♥",  # high card, queen
    "A♠ K♥ 9♣ 5♦ 2♥",  # high card, ace
    "7♠ 7♥ 9♣ 5♦ 2♥",  # pair
    "8♠ 8♥ 8♣ 5♦ 2♥",  # three of a kind
)
"""Five-card hands, weakest first. Each one is a distinct :class:`HandRank`."""

ACE_HIGH_OTHER_SUITS = "A♥ K♠ 9♦ 5♣ 2♠"
"""The same rank as ``LADDER[1]`` out of different cards, so a tie is not an identity."""


def make_hand(text: str) -> BestHand:
    """A best hand made of exactly the five cards written in ``text``."""
    cards = parse_cards(text)
    return BestHand(rank=rank_five(cards), cards=cards)


def board_of(board: Board, tiers: list[int]) -> BoardScore:
    """One board where seat ``i`` holds ``LADDER[tiers[i]]``."""
    return score_board(board, [make_hand(LADDER[tier]) for tier in tiers])


def scorecard_of(boards: tuple[list[int], list[int], list[int]]) -> Scorecard:
    """A full scorecard from one tier list per board, in :class:`Board` order."""
    return Scorecard(
        boards=tuple(board_of(Board(index), tiers) for index, tiers in enumerate(boards))
    )

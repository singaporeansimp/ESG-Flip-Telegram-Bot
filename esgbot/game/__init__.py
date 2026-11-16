"""Pure game logic: no Telegram, no database, no clock.

The public surface of this package, which the engine, the storage layer and the
other game modules build on:

* :mod:`esgbot.game.cards` -- :class:`~esgbot.game.cards.Card`,
  :class:`~esgbot.game.cards.Rank`, :class:`~esgbot.game.cards.Suit` and the
  stable 0..51 card index.
* :mod:`esgbot.game.fairness` -- seed, commitment, deck order and button.
* :mod:`esgbot.game.deal` -- the deal procedure and the :class:`~esgbot.game.deal.Deal`
  record it produces.
* :mod:`esgbot.game.rules` -- the four settings, their limits and the player cap.
* :mod:`esgbot.game.evaluator` -- what five cards are worth, and the best five a
  player can make on each of the three boards.
* :mod:`esgbot.game.scoring` -- the three boards' winners and the points they split.
* :mod:`esgbot.game.settlement` -- net dollars, the scoop and odd-chip rules, and
  who owes whom. :func:`~esgbot.game.settlement.settle_deal` is the whole showdown
  in one call.

The names below are re-exported for convenience; importing from the module
itself is equally fine and is what the modules do among themselves.
"""

from esgbot.game.cards import DECK_SIZE, FULL_DECK, Card, Rank, Suit
from esgbot.game.deal import Deal, PlayerHand, deal_cards, seat_order
from esgbot.game.evaluator import (
    BestHand,
    Category,
    HandRank,
    best_hand,
    best_hand_for_board,
    rank_five,
)
from esgbot.game.fairness import button_seat, commitment, deck_order, generate_seed
from esgbot.game.rules import Settings, SettingsError, max_players, parse_settings
from esgbot.game.scoring import (
    POINTS_IN_PLAY,
    Board,
    BoardScore,
    Scorecard,
    score_board,
    score_deal,
)
from esgbot.game.settlement import (
    FlipOutcome,
    Payment,
    SeatResult,
    Settlement,
    payments_for,
    settle,
    settle_deal,
    settle_points,
)

__all__ = [
    "DECK_SIZE",
    "FULL_DECK",
    "POINTS_IN_PLAY",
    "BestHand",
    "Board",
    "BoardScore",
    "Card",
    "Category",
    "Deal",
    "FlipOutcome",
    "HandRank",
    "Payment",
    "PlayerHand",
    "Rank",
    "Scorecard",
    "SeatResult",
    "Settings",
    "SettingsError",
    "Settlement",
    "Suit",
    "best_hand",
    "best_hand_for_board",
    "button_seat",
    "commitment",
    "deal_cards",
    "deck_order",
    "generate_seed",
    "max_players",
    "parse_settings",
    "payments_for",
    "rank_five",
    "score_board",
    "score_deal",
    "seat_order",
    "settle",
    "settle_deal",
    "settle_points",
]

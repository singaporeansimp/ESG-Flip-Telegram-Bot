"""The deal procedure: seed plus player list in, everybody's cards out.

The whole deal is a pure function of the seed and the locked player list, so the
group can recompute it from the revealed seed. The procedure, with byte-level
encodings and test vectors, is published in ``docs/fairness-spec.md``.

Seats are the join order, numbered from 0. Every round of dealing starts with the
seat after the button and moves up through the seats, wrapping around. Cards come
off the seed-derived deck (:func:`esgbot.game.fairness.deck_order`) in one stream:

1. ``X`` rounds of one card per seat (the hole cards),
2. Board 1 (five cards), then Board 2 (five cards),
3. extra rounds of one card per seat while every seat can still get one, up to ``Y``
   rounds. With 52 cards that is ``min(Y, (52 - X * players - 10) // players)`` rounds,
   so either every player gets the same number of extra cards or nobody gets any.

:class:`Deal` is plain data: :meth:`Deal.to_json` and :meth:`Deal.from_json`
round-trip it for storage, with cards written as their 0..51 indexes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Self

from esgbot.game import fairness, rules
from esgbot.game.cards import DECK_SIZE, Card, card_indexes, cards_from_indexes

__all__ = [
    "DEAL_JSON_VERSION",
    "Deal",
    "PlayerHand",
    "deal_cards",
    "extra_rounds_for",
    "seat_order",
]

DEAL_JSON_VERSION: Final = 1
"""Version tag stored with a deal, so a later schema change can be recognised."""


def seat_order(button_seat: int, player_count: int) -> tuple[int, ...]:
    """The seats in acting order: the seat after the button first, wrapping around.

    This is the order cards are dealt in and the order odd chips are paid out in.

    Raises:
        ValueError: if ``player_count`` is below 1 or ``button_seat`` is not a seat.
    """
    if player_count < 1:
        raise ValueError(f"player count must be at least 1, got {player_count}")
    if not 0 <= button_seat < player_count:
        raise ValueError(f"button seat must be 0..{player_count - 1}, got {button_seat}")
    return tuple((button_seat + 1 + step) % player_count for step in range(player_count))


def extra_rounds_for(*, player_count: int, x: int, y: int) -> int:
    """How many extra cards each player gets, given the deck that is left.

    Rounds go out only while every seat can still get one, up to ``y`` rounds.
    """
    if player_count < 1:
        raise ValueError(f"player count must be at least 1, got {player_count}")
    remaining = DECK_SIZE - player_count * x - rules.BOARD_CARDS
    if remaining < 0:
        raise ValueError(
            f"{player_count} players at X = {x} plus {rules.BOARD_CARDS} board cards "
            f"need more than the {DECK_SIZE} cards in the deck"
        )
    return min(y, remaining // player_count)


@dataclass(frozen=True, slots=True)
class PlayerHand:
    """One player's cards: the hole cards, then the extra cards dealt after the boards."""

    seat: int
    player_id: int
    hole: tuple[Card, ...]
    extra: tuple[Card, ...]

    @property
    def cards(self) -> tuple[Card, ...]:
        """The player's full hand, hole cards first, in the order they were dealt."""
        return self.hole + self.extra


@dataclass(frozen=True, slots=True)
class Deal:
    """Everything the deal produced. Immutable, JSON round-trippable plain data.

    ``hands`` is indexed by seat, which is the players' join order.
    """

    x: int
    y: int
    button_seat: int
    hands: tuple[PlayerHand, ...]
    board1: tuple[Card, ...]
    board2: tuple[Card, ...]

    def __post_init__(self) -> None:
        if not self.hands:
            raise ValueError("a deal needs at least one player")
        if not 0 <= self.button_seat < len(self.hands):
            raise ValueError(
                f"button seat must be 0..{len(self.hands) - 1}, got {self.button_seat}"
            )
        for board_name, board in (("board1", self.board1), ("board2", self.board2)):
            if len(board) != rules.BOARD_SIZE:
                raise ValueError(
                    f"{board_name} must hold {rules.BOARD_SIZE} cards, got {len(board)}"
                )
        extra_count = len(self.hands[0].extra)
        for seat, hand in enumerate(self.hands):
            if hand.seat != seat:
                raise ValueError(f"hand {seat} has seat {hand.seat}; hands must be in seat order")
            if len(hand.hole) != self.x:
                raise ValueError(
                    f"seat {seat} has {len(hand.hole)} hole cards, expected X = {self.x}"
                )
            if len(hand.extra) != extra_count:
                raise ValueError(
                    f"seat {seat} has {len(hand.extra)} extra cards but seat 0 has {extra_count}; "
                    "extra cards go out in full rounds"
                )
        if extra_count > self.y:
            raise ValueError(f"{extra_count} extra cards per player exceeds Y = {self.y}")
        all_cards = self.all_cards()
        if len(set(all_cards)) != len(all_cards):
            raise ValueError("the same card was dealt twice")

    @property
    def player_count(self) -> int:
        """How many players, and so how many seats, are in this deal."""
        return len(self.hands)

    @property
    def player_ids(self) -> tuple[int, ...]:
        """The player ids by seat, which is their join order."""
        return tuple(hand.player_id for hand in self.hands)

    @property
    def boards(self) -> tuple[tuple[Card, ...], tuple[Card, ...]]:
        """Both community boards, Board 1 first."""
        return (self.board1, self.board2)

    @property
    def extra_count(self) -> int:
        """Extra cards each player received, the same for every seat."""
        return len(self.hands[0].extra)

    @property
    def seat_order(self) -> tuple[int, ...]:
        """Seats in acting order, starting with the seat after the button."""
        return seat_order(self.button_seat, self.player_count)

    def hand_for_seat(self, seat: int) -> PlayerHand:
        """The hand at this seat.

        Raises:
            IndexError: if there is no such seat.
        """
        if not 0 <= seat < self.player_count:
            raise IndexError(f"no seat {seat} in a {self.player_count}-player deal")
        return self.hands[seat]

    def seat_of(self, player_id: int) -> int:
        """The seat this player sits in.

        Raises:
            KeyError: if the player is not in this deal.
        """
        for hand in self.hands:
            if hand.player_id == player_id:
                return hand.seat
        raise KeyError(f"player {player_id} is not in this deal")

    def all_cards(self) -> tuple[Card, ...]:
        """Every card this deal used: both boards and every player's cards."""
        cards: list[Card] = [*self.board1, *self.board2]
        for hand in self.hands:
            cards.extend(hand.cards)
        return tuple(cards)

    def to_json_dict(self) -> dict[str, Any]:
        """The deal as JSON-ready plain data, with cards written as 0..51 indexes."""
        return {
            "version": DEAL_JSON_VERSION,
            "x": self.x,
            "y": self.y,
            "button_seat": self.button_seat,
            "board1": card_indexes(self.board1),
            "board2": card_indexes(self.board2),
            "hands": [
                {
                    "seat": hand.seat,
                    "player_id": hand.player_id,
                    "hole": card_indexes(hand.hole),
                    "extra": card_indexes(hand.extra),
                }
                for hand in self.hands
            ],
        }

    def to_json(self) -> str:
        """The deal as a compact JSON string, the form stored with a finished flip."""
        return json.dumps(self.to_json_dict(), separators=(",", ":"))

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild a deal from :meth:`to_json_dict` output.

        Raises:
            ValueError: if a field is missing, of the wrong type, or inconsistent.
        """
        version = _require_int(data, "version")
        if version != DEAL_JSON_VERSION:
            raise ValueError(f"unsupported deal JSON version {version}")
        hands: list[PlayerHand] = []
        for position, entry in enumerate(_require_list(data, "hands")):
            hand = _require_mapping(entry, f"hands[{position}]")
            hands.append(
                PlayerHand(
                    seat=_require_int(hand, "seat"),
                    player_id=_require_int(hand, "player_id"),
                    hole=_require_cards(hand, "hole"),
                    extra=_require_cards(hand, "extra"),
                )
            )
        return cls(
            x=_require_int(data, "x"),
            y=_require_int(data, "y"),
            button_seat=_require_int(data, "button_seat"),
            hands=tuple(hands),
            board1=_require_cards(data, "board1"),
            board2=_require_cards(data, "board2"),
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Rebuild a deal from a JSON string.

        Raises:
            ValueError: if the text is not JSON, or is not a valid deal.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"deal is not valid JSON: {error}") from error
        return cls.from_json_dict(_require_mapping(data, "deal"))


def deal_cards(*, seed: bytes, player_ids: Sequence[int], x: int, y: int) -> Deal:
    """Deal a flip.

    Args:
        seed: the 32-byte seed the commitment was made over.
        player_ids: the players in join order; seat ``i`` is ``player_ids[i]``.
        x: hole cards per player.
        y: maximum extra cards per player.

    Returns:
        The complete deal. The same arguments always give the same result.

    Raises:
        ValueError: if the seed is not 32 bytes, the player ids are empty or repeat,
            ``x`` or ``y`` is outside its limits, or that many players do not fit
            in one deck at this ``x``.
    """
    fairness.validate_seed(seed)
    player_count = len(player_ids)
    if player_count < 1:
        raise ValueError("a deal needs at least one player")
    if len(set(player_ids)) != player_count:
        raise ValueError("player ids must be unique")
    if not rules.X_MIN <= x <= rules.X_MAX:
        raise ValueError(f"X must be {rules.X_MIN}..{rules.X_MAX}, got {x}")
    if not rules.Y_MIN <= y <= rules.Y_MAX:
        raise ValueError(f"Y must be {rules.Y_MIN}..{rules.Y_MAX}, got {y}")
    cap = rules.max_players(x)
    if player_count > cap:
        raise ValueError(
            f"{player_count} players do not fit at X = {x}: at most {cap} do "
            f"({DECK_SIZE} cards less {rules.BOARD_CARDS} board cards)"
        )

    button = fairness.button_seat(seed, player_count)
    order = seat_order(button, player_count)
    stream = iter(fairness.deck_order(seed))

    hole: list[list[Card]] = [[] for _ in range(player_count)]
    for _ in range(x):
        for seat in order:
            hole[seat].append(next(stream))

    board1 = tuple(next(stream) for _ in range(rules.BOARD_SIZE))
    board2 = tuple(next(stream) for _ in range(rules.BOARD_SIZE))

    extra: list[list[Card]] = [[] for _ in range(player_count)]
    for _ in range(extra_rounds_for(player_count=player_count, x=x, y=y)):
        for seat in order:
            extra[seat].append(next(stream))

    hands = tuple(
        PlayerHand(
            seat=seat,
            player_id=player_ids[seat],
            hole=tuple(hole[seat]),
            extra=tuple(extra[seat]),
        )
        for seat in range(player_count)
    )
    return Deal(x=x, y=y, button_seat=button, hands=hands, board1=board1, board2=board2)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object, got {type(value).__name__}")
    return value


def _require_list(data: Mapping[str, Any], key: str) -> list[Any]:
    if key not in data:
        raise ValueError(f"deal is missing {key!r}")
    value = data[key]
    if not isinstance(value, list):
        raise ValueError(f"deal {key!r} must be a list, got {type(value).__name__}")
    return value


def _require_int(data: Mapping[str, Any], key: str) -> int:
    if key not in data:
        raise ValueError(f"deal is missing {key!r}")
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"deal {key!r} must be an integer, got {value!r}")
    return value


def _require_cards(data: Mapping[str, Any], key: str) -> tuple[Card, ...]:
    indexes = _require_list(data, key)
    for index in indexes:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"deal {key!r} must hold card indexes, got {index!r}")
    try:
        return cards_from_indexes(indexes)
    except ValueError as error:
        raise ValueError(f"deal {key!r}: {error}") from error

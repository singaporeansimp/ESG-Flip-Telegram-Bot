"""Reads the published test vectors out of ``docs/fairness-spec.md``.

The specification is the single source of truth for the vectors, so the document
and the code cannot drift apart: tests load the vectors from the document itself
rather than keeping a second copy.

Usage::

    from tests.fairness_vectors import load_vectors

    for vector in load_vectors():
        assert commitment(vector.seed) == vector.commitment

See the "Test vectors" section of the specification for the block format.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from esgbot.game.cards import Card, parse_cards

__all__ = ["SPEC_PATH", "FairnessVector", "VectorHand", "load_vectors"]

SPEC_PATH: Final = Path(__file__).resolve().parent.parent / "docs" / "fairness-spec.md"
"""The published specification the vectors are read from."""

_BLOCK = re.compile(r"^```text\n(.*?)^```", re.DOTALL | re.MULTILINE)


@dataclass(frozen=True, slots=True)
class VectorHand:
    """One seat's cards in a vector's deal."""

    seat: int
    hole: tuple[Card, ...]
    extra: tuple[Card, ...]


@dataclass(frozen=True, slots=True)
class FairnessVector:
    """One published test vector."""

    number: int
    seed: bytes
    commitment: str
    deck_first_ten: tuple[Card, ...]
    button_2: int
    button_5: int
    players: tuple[int, ...]
    x: int
    y: int
    button_seat: int
    board1: tuple[Card, ...]
    board2: tuple[Card, ...]
    hands: tuple[VectorHand, ...]

    @property
    def player_count(self) -> int:
        return len(self.players)


def load_vectors(path: Path = SPEC_PATH) -> tuple[FairnessVector, ...]:
    """Every vector in the specification, in document order.

    Raises:
        ValueError: if a block is missing a key, which means the document is wrong.
    """
    text = path.read_text(encoding="utf-8")
    vectors = [
        _build(_fields(block.group(1)))
        for block in _BLOCK.finditer(text)
        if block.group(1).lstrip().startswith("vector")
    ]
    return tuple(vectors)


def _fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"vector line is not 'key = value': {line!r}")
        fields[key.strip()] = value.strip()
    return fields


def _build(fields: dict[str, str]) -> FairnessVector:
    hands: list[VectorHand] = []
    seat = 0
    while f"deal_hole_{seat}" in fields:
        hands.append(
            VectorHand(
                seat=seat,
                hole=parse_cards(_field(fields, f"deal_hole_{seat}")),
                extra=parse_cards(_field(fields, f"deal_extra_{seat}")),
            )
        )
        seat += 1
    return FairnessVector(
        number=int(_field(fields, "vector")),
        seed=bytes.fromhex(_field(fields, "seed")),
        commitment=_field(fields, "commitment"),
        deck_first_ten=parse_cards(_field(fields, "deck_first_ten")),
        button_2=int(_field(fields, "button_2")),
        button_5=int(_field(fields, "button_5")),
        players=tuple(int(token) for token in _field(fields, "deal_players").split()),
        x=int(_field(fields, "deal_x")),
        y=int(_field(fields, "deal_y")),
        button_seat=int(_field(fields, "deal_button_seat")),
        board1=parse_cards(_field(fields, "deal_board1")),
        board2=parse_cards(_field(fields, "deal_board2")),
        hands=tuple(hands),
    )


def _field(fields: dict[str, str], key: str) -> str:
    if key not in fields:
        raise ValueError(f"vector is missing {key!r}; see docs/fairness-spec.md")
    return fields[key]

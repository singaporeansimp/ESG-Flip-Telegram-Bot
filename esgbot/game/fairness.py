"""Seed, commitment, deck order and button: the provable-fairness primitives.

Everything about a deal is a pure function of a 32-byte random seed and the
locked player list, so anyone can recompute it from the revealed seed. The
byte-level procedure, with test vectors, is published in ``docs/fairness-spec.md``;
this module is its only implementation and the two must agree.

In short::

    seed       = 32 random bytes (secrets)
    commitment = SHA-256(seed), lowercase hex
    deck order = the 52 cards sorted ascending by SHA-256(seed || bytes([card_index]))
    button     = int.from_bytes(SHA-256(seed || b"button"), "big") mod player_count
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

from esgbot.game.cards import DECK_SIZE, FULL_DECK, Card

__all__ = [
    "BUTTON_LABEL",
    "SEED_SIZE",
    "button_seat",
    "card_sort_key",
    "commitment",
    "generate_seed",
    "seed_from_hex",
    "seed_to_hex",
    "validate_seed",
]

SEED_SIZE: Final = 32
"""Seed length in bytes."""

BUTTON_LABEL: Final = b"button"
"""The fixed ASCII suffix hashed with the seed to choose the button."""


def generate_seed() -> bytes:
    """A fresh 32-byte seed from the operating system's CSPRNG."""
    return secrets.token_bytes(SEED_SIZE)


def validate_seed(seed: bytes) -> bytes:
    """Return ``seed`` unchanged, or raise :class:`ValueError` if it is not 32 bytes."""
    if len(seed) != SEED_SIZE:
        raise ValueError(f"seed must be {SEED_SIZE} bytes, got {len(seed)}")
    return seed


def seed_to_hex(seed: bytes) -> str:
    """The seed as lowercase hex, the form that is stored and revealed."""
    return validate_seed(seed).hex()


def seed_from_hex(text: str) -> bytes:
    """Read a seed from lowercase or uppercase hex.

    Raises:
        ValueError: if the text is not 64 hex digits.
    """
    try:
        seed = bytes.fromhex(text.strip())
    except ValueError as error:
        raise ValueError(f"seed must be {SEED_SIZE * 2} hex digits: {error}") from error
    return validate_seed(seed)


def commitment(seed: bytes) -> str:
    """The commitment published before any card is shown: lowercase hex SHA-256 of the seed."""
    return hashlib.sha256(validate_seed(seed)).hexdigest()


def card_sort_key(seed: bytes, card_index: int) -> bytes:
    """The 32-byte sort key of one card: ``SHA-256(seed || bytes([card_index]))``.

    The hash input is 33 bytes: the seed, then the single byte of the card index.
    """
    if not 0 <= card_index < DECK_SIZE:
        raise ValueError(f"card index must be 0..{DECK_SIZE - 1}, got {card_index}")
    return hashlib.sha256(validate_seed(seed) + bytes([card_index])).digest()


def deck_order(seed: bytes) -> tuple[Card, ...]:
    """The 52 cards in deal order for this seed.

    The cards are sorted ascending by their :func:`card_sort_key`, compared as
    unsigned big-endian byte strings. The card index breaks ties, which keeps the
    order total even in the (practically impossible) event of a SHA-256 collision.
    """
    validate_seed(seed)
    return tuple(sorted(FULL_DECK, key=lambda card: (card_sort_key(seed, card.index), card.index)))


def button_seat(seed: bytes, player_count: int) -> int:
    """The seat holding the button, 0-based, for this seed and player count.

    ``int.from_bytes(SHA-256(seed || b"button"), "big") mod player_count``.

    Raises:
        ValueError: if ``player_count`` is below 1.
    """
    if player_count < 1:
        raise ValueError(f"player count must be at least 1, got {player_count}")
    digest = hashlib.sha256(validate_seed(seed) + BUTTON_LABEL).digest()
    return int.from_bytes(digest, "big") % player_count

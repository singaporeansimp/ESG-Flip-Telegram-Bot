"""Fixtures shared by the storage tests: real deals wrapped as finished flips."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

from esgbot.game import fairness
from esgbot.game.deal import deal_cards
from esgbot.game.rules import Settings
from esgbot.storage import FinishedFlip, FlipPlayer, schema

SETTINGS = Settings(wager=50, x=5, y=2, z=3)
EPOCH = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Line:
    """One player's result in a flip, keyed by Telegram user id."""

    user_id: int
    name: str
    net: int
    username: str | None = None
    points: Fraction = Fraction(1)
    scooped: bool = False


def make_flip(
    group_id: int,
    lines: Sequence[Line],
    *,
    seed_byte: int,
    started_at: datetime = EPOCH,
    settings: Settings = SETTINGS,
) -> FinishedFlip:
    """A finished flip with a genuine deal; players follow the deal's seat order."""
    seed = bytes([seed_byte]) * fairness.SEED_SIZE
    deal = deal_cards(
        seed=seed, player_ids=[line.user_id for line in lines], x=settings.x, y=settings.y
    )
    by_id = {line.user_id: line for line in lines}
    players = tuple(
        FlipPlayer(
            user_id=hand.player_id,
            display_name=by_id[hand.player_id].name,
            username=by_id[hand.player_id].username,
            seat=seat,
            points=by_id[hand.player_id].points,
            net_dollars=by_id[hand.player_id].net,
            scooped=by_id[hand.player_id].scooped,
        )
        for seat, hand in enumerate(deal.hands)
    )
    return FinishedFlip(
        group_id=group_id,
        settings=settings,
        seed=seed,
        commitment=fairness.commitment(seed),
        deal=deal,
        players=players,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=2),
    )


def sync_connection(path: Path) -> sqlite3.Connection:
    """A second, synchronous connection to the file, for raw-SQL tampering and counts."""
    return schema.connect(path)


def count_rows(path: Path, table: str) -> int:
    connection = sync_connection(path)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])
    finally:
        connection.close()

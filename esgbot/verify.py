"""Re-running a recorded flip against the seed it was committed to.

This is the whole of ``/verify`` apart from its wording. It takes a flip read
back out of storage and recomputes it from the one thing everything else follows
from -- the 32-byte seed -- using the same functions the flip itself ran on:

1. :func:`~esgbot.game.fairness.commitment` over the stored seed, compared with
   the commitment the group was shown before any card appeared;
2. :func:`~esgbot.game.deal.deal_cards` with the stored seed, player list and
   settings, compared with the stored deal card for card, including the button;
3. :func:`~esgbot.game.settlement.settle_deal` over that recomputed deal,
   compared with every player's stored points, net dollars and scoop flag.

The results are recomputed from the **recomputed** deal rather than the stored
one, so a report answers one question all the way down: does what was stored
follow from the seed? Each kind of damage then has one definite answer -- a
changed seed fails on the commitment, a changed deal on the deal, a changed
result on the points or the nets -- rather than depending on whether the change
happened to matter.

:func:`verify_flip` is pure, and does the evaluator's work, so it belongs in a
worker thread: ``await asyncio.to_thread(verify_flip, stored)``. It raises
nothing. A flip whose settings and player list cannot be dealt at all -- which
takes a hand-edited database -- counts as a flip whose deal does not match, and
its results are left unchecked because there is nothing to compare them with.

What a pass means is written down in the Tech Plan and worth repeating: it shows
the stored flip follows from the committed seed. It does not show that the seed
was chosen fairly, because the bot's own code is checking the bot's own records.
Independent proof comes from someone running ``docs/fairness-spec.md`` against
the revealed seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from esgbot.game import fairness
from esgbot.game.deal import Deal, deal_cards
from esgbot.game.settlement import settle_deal
from esgbot.storage import FinishedFlip, StoredFlip

__all__ = ["PART_ORDER", "Verification", "VerifyPart", "verify_flip"]


class VerifyPart(StrEnum):
    """One thing a flip is checked on, and so one thing that can fail.

    The wording that goes with each is in :mod:`esgbot.telegram.render`, as
    every other word the bot says is.
    """

    COMMITMENT = "commitment"
    """The stored commitment is the SHA-256 of the stored seed."""

    DEAL = "deal"
    """Dealing the stored seed again gives the stored deal, button included."""

    POINTS = "points"
    """Every player's stored point total is what the recomputed deal scores."""

    NETS = "nets"
    """Every player's stored net dollars is what the recomputed deal settles."""

    SCOOP = "scoop"
    """The stored scoop flags are the ones the recomputed deal produces."""


PART_ORDER: Final = tuple(VerifyPart)
"""The order a report lists differences in: the seed's own check first, then down
through the deal to the money that came out of it."""


@dataclass(frozen=True, slots=True)
class Verification:
    """What came of checking one flip.

    ``commitment`` is the commitment as stored, which is the one the group was
    shown. ``recomputed_commitment`` is the SHA-256 of the stored seed; the two
    are equal unless :attr:`VerifyPart.COMMITMENT` is among the differences.

    ``differences`` is empty for a flip that verifies. Otherwise it holds the
    parts that did not match, in :data:`PART_ORDER`.

    Raises:
        ValueError: if the flip number is below one, or a part is listed twice or
            out of order.
    """

    flip_number: int
    commitment: str
    recomputed_commitment: str
    differences: tuple[VerifyPart, ...]

    def __post_init__(self) -> None:
        if self.flip_number < 1:
            raise ValueError(f"flip number must be 1 or more, got {self.flip_number}")
        ordered = tuple(part for part in PART_ORDER if part in self.differences)
        if self.differences != ordered:
            raise ValueError(
                f"differences must be distinct and in {PART_ORDER!r}, got {self.differences!r}"
            )

    @property
    def passed(self) -> bool:
        """Whether everything stored followed from the seed."""
        return not self.differences

    def differs(self, part: VerifyPart) -> bool:
        """Whether this part is one of the ones that did not match."""
        return part in self.differences


def verify_flip(stored: StoredFlip) -> Verification:
    """Recompute a stored flip from its seed and report what does not match.

    CPU-heavy, because it runs the evaluator over every player on all three
    boards, so call it in a worker thread.

    Args:
        stored: the flip as :meth:`~esgbot.storage.Database.fetch_flip` returned
            it. A flip that would not read back never gets this far: storage
            raises :class:`~esgbot.storage.CorruptRecordError` instead.

    Returns:
        The report. :attr:`Verification.passed` is the pass or fail.
    """
    flip = stored.flip
    recomputed_commitment = fairness.commitment(flip.seed)
    differences: list[VerifyPart] = []
    if recomputed_commitment != flip.commitment:
        differences.append(VerifyPart.COMMITMENT)

    deal = _redeal(flip)
    if deal is None or deal != flip.deal:
        differences.append(VerifyPart.DEAL)
    if deal is not None:
        differences.extend(_result_differences(flip, deal))

    return Verification(
        flip_number=stored.flip_number,
        commitment=flip.commitment,
        recomputed_commitment=recomputed_commitment,
        differences=tuple(part for part in PART_ORDER if part in differences),
    )


def _redeal(flip: FinishedFlip) -> Deal | None:
    """Deal the stored seed again, or ``None`` if these stored values cannot be dealt.

    Only a hand-edited database gets ``None``: every deal the bot writes was
    dealt by this function in the first place.
    """
    try:
        return deal_cards(
            seed=flip.seed,
            player_ids=flip.deal.player_ids,
            x=flip.settings.x,
            y=flip.settings.y,
        )
    except ValueError:
        return None


def _result_differences(flip: FinishedFlip, deal: Deal) -> list[VerifyPart]:
    """The parts of the money and the points that the recomputed deal disagrees with."""
    outcome = settle_deal(deal, flip.settings)
    differences: list[VerifyPart] = []
    for player, result in zip(flip.players, outcome.seats, strict=True):
        if player.points != result.points:
            differences.append(VerifyPart.POINTS)
        if player.net_dollars != result.net_dollars:
            differences.append(VerifyPart.NETS)
        if player.scooped != result.scooped:
            differences.append(VerifyPart.SCOOP)
    return differences

"""What a Peek says a hand has made, and the boards it must never mention.

The reading itself is a pure function of the cards a player holds and the boards
the group has already seen, so the first half of this is
:func:`~esgbot.engine.read_hand` over hands written out by hand -- the two rules
it has to get right being the Omaha one on a community board, exactly two of the
player's cards and three of the five, and the hand-strength board being the
player's own cards alone.

The second half plays real flips and stops them at each moment of the deal,
because the thing a pop-up must never do is read a hand against a board nobody
has been shown. Every card is fixed at Start, so a made hand on Board 2 before
Board 2 is out would be a hint at cards still to come.

The third half is where the work runs: in a worker thread, with neither of the
flip's locks held, so a Peek is answered promptly while Telegram is slow and
several at once do not queue behind each other.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections.abc import Callable, Sequence
from typing import Final

import pytest

from esgbot.engine import (
    Flip,
    FlipPhase,
    FlipView,
    HandReader,
    MadeHand,
    Notice,
    PeekAnswer,
    PressAnswer,
    TableStage,
    TableView,
    VoidReason,
    read_hand,
    read_hand_in_thread,
)
from esgbot.engine import peek as peek_module
from esgbot.game.cards import FULL_DECK, Card, parse_cards
from esgbot.game.deal import Deal, deal_cards
from esgbot.game.evaluator import Category, best_hand, best_hand_for_board
from esgbot.game.rules import Settings
from esgbot.game.scoring import Board
from tests.engine_helpers import (
    SETTINGS,
    FakeClock,
    FakeGateway,
    Harness,
    HoldsAView,
    harness,
    harness_over,
)

_IDS: Final = [11, 12]
"""The starter and one joiner, in join order, which is seat order."""

NO_EXTRAS: Final = Settings(wager=50, x=5, y=0, z=3)
"""A flip that asked for no extra cards, so every hand is final from the deal."""

_COMMUNITY: Final = (Board.BOARD1, Board.BOARD2)


# ------------------------------------------------------------- the reading itself


def test_a_reading_ends_with_the_hand_strength_board_however_much_is_out() -> None:
    deal = deal_cards(seed=bytes(range(32)), player_ids=_IDS, x=5, y=2)
    cards = deal.hands[0].cards
    assert [made.board for made in read_hand(cards, ())] == [Board.HAND_STRENGTH]
    assert [made.board for made in read_hand(cards, deal.boards[:1])] == [
        Board.BOARD1,
        Board.HAND_STRENGTH,
    ]
    assert [made.board for made in read_hand(cards, deal.boards)] == list(Board)


def test_a_reading_is_the_evaluators_own_answer_for_every_board() -> None:
    deal = deal_cards(seed=bytes(range(32)), player_ids=_IDS, x=5, y=2)
    for hand in deal.hands:
        made = read_hand(hand.cards, deal.boards)
        assert made[-1].hand == best_hand(hand.cards)
        for index, board in enumerate(_COMMUNITY):
            assert made[index].board is board
            assert made[index].hand == best_hand_for_board(hand.cards, deal.boards[index])


def test_the_hand_strength_board_is_read_from_the_players_own_cards_alone() -> None:
    hand = parse_cards("A♥ K♥ Q♥ J♥ 9♥")
    boards = (parse_cards("2♠ 3♠ 4♠ 5♠ 7♠"), parse_cards("2♣ 3♣ 4♣ 5♣ 7♣"))

    board1, board2, strength = read_hand(hand, boards)

    assert strength.hand.rank.category is Category.FLUSH
    assert set(strength.hand.cards) == set(hand)
    # Neither community board can be played in hearts, because only two of the
    # player's cards may be used and the board has none.
    assert board1.hand.rank.category is Category.HIGH_CARD
    assert board2.hand.rank.category is Category.HIGH_CARD


def test_a_community_board_takes_exactly_two_of_the_players_cards() -> None:
    """Four hearts in hand is no flush on a board holding two, and is one on three.

    The Omaha rule is an equality, not a ceiling: two of the player's cards and
    three of the board's, so a fifth heart in hand buys nothing and a third on
    the board buys everything.
    """
    hand = parse_cards("A♥ K♥ Q♥ J♥ 9♠")
    two_hearts = parse_cards("2♥ 3♥ 4♠ 5♠ 7♠")
    three_hearts = parse_cards("2♥ 3♥ 4♥ 5♠ 7♠")

    (short,) = read_hand(hand, (two_hearts,))[:1]
    (made,) = read_hand(hand, (three_hearts,))[:1]

    assert short.hand.rank.category is not Category.FLUSH
    assert made.hand.rank.category is Category.FLUSH
    assert len([card for card in made.hand.cards if card in hand]) == 2
    assert len([card for card in made.hand.cards if card in three_hearts]) == 3


def test_a_community_board_can_never_be_played_on_its_own() -> None:
    hand = parse_cards("A♥ K♦ Q♣ J♠ 9♥")
    board = parse_cards("2♠ 3♠ 4♠ 5♠ 6♠")

    (made,) = read_hand(hand, (board,))[:1]

    assert made.hand.rank.category is not Category.STRAIGHT_FLUSH
    assert len([card for card in made.hand.cards if card in hand]) == 2


def test_more_boards_than_a_flip_has_is_refused() -> None:
    hand = parse_cards("A♥ K♦ Q♣ J♠ 9♥")
    board = parse_cards("2♠ 3♠ 4♠ 5♠ 6♦")
    with pytest.raises(ValueError, match="2 community boards, got 3"):
        read_hand(hand, (board, board, board))


# ------------------------------------------------- the boards a peek may be read against


def _at(stage: TableStage) -> Callable[[FlipView], bool]:
    """Hold the round just before it shows the table at this stage."""
    return lambda view: isinstance(view, TableView) and view.stage is stage


def _world_held_at(stage: TableStage, *, settings: Settings) -> tuple[Harness, HoldsAView]:
    clock = FakeClock()
    gateway = HoldsAView(clock, chosen=_at(stage))
    return harness_over(gateway, clock=clock, settings=settings), gateway


def _recorded_deal(world: Harness) -> Deal:
    (recorded,) = world.store.recorded
    return recorded.deal


def _cards_of(made: Sequence[MadeHand]) -> set[Card]:
    return {card for one in made for card in one.hand.cards}


@pytest.mark.parametrize(
    ("stage", "shown", "settings", "final"),
    [
        pytest.param(TableStage.HOLE_CARDS, 0, SETTINGS, False, id="before-the-table-is-drawn"),
        pytest.param(TableStage.BOARD1, 0, SETTINGS, False, id="before-board-1-is-shown"),
        pytest.param(TableStage.BOARD2, 1, SETTINGS, False, id="board-1-alone"),
        pytest.param(TableStage.PEEKING, 2, SETTINGS, True, id="both-boards-and-the-extras"),
        pytest.param(TableStage.PEEKING, 2, NO_EXTRAS, True, id="both-boards-and-no-extras"),
    ],
)
def test_a_peek_reads_only_the_boards_the_table_has_already_shown(
    stage: TableStage, shown: int, settings: Settings, final: bool
) -> None:
    """The secrecy rule, one moment of the deal at a time.

    The round is stopped with the table edit for ``stage`` still in flight, so
    the group has seen everything before it and nothing of it. The answer may
    name the boards that are out and the player's own cards, and no card of a
    board still to come may appear anywhere in it -- not as a board, not inside
    a made hand.
    """

    async def scenario() -> None:
        world, gateway = _world_held_at(stage, settings=settings)
        async with world.registry:
            flip = await world.open_lobby(joiners=1, settings=settings)
            # Far enough in for the two-second gap between the boards to have
            # passed, so the round has reached whichever edit is being held.
            await world.deal(flip)
            assert gateway.reached.is_set()

            answer = await flip.peek(_IDS[0], press_id=world.press())

            assert isinstance(answer, PeekAnswer)
            gateway.go.set()
            await world.finish(flip)

            deal = _recorded_deal(world)
            mine = deal.hands[0]
            out = deal.boards[:shown]
            to_come = {card for board in deal.boards[shown:] for card in board}

            assert [made.board for made in answer.made] == [
                *_COMMUNITY[:shown],
                Board.HAND_STRENGTH,
            ]
            held = mine.cards if final else mine.hole
            assert answer.cards == held
            assert answer.extra == (mine.extra if final else ())
            assert answer.made == read_hand(held, out)
            # Nothing in the answer comes from a board the group has not seen, and
            # nothing from another player's hand either.
            assert _cards_of(answer.made) <= {*held, *(card for board in out for card in board)}
            assert _cards_of(answer.made).isdisjoint(to_come)
            assert set(answer.cards).isdisjoint(to_come)
            assert set(answer.cards).isdisjoint(deal.hands[1].cards)

    asyncio.run(scenario())


def test_the_peek_count_does_not_change_with_what_the_reading_says() -> None:
    """A peek before the extra cards still does not count, reading or no reading."""

    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            early = await flip.peek(11, press_id=world.press())
            assert isinstance(early, PeekAnswer)
            assert early.made  # it says what the hand has, and still does not count

            await world.clock.advance(1_000)
            table = [view for view in world.gateway.views if isinstance(view, TableView)]
            assert [view.peeked for view in table if view.stage is TableStage.PEEKING] == [0]
            await world.finish(flip)

    asyncio.run(scenario())


# ------------------------------------------------------- where the reading runs


def test_the_default_reader_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    ran_on: list[int] = []

    def spy(cards: Sequence[Card], boards: Sequence[Sequence[Card]]) -> tuple[MadeHand, ...]:
        ran_on.append(threading.get_ident())
        return read_hand(cards, boards)

    deal = deal_cards(seed=bytes(range(32)), player_ids=_IDS, x=5, y=2)
    monkeypatch.setattr(peek_module, "read_hand", spy)

    async def scenario() -> None:
        made = await read_hand_in_thread(deal.hands[0].cards, deal.boards)
        assert [one.board for one in made] == list(Board)

    asyncio.run(scenario())
    assert len(ran_on) == 1
    assert ran_on[0] != threading.get_ident()


class _HoldsAReading:
    """A hand reader that holds the first reading open until the test lets it go.

    Every reading after that is immediate, so one player's Peek can be caught
    half way through while the flip carries on around it.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.go = asyncio.Event()
        self.calls = 0

    async def __call__(
        self, cards: Sequence[Card], boards: Sequence[Sequence[Card]]
    ) -> tuple[MadeHand, ...]:
        self.calls += 1
        if self.calls == 1:
            self.reached.set()
            await self.go.wait()
        return read_hand(cards, boards)


def _world_reading(reader: HandReader) -> Harness:
    """A plain flip world whose peek readings go through ``reader``."""
    clock = FakeClock()
    return harness_over(FakeGateway(clock), clock=clock, read_hand=reader)


def test_one_hand_being_read_does_not_hold_up_another_players_peek() -> None:
    """The state lock is not held across the reading, so two peeks do not queue.

    Were it held, the second press could not even be looked at until the first
    reading came back -- and a 21-card hand takes tens of milliseconds, with a
    Telegram answer only good for a few seconds.
    """

    async def scenario() -> None:
        reader = _HoldsAReading()
        world = _world_reading(reader)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            first = asyncio.create_task(flip.peek(11, press_id=world.press()))
            await asyncio.wait_for(reader.reached.wait(), timeout=1)

            second = await asyncio.wait_for(flip.peek(12, press_id=world.press()), timeout=1)

            assert isinstance(second, PeekAnswer)
            assert second.made
            assert not first.done()
            reader.go.set()
            answered = await asyncio.wait_for(first, timeout=1)
            assert isinstance(answered, PeekAnswer)
            assert answered.seat == 0
            assert second.seat == 1
            await world.finish(flip)

    asyncio.run(scenario())


def test_a_peek_is_answered_after_the_press_rather_than_before_the_reading() -> None:
    """Nothing goes out until the reading is in, so one pop-up holds the lot."""

    async def scenario() -> None:
        reader = _HoldsAReading()
        world = _world_reading(reader)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)
            answers = len(world.gateway.answers)

            pressed = asyncio.create_task(flip.peek(11, press_id=world.press()))
            await asyncio.wait_for(reader.reached.wait(), timeout=1)
            await world.clock.quiesce()

            assert len(world.gateway.answers) == answers
            reader.go.set()
            answer = await asyncio.wait_for(pressed, timeout=1)
            assert world.gateway.answers[-1].answer is answer
            await world.finish(flip)

    asyncio.run(scenario())


def test_a_flip_that_finishes_while_a_hand_is_read_still_answers_the_press() -> None:
    """The hand was that player's when they pressed, so the pop-up still goes out."""

    async def scenario() -> None:
        reader = _HoldsAReading()
        world = _world_reading(reader)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            pressed = asyncio.create_task(flip.peek(11, press_id=world.press()))
            await asyncio.wait_for(reader.reached.wait(), timeout=1)
            await world.finish(flip)
            assert flip.phase is FlipPhase.FINISHED

            reader.go.set()
            answer = await asyncio.wait_for(pressed, timeout=1)

            assert isinstance(answer, PeekAnswer)
            assert answer.made
            assert world.gateway.answers[-1].answer is answer

    asyncio.run(scenario())


def test_a_flip_voided_while_a_hand_is_read_still_answers_the_press() -> None:
    async def scenario() -> None:
        reader = _HoldsAReading()
        world = _world_reading(reader)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            pressed = asyncio.create_task(flip.peek(11, press_id=world.press()))
            await asyncio.wait_for(reader.reached.wait(), timeout=1)
            await flip.void(VoidReason.ERROR)
            assert flip.phase is FlipPhase.VOIDED

            reader.go.set()
            answer = await asyncio.wait_for(pressed, timeout=1)

            assert isinstance(answer, PeekAnswer)
            assert answer.made
            assert await flip.peek(11, press_id=world.press()) is Notice.EXPIRED

    asyncio.run(scenario())


def test_a_peek_cancelled_while_its_hand_is_read_stays_cancelled() -> None:
    """A press dropped part way through -- the bot stopping -- is not swallowed."""

    async def scenario() -> None:
        reader = _HoldsAReading()
        world = _world_reading(reader)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            answers = len(world.gateway.answers)

            pressed = asyncio.create_task(flip.peek(11, press_id=world.press()))
            await asyncio.wait_for(reader.reached.wait(), timeout=1)
            pressed.cancel()

            with pytest.raises(asyncio.CancelledError):
                await pressed
            assert pressed.cancelled()
            assert len(world.gateway.answers) == answers
            await world.finish(flip)

    asyncio.run(scenario())


def test_pressing_peek_twice_answers_both_presses_with_the_same_reading() -> None:
    async def scenario() -> None:
        world = harness()
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)
            await world.clock.advance(1.0)

            first = await flip.peek(11, press_id=world.press())
            second = await flip.peek(11, press_id=world.press())

            assert isinstance(first, PeekAnswer)
            assert first == second
            assert len(world.gateway.answers) >= 2
            await world.finish(flip)

    asyncio.run(scenario())


def test_a_reading_that_breaks_still_shows_the_player_their_cards() -> None:
    """A pop-up is the one thing in a flip nothing depends on, so it degrades."""
    broken: list[int] = []

    async def unreadable(
        cards: Sequence[Card], boards: Sequence[Sequence[Card]]
    ) -> tuple[MadeHand, ...]:
        broken.append(len(cards))
        raise RuntimeError("the evaluator fell over")

    async def scenario() -> None:
        world = _world_reading(unreadable)
        async with world.registry:
            flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            answer: PressAnswer = await flip.peek(11, press_id=world.press())

            assert isinstance(answer, PeekAnswer)
            assert answer.made == ()
            assert len(answer.hole) == world.settings.x
            assert world.gateway.answers[-1].answer is answer
            await world.finish(flip)
            assert flip.phase is FlipPhase.FINISHED

    asyncio.run(scenario())
    assert broken


def test_a_peek_by_somebody_who_is_not_playing_reads_nothing() -> None:
    read: list[int] = []

    async def counting(
        cards: Sequence[Card], boards: Sequence[Sequence[Card]]
    ) -> tuple[MadeHand, ...]:
        read.append(len(cards))
        return read_hand(cards, boards)

    async def scenario() -> None:
        world = _world_reading(counting)
        async with world.registry:
            flip: Flip = await world.open_lobby(joiners=1)
            await world.start(flip)

            assert await flip.peek(99, press_id=world.press()) is Notice.NOT_A_PLAYER
            assert read == []
            assert isinstance(await flip.peek(11, press_id=world.press()), PeekAnswer)
            assert len(read) == 1
            await world.finish(flip)

    asyncio.run(scenario())


# ------------------------------------------------------------------------- the nuts


def _nuts(hand: str, *boards: str) -> list[bool]:
    """Which boards of this reading are the nuts: the boards shown, then hand strength."""
    return [made.nuts for made in read_hand(parse_cards(hand), [parse_cards(b) for b in boards])]


_SPADES = "Q♠ J♠ 10♠ 2♦ 3♣"
_FILLER = "2♥ 3♥ 4♥ 5♥"


def test_a_hand_nobody_can_beat_on_the_cards_in_view_is_the_nuts() -> None:
    # The royal flush itself, and nothing beats it.
    assert _nuts(f"A♠ K♠ {_FILLER}", _SPADES) == [True, False]
    # A♠ is in this hand and 9♠ is too, so the K♠ left cannot make a better straight flush.
    assert _nuts(f"A♠ 9♠ 8♠ {_FILLER}", _SPADES) == [True, False]


def test_a_hand_that_somebody_could_beat_is_not_the_nuts() -> None:
    # A♠ K♠ are both unseen, so somebody may hold the royal flush.
    assert _nuts(f"9♠ 8♠ {_FILLER}", _SPADES) == [False, False]


def test_the_hand_strength_board_is_never_marked() -> None:
    assert _nuts(f"A♠ K♠ {_FILLER}") == [False]


def test_a_board_that_is_not_out_is_not_read() -> None:
    assert len(_nuts(f"A♠ K♠ {_FILLER}")) == 1
    assert len(_nuts(f"A♠ K♠ {_FILLER}", _SPADES)) == 2


def test_the_other_board_hides_cards_from_the_nuts_only_once_it_is_out() -> None:
    """Both boards come from one deck, so a card on Board 2 is nobody's hole card once seen.

    On Board 1 a Q-high straight flush loses to the royal flush while the A♥ and
    K♥ are both unseen; when the K♥ turns up on Board 2 the royal flush cannot
    be held by anybody, and the hand is the nuts. Before Board 2 is out the K♥ is
    still anybody's, which is what keeps this from telling a player anything.
    """
    hand = "9♥ 8♥ 2♦ 3♦ 4♦ 5♦"
    board1 = "Q♥ J♥ 10♥ 2♠ 3♠"
    board2 = "K♥ 7♣ 6♣ 4♣ 2♣"

    assert _nuts(hand, board1) == [False, False]
    assert _nuts(hand, board1, board2) == [True, False, False]


def test_the_peek_a_flip_raises_marks_the_nuts_it_found() -> None:
    deal = deal_cards(seed=bytes(range(32)), player_ids=_IDS, x=5, y=2)
    for hand in deal.hands:
        made = read_hand(hand.cards, deal.boards)
        assert [one.nuts for one in made] == [
            all(
                best_hand_for_board(pair, deal.boards[index]).rank <= one.hand.rank
                for pair in itertools.combinations(
                    [
                        c
                        for c in FULL_DECK
                        if c not in {*hand.cards, *deal.boards[0], *deal.boards[1]}
                    ],
                    2,
                )
            )
            for index, one in enumerate(made[:2])
        ] + [False]

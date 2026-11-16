# ESG Flip Bot: fairness specification

Every card in a flip is derived from one 32-byte seed. The bot publishes a commitment to that
seed **before any card is shown** and reveals the seed with the result (or with the void notice),
so anyone can recompute the entire deal and check that nothing changed after the commitment.

This document is the authoritative description of that derivation, down to the bytes that are
hashed. It is written so that an independent implementation — in any language — reproduces the
bot's deals exactly. The implementation lives in
[`esgbot/game/fairness.py`](../esgbot/game/fairness.py) and
[`esgbot/game/deal.py`](../esgbot/game/deal.py), and the test suite checks both against the
[test vectors](#test-vectors) below.

Nothing here may change without a new flip-record version: old flips must stay verifiable.

## Notation

- `SHA-256(m)` is the SHA-256 digest of the byte string `m`, 32 bytes.
- `a || b` is byte concatenation.
- Hex strings are lowercase, two digits per byte, with no prefix or separators.
- All integers below are unsigned.

## Card index

Cards are numbered 0 to 51 in **suit-major** order:

```
index = suit * 13 + (rank - 2)
```

| Suit | Value |
| --- | --- |
| clubs `♣` | 0 |
| diamonds `♦` | 1 |
| hearts `♥` | 2 |
| spades `♠` | 3 |

| Rank | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | J | Q | K | A |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Value | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 |

So index 0 is `2♣`, index 12 is `A♣`, index 13 is `2♦`, index 39 is `2♠` and index 51 is `A♠`.
One deck, 52 cards, no jokers. The index is the only form cards are stored in.

## Seed

```
seed = 32 bytes from the operating system's CSPRNG (Python: secrets.token_bytes(32))
```

The seed is generated when **Start** is pressed, after the player list is locked. It is stored
with the flip and revealed afterwards, as 64 lowercase hex digits.

## Commitment

```
commitment = SHA-256(seed), as 64 lowercase hex digits
```

The hash input is exactly the 32 seed bytes: no salt, no prefix, no hex, no newline.

The commitment is posted as its own message, before any card is shown, and that message is
never edited. (The table message is edited many times, and edited messages are not a reliable
record.)

## Deck order

Each card gets a 32-byte sort key:

```
key(i) = SHA-256(seed || bytes([i]))        for card index i in 0..51
```

The hash input is 33 bytes: the 32 seed bytes, then one byte holding the card index.

The deck order is the 52 cards sorted **ascending by `key(i)`**, comparing the keys as unsigned
big-endian 32-byte integers — equivalently, lexicographically as byte strings. If two keys were
ever equal, the lower card index comes first; with SHA-256 this never happens in practice, and
the rule exists only so the order is total.

Cards are drawn from the front of this order.

## Button

```
button_seat = int.from_bytes(SHA-256(seed || b"button"), "big") mod player_count
```

The hash input is 38 bytes: the 32 seed bytes, then the 6 ASCII bytes `62 75 74 74 6f 6e`
(`"button"`). The digest is read as one 256-bit **big-endian** unsigned integer, then reduced
modulo the number of players. Seats are numbered from 0 in join order, so the result is a seat.

(The modulo makes the lowest seats very slightly more likely — by about one part in 2²⁵⁴. It is
not measurable and not worth correcting for.)

## Deal procedure

Inputs: the seed, the player list in join order, `X` (hole cards per player) and `Y` (maximum
extra cards per player). Seat `i` is the `i`-th player to have joined, counting from 0.

The bot itself never says `X`, `Y` or `Z` to a player: it calls `X` the **private cards**, `Y` the
**extra cards** and `Z` the **scoop multiplier**, which plays no part in the deal at all. This
document keeps the letters, because they are what the formulas below are written with.

Nothing here depends on how the flip was configured. A command may leave `Y` out, in which case
the bot fills in the maximum (`Y = 21`) before the deal, and `Z` out, in which case it fills in
the number of players — but both are resolved when **Start** locks the player list, which is
before the seed exists, and what is stored and revealed is the values the deal actually ran on.
So an independent implementation reads `X` and `Y` from the stored flip and needs to know nothing
about the command that opened it.

**Dealing order.** Every round of dealing starts with the seat after the button and moves up
through the seats, wrapping around:

```
order = [(button_seat + 1 + k) mod player_count   for k in 0 .. player_count - 1]
```

**Rounds.** Cards come off the front of the deck order in one continuous stream:

1. **Hole cards.** `X` rounds of one card per seat, dealt in `order`.
2. **Board 1.** The next 5 cards, in order.
3. **Board 2.** The next 5 cards, in order.
4. **Extra cards.** Rounds of one card per seat, dealt in `order`, for as long as every seat can
   still get one, up to `Y` rounds:

   ```
   extra_rounds = min(Y, (52 - X * player_count - 10) // player_count)
   ```

   So either every player receives the same number of extra cards, or nobody receives any. Cards
   left over after the last complete round stay in the deck and are never shown.

**Player cap.** Both boards take 10 cards, which leaves 42 for the players, so a flip holds at
most `floor(42 / X)` players. With `X = 5` that is 8 players, who use 50 of the 52 cards and
therefore get no extra cards whatever `Y` says.

**Worked example.** 8 players, `X = 5`: `8 × 5 + 10 = 50` cards dealt, 2 left, `2 // 8 = 0`
extra rounds. 2 players, `X = 5`, `Y = 21`: 20 cards dealt, 32 left, `32 // 2 = 16` extra
rounds, so each player ends with 21 cards.

## Stored form and reveal

A finished flip stores the seed (hex), the commitment, the button seat, all four settings and the
full deal. It also stores one flag per setting the command left out, which `/again` reads to
repeat the command; the deal and the money follow from the settings themselves, so those flags
are no part of this procedure and no part of what `/verify` checks. The deal is stored as JSON
with every card written as its index:

```json
{
  "version": 1,
  "x": 5,
  "y": 2,
  "button_seat": 1,
  "board1": [43, 12, 30, 13, 49],
  "board2": [48, 45, 2, 39, 40],
  "hands": [
    {"seat": 0, "player_id": 1001, "hole": [32, 51, 1, 33, 7], "extra": [16, 8]},
    {"seat": 1, "player_id": 1002, "hole": [5, 23, 38, 25, 15], "extra": [26, 22]}
  ]
}
```

`/verify <flip number>` reads the stored seed and deal, recomputes the commitment, deck order,
button and deal from the seed, re-runs scoring and settlement, and reports whether everything
matches what was stored.

## What this proves, and what it does not

It proves that the deal and the result match a seed that was committed to before any card was
shown, and that the commitment message was posted before the cards appeared.

It does **not** prove:

- **That the operator did not choose a favourable seed.** The operator sees the player list
  before the seed exists and could try many seeds and keep one it likes. Only an independent
  source of public randomness would prevent that, and this bot deliberately does not use one.
- **That the cards were hidden from the operator.** The server holds the seed from the start, so
  whoever runs the bot can see every hand.
- **Honesty, when `/verify` says "pass".** That command runs the bot's own code against the
  operator's own database, so it checks consistency, not honesty. Independent proof comes from
  someone else running this specification against the revealed seed.

Revealing the seed for voided flips as well means the operator cannot quietly abandon a flip
after seeing a bad deal: the group can always recompute what the deal would have been.

## Test vectors

Three vectors, each giving a seed, its commitment, the first ten cards of the deck order, the
button for 2 and for 5 players, and one complete deal. The test suite reads these blocks from
this file, so the document and the code cannot drift apart.

Format: fenced `text` blocks, one per vector, holding `key = value` lines. Cards are written as
rank plus suit symbol, separated by spaces; an empty value is an empty list of cards.
`deal_hole_<seat>` and `deal_extra_<seat>` give one seat's cards in the order they were dealt.

The seeds are fixed and chosen to be reproducible, not random: vector 1 is 32 zero bytes,
vector 2 is the bytes `0x00` through `0x1f` in order, and vector 3 is
`SHA-256("esg flip bot")` (a label picked so that the three vectors between them produce
different buttons). Vector 1 is the 2-player `Y = 21` case, which deals out 16 extra cards each;
vector 2 caps the extra rounds with a small `Y`; vector 3 is the 8-player `X = 5` case, where
only 2 cards are left over and nobody gets an extra card.

```text
vector = 1
seed = 0000000000000000000000000000000000000000000000000000000000000000
commitment = 66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925
deck_first_ten = 7♣ 8♥ Q♦ A♠ A♥ 3♣ A♦ 9♥ 4♦ 9♣
button_2 = 0
button_5 = 4
deal_players = 1001 1002
deal_x = 5
deal_y = 21
deal_button_seat = 0
deal_board1 = 10♠ A♣ 6♥ 2♦ Q♠
deal_board2 = J♠ 8♠ 4♣ 2♠ 3♠
deal_hole_0 = 8♥ A♠ 3♣ 9♥ 9♣
deal_extra_0 = 5♦ 10♣ Q♥ J♦ 2♣ 6♦ 8♦ 7♦ K♦ 3♦ K♠ 10♦ J♣ 10♥ K♥ 8♣
deal_hole_1 = 7♣ Q♦ A♥ A♦ 4♦
deal_extra_1 = 2♥ Q♣ 7♥ 9♠ J♥ 6♣ 6♠ K♣ 3♥ 7♠ 5♥ 4♠ 5♣ 4♥ 5♠ 9♦
```

```text
vector = 2
seed = 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
commitment = 630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd
deck_first_ten = 10♦ K♣ 7♣ 2♣ 5♦ 9♥ J♦ 6♥ 6♠ 4♠
button_2 = 0
button_5 = 3
deal_players = 2001 2002 2003
deal_x = 6
deal_y = 2
deal_button_seat = 0
deal_board1 = 5♥ 4♣ 8♥ 5♠ K♥
deal_board2 = 3♦ 3♠ 2♠ 4♦ 3♣
deal_hole_0 = 7♣ 9♥ 6♠ 4♥ 10♠ 8♠
deal_extra_0 = J♣ 8♦
deal_hole_1 = 10♦ 2♣ J♦ 4♠ K♦ A♦
deal_extra_1 = A♣ 5♣
deal_hole_2 = K♣ 5♦ 6♥ 7♠ 9♠ 8♣
deal_extra_2 = 2♥ 9♣
```

```text
vector = 3
seed = 837ee35414398ef9db85d36fbfad5d4e10473f50b66cb9e0718cf81e87eed932
commitment = 094734e342580aea383432a0a3fb6f35059ac4916d3af6648d98e9beca3634d9
deck_first_ten = 10♥ Q♣ 8♦ 2♦ 9♥ 7♦ A♠ 9♠ 4♠ K♦
button_2 = 1
button_5 = 3
deal_players = 3001 3002 3003 3004 3005 3006 3007 3008
deal_x = 5
deal_y = 3
deal_button_seat = 7
deal_board1 = 7♣ Q♦ 8♣ 7♠ 2♠
deal_board2 = J♥ 10♣ 6♠ 5♦ 6♥
deal_hole_0 = 10♥ 4♠ K♥ 7♥ 10♠
deal_extra_0 = 
deal_hole_1 = Q♣ K♦ 6♦ 4♥ 8♥
deal_extra_1 = 
deal_hole_2 = 8♦ A♦ Q♠ 6♣ 5♣
deal_extra_2 = 
deal_hole_3 = 2♦ 4♣ 9♣ A♣ 3♠
deal_extra_3 = 
deal_hole_4 = 9♥ 5♥ 9♦ 3♦ 3♣
deal_extra_4 = 
deal_hole_5 = 7♦ K♠ J♦ 2♥ J♠
deal_extra_5 = 
deal_hole_6 = A♠ 3♥ 8♠ 4♦ A♥
deal_extra_6 = 
deal_hole_7 = 9♠ J♣ 5♠ 10♦ K♣
deal_extra_7 = 
```

### Intermediate values

For vector 1 (`seed = 00 × 32`), to check the exact hash inputs without implementing the whole
procedure:

| Value | Hash input | Digest (hex) |
| --- | --- | --- |
| `key(0)` (`2♣`) | 32 zero bytes, then `00` | `7f9c9e31ac8256ca2f258583df262dbc7d6f68f2a03043d5c99a4ae5a7396ce9` |
| `key(1)` (`3♣`) | 32 zero bytes, then `01` | `1fd4247443c9440cb3c48c28851937196bc156032d70a96c98e127ecb347e45f` |
| `key(51)` (`A♠`) | 32 zero bytes, then `33` | `154a2797f271dcb9dddce221f5ec00865387ac25d721045987cc5cb693bef22e` |
| button digest | 32 zero bytes, then `627574746f6e` | `0ef4538d80545fe1af397aaab17fdec3f6cf02ceacc9bdad58fa20de31896faa` |

The smallest of the 52 keys for this seed is
`086fb60bd968fe68ecec6a8d826ea5aa7d3d8020e644d7c5d0e07ded456ca3e8`, which belongs to card index
5 (`7♣`) — the first card of the deck order, as the vector shows.

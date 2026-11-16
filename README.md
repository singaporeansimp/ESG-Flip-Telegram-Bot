# ESG Flip Bot

A Telegram bot that runs ESG (Extremely Stupid Game) poker flips between members of a group
chat and posts the result of each one.

ESG is a poker variant with two community boards plus a third scoring "board" for pure hand
strength. Each flip is configured with a wager and three settings — `X` hole cards per player,
up to `Y` extra cards after the boards, and a scoop multiplier `Z` — and every board is worth
one point. The player with the most points takes the pot; a player who takes all three points
scoops and collects `wager × Z` from each other player.

> **Status.** The bot runs flips end to end: `/flip` opens a lobby, the round deals itself,
> players peek privately, and the result is posted and recorded. `/leaderboard`, `/stats` and
> `/verify` read those recorded flips back, and [`deploy/`](deploy/) runs it as a service with
> nightly backups — see [Deploying on a VPS](#deploying-on-a-vps). What is left is the live
> check: a few real flips in a test group, which is the only way to see whether Telegram
> throttles the edits a flip makes.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for dependency management

Install uv if you do not have it:

```sh
curl -fsSL https://astral.sh/uv/install.sh | sh
```

## Install

```sh
git clone <this repository> && cd TelegramBotESGFlip
uv sync
```

`uv sync` creates `.venv`, installs the project in editable mode and installs the dev tools
(pytest, ruff, mypy). Run everything through `uv run`, which uses that environment; there is no
need to activate it.

## Configure

Configuration comes from environment variables. Only the token is required; the bot stops with a
message naming the variable if it is missing.

| Variable | Used for | Default |
| --- | --- | --- |
| `ESGBOT_TELEGRAM_TOKEN` | The bot token from @BotFather. Never logged. | — (required) |
| `ESGBOT_DB_PATH` | Path to the SQLite database file, created if it is not there. | `esgbot.sqlite3` |
| `ESGBOT_LOG_LEVEL` | Log level: `DEBUG`, `INFO`, `WARNING`, … | `INFO` |

Logging is configured in one place, `esgbot/logging_setup.py`:

```python
from esgbot.logging_setup import setup_logging

setup_logging()  # INFO to stderr, or whatever ESGBOT_LOG_LEVEL says
```

## Run the checks

```sh
uv run pytest                  # tests
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
uv run mypy                    # type checking (strict, paths come from pyproject.toml)
```

All four are expected to pass on a fresh `uv sync`. `uv run ruff format .` rewrites files
instead of just reporting.

## Run the bot

```sh
export ESGBOT_TELEGRAM_TOKEN='123456:your-token-from-botfather'
export ESGBOT_DB_PATH=./esgbot.sqlite3
uv run python -m esgbot
```

The bot long-polls Telegram, so it needs no domain, no TLS and no reverse proxy — but **run
exactly one instance**: two pollers on the same token fight over updates. It starts by voiding any
flip that was still open when it last stopped, telling each group and revealing that flip's seed,
and then begins polling.

Add it to a group and give it permission to send and edit messages. Telegram's default privacy
mode is fine: the bot only needs slash commands and button presses. In a group with topics, a
flip stays in the topic `/flip` was sent in.

Telegram gives a group a new id when it becomes a supergroup. The group's history moves to the
new id, and so does a flip that is open at the time: the round carries on in the same chat, in
the same topic, and a second `/flip` there is still refused while it runs.

### Commands

| Command | Does |
| --- | --- |
| `/flip <wager> <X> <Y> <Z>` | Opens a lobby, for example `/flip 50 5 2 3`. One flip per group at a time. |
| `/leaderboard` | The group's top twenty by net winnings, with flips played, win rate and scoops. |
| `/stats` | Your own figures. `/stats @name`, or `/stats` as a reply to somebody's message, shows theirs. |
| `/verify <flip number>` | Re-runs a finished flip against its seed and reports whether everything still matches. |

The bot registers these four with Telegram on every start, so they appear when someone types `/` in a
chat with it. If Telegram refuses, the bot logs a warning and runs anyway; the commands still work typed out.

The lobby's **Join**, **Leave**, **Start** and **Cancel** buttons run the rest. Only whoever
opened the flip can Start or Cancel it, and a lobby closes itself after ten minutes with no joins
or leaves. Once the round starts, the one button left is **Peek**, which shows a player their own
cards in a private pop-up — no direct chat with the bot needed. Peek works from the moment the
deck commitment is posted, so no hand is ever shown before the group has been told which deck it
came from.

All four commands belong in a group: flips, and the stats that come out of them, are kept per
group, so the same person has separate figures in each one.

### Stats

A flip counts once its result has been saved, so a cancelled or voided flip never appears. A
**win** is a flip whose net came out above $0 — a chop winner who gets their wager back played
without winning — and win rate is wins divided by flips played. The leaderboard ranks by net
winnings, then wins, then name.

`/stats @name` looks for the most recent flip in the group that was played under that username,
so a username that has changed hands belongs to whoever used it last. Players with no username
at all are reached by replying to one of their messages with `/stats`.

## Deploying on a VPS

One small Linux box is enough: one process, long polling, SQLite on local disk. No domain, no
TLS, no reverse proxy, no database server. [`deploy/`](deploy/) holds a systemd unit, an
environment file to copy, and a nightly backup timer, all written for this layout:

```
/opt/esgbot              the code, read-only to the service
/opt/esgbot/.venv        the environment uv builds
/var/lib/esgbot          the database, created by the unit
/var/backups/esgbot      the backups
/etc/esgbot/esgbot.env   the token, root-owned and chmod 0600
```

Two rules worth knowing before the first deploy. **Only one instance may poll** — two pollers on
one token fight over updates — so never run the bot by hand while the service is up. And
**restarting voids a flip that is in progress**: the bot holds a running flip in memory, so the
next start finds the leftover row, tells the group the flip was voided and reveals its seed.
Nobody's recorded history is touched, but the round on screen is gone, so deploy during a quiet
minute. The one flip that is not voided is one whose result was being recorded as the bot was
asked to stop: that flip is finished, so stopping waits a few seconds for it to post its result
before closing the connection to Telegram.

Everything below runs as root on a Debian or Ubuntu box.

**1. A user for it to run as.** No login, no home directory of its own; systemd makes
`/var/lib/esgbot` on first start.

```sh
groupadd --system esgbot
useradd --system --gid esgbot --home-dir /var/lib/esgbot --no-create-home \
    --shell /usr/sbin/nologin esgbot
install -d -o esgbot -g esgbot -m 0700 /var/backups/esgbot
```

**2. uv, and the code.** uv is only ever used for installing; the service runs the interpreter in
`.venv` directly.

```sh
curl -fsSL https://astral.sh/uv/install.sh | sh
git clone <this repository> /opt/esgbot    # or copy the tree there
cd /opt/esgbot
uv sync --no-dev                           # no pytest, ruff or mypy on the server
```

The installer puts uv in `~/.local/bin`, which a shell opened before it ran will not have on its
`PATH`; `~/.local/bin/uv sync --no-dev` works either way. Leave `/opt/esgbot` owned by root — the
service only needs to read it, and the unit mounts the whole filesystem read-only in any case.

**3. A bot, from @BotFather.** Send `/newbot` and keep the token it gives you — it is the one
secret here, and anyone holding it is the bot. Then:

- Add the bot to the group, and give it permission to **send** and **edit** messages. Editing is
  not optional: a flip is one message edited about fifteen times.
- Leave privacy mode alone. Telegram's default keeps the bot from seeing ordinary chatter, and
  slash commands and button presses still reach it, which is all it asks for.
- `/setcommands` with the four commands from [Commands](#commands) is optional and makes them
  appear in the group's menu.

**4. The environment file.** Copy the example, then edit the token in:

```sh
install -D -m 0600 -o root -g root /opt/esgbot/deploy/esgbot.env.example /etc/esgbot/esgbot.env
editor /etc/esgbot/esgbot.env
```

Keep it 0600 and root-owned. systemd reads it as root before dropping to the `esgbot` user, so
the service's own user never needs access to the token, and `systemctl show` never prints it.

**5. The units.**

```sh
install -m 0644 /opt/esgbot/deploy/esgbot.service \
    /opt/esgbot/deploy/esgbot-backup.service \
    /opt/esgbot/deploy/esgbot-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now esgbot.service esgbot-backup.timer
```

If you put the code anywhere but `/opt/esgbot`, or the database anywhere but
`/var/lib/esgbot/esgbot.sqlite3`, change the paths in all three files — `ESGBOT_DB_PATH` in the
environment file and in `esgbot-backup.service` have to agree.

**6. Watch it come up.**

```sh
systemctl status esgbot
journalctl -u esgbot -f                 # follow
journalctl -u esgbot -n 200 --no-pager  # the last 200 lines
```

A healthy start logs the database path, the schema version, the startup sweep and `polling for
updates`. `ESGBOT_LOG_LEVEL=DEBUG` in the environment file, then `systemctl restart esgbot`, adds
every gateway call and timer.

The bot stops cleanly: on `systemctl stop` it leaves polling, closes the database and exits 0 in
well under a second. A wrong or revoked token is the opposite — it ends the process on the first
call to Telegram with `TelegramUnauthorizedError` at the foot of a traceback, and systemd retries
ten times over ten minutes before giving up, so `systemctl status esgbot` is where a bot that
never came up explains itself.

### Backups

The timer takes one copy a night at about 03:30, keeps a fortnight of them and catches up after
downtime. Backups are taken with SQLite's online backup API, which is the only safe way to copy
this database while the bot is writing to it: the file is in WAL mode, so a plain `cp` of
`esgbot.sqlite3` can hand over a file missing the newest flips, or one that will not open at all.
Each copy is checked with `PRAGMA integrity_check` before it is given its final name, so a file
called `esgbot-<timestamp>.sqlite3` is one that read back sound. Nothing beyond this project's own
virtual environment is needed — in particular not the `sqlite3` command-line tool.

```sh
systemctl list-timers esgbot-backup     # when the next one is due
systemctl start esgbot-backup.service   # one now, without waiting
journalctl -u esgbot-backup -n 50       # what the last runs did
ls -l /var/backups/esgbot
```

Three variables, set in `esgbot-backup.service`, say what it does. They are read by
`deploy/backup.sh` only, not by the bot:

| Variable | Used for | Default |
| --- | --- | --- |
| `ESGBOT_DB_PATH` | The database to copy. Must match the bot's. | `esgbot.sqlite3` |
| `ESGBOT_BACKUP_DIR` | Where copies go. | `backups` |
| `ESGBOT_BACKUP_KEEP_DAYS` | Days of copies to keep; `0` keeps every one. | `14` |

`/var/backups/esgbot` has to exist and be writable by the `esgbot` user — step 1 above creates
it. If it is missing, the journal says `cannot write backups to /var/backups/esgbot: Read-only
file system`, because the unit's sandbox leaves everything but that one directory read-only.

The copies hold finished flips, including their revealed seeds and the names players had at the
time. They never hold the token. Backing them up off the box is worth doing, and is left to
whatever you already use for that.

### Upgrading

```sh
systemctl start esgbot-backup.service   # a fresh copy first
systemctl stop esgbot                   # voids an open flip
cd /opt/esgbot && git pull              # or copy the new tree in
uv sync --no-dev
systemctl start esgbot
journalctl -u esgbot -n 50 --no-pager
```

Take the backup first because the bot migrates the database as it starts, and an older bot
refuses a file a newer one has written — the journal says `database schema version N is newer
than this version of the bot supports`. Going back a version therefore means restoring the
backup as well.

### Restoring from a backup

```sh
systemctl stop esgbot
cd /var/lib/esgbot
mv esgbot.sqlite3 esgbot.sqlite3.before-restore          # keep what was there
install -m 0600 -o esgbot -g esgbot \
    /var/backups/esgbot/esgbot-20261115T033000Z.sqlite3 esgbot.sqlite3
rm -f esgbot.sqlite3-wal esgbot.sqlite3-shm              # see below
systemctl start esgbot
journalctl -u esgbot -n 50 --no-pager
```

Deleting the leftover `-wal` and `-shm` files is the step not to skip. They belong to the
database that was just moved aside, and a leftover write-ahead log can be replayed into whatever
file has taken its place. SQLite will often notice the mismatch and discard it, but that is not
a thing to lean on with the only copy of the history, and the restored file is a complete
database on its own — there is nothing in them to lose.

Flips that were in progress are voided on the next start, as after any restart, and the groups
are told. `/verify <flip number>` is a good first thing to run afterwards: it re-deals a stored
flip from its seed and re-scores it, so a pass says the restored history still hangs together.

### The first live check

Nothing here measures what the Tech Plan calls the one real unknown: **Telegram's edit limits
are not documented**. A flip spends about fifteen edits and three sends over roughly a minute,
which is believed to fit, but the way to know is to play a few flips in a private test group
and watch for edits arriving late or not at all — `journalctl -u esgbot -f` logs every
flood-control wait the gateway is told to take. Do that before letting a busy group at it.

## Known limits

These are edge cases found in review and accepted rather than fixed. None can change who owes
what: a flip that is recorded stays recorded and `/verify <number>` can always check it.

- **A cancelled or idle-timed-out lobby can hold its group.** If Telegram makes the final
  "cancelled" or "timed out" edit wait a long flood-control delay, the group stays registered
  to that lobby, and `/flip` there is refused, until the edit goes through.
- **A stuck database cleanup can hold a group.** After a result is posted the flip's saved
  row is removed. If that one database call hangs, the group stays occupied until it returns.
- **A flip that is still opening during shutdown can leave a lobby behind.** Stopping the bot
  at the very moment a `/flip` is being opened can leave a lobby message and its saved row,
  with no timer. The next start voids it like any other leftover.
- **A result can go unannounced after a slow stop.** On shutdown the bot waits up to 5 seconds
  for a result that is being recorded and sent. If sending takes longer, the result is not
  posted. The flip is still recorded and its showdown message already shows every hand.
- **A hung database write can delay the process exit.** Closing the database waits for any
  write in progress, so a write that hangs holds the exit up to SQLite's own busy timeout
  (about 5 seconds). The unit's `TimeoutStopSec=30` leaves room for that.

## Layout

```
esgbot/
  game/          cards, fairness, deal, rules, evaluator, scoring, settlement
                                                   (pure logic, no Telegram or SQLite)
  engine/        one flip's state machine, its timers, one flip per group
  telegram/      commands, rendering, buttons, gateway, startup sweep
  storage/       SQLite schema, finished flips, flips in progress, stats queries
  verify.py      re-running a recorded flip against its seed, for /verify
  backup.py      an online copy of the database, for the backup timer
  tasks.py       awaiting work that cannot be called back once it has begun
  logging_setup.py
  __main__.py    reads the environment and runs the bot
deploy/
  esgbot.service          the systemd unit: one instance, hardened, journald
  esgbot.env.example      the environment file to copy to /etc/esgbot/esgbot.env
  backup.sh               one backup, run by hand or by the timer
  esgbot-backup.service   what the timer starts
  esgbot-backup.timer     nightly, and catching up after downtime
docs/
  fairness-spec.md    the deal procedure, byte for byte, with test vectors
tests/
```

What is in `esgbot/game`:

| Module | Holds |
| --- | --- |
| `cards` | `Card`, `Rank`, `Suit`, the stable 0..51 card index, `FULL_DECK`, card text |
| `fairness` | `generate_seed`, `commitment`, `deck_order`, `button_seat`, seed hex helpers |
| `deal` | `deal_cards`, the `Deal` and `PlayerHand` records, `seat_order`, JSON round-trip |
| `rules` | `Settings`, the four settings' limits, `parse_settings`, `max_players` |
| `evaluator` | Five-card hand ranking and each player's best hand on a board |
| `scoring` | A point per board, split among tied best hands, and the point totals |
| `settlement` | The pot, the scoop rule, odd dollars in a chop, and who owes whom |

And in `esgbot/telegram`:

| Module | Holds |
| --- | --- |
| `settings` | The token and database path, read from the environment. The token is never logged. |
| `render` | Every word the bot says. View data in, escaped HTML out, and nothing else. |
| `keyboards` | The buttons, and the flip token their callback data carries |
| `gateway` | The engine's output for real: flood-control waits, deleted messages, a group that has become a supergroup, giving up |
| `handlers` | `/flip`, `/leaderboard`, `/stats`, `/verify`, button presses, and the group-to-supergroup migration |
| `sweep` | Voiding the flips that were open when the bot last stopped |
| `app` | Wiring it all together and long-polling |

## Settings and their limits

| Setting | Meaning | Limits |
| --- | --- | --- |
| wager | Whole dollars each player puts in | 1 to 1,000,000 |
| `X` | Hole cards dealt to each player | 5 to 21 |
| `Y` | Maximum extra cards per player after both boards | 0 to 21 |
| `Z` | Scoop multiplier (`1` means no bonus) | 1 to 100 |

Settings are plain whole numbers written in digits only: `$50`, `5.0`, `1,000` and `-3` are
rejected rather than guessed at. Two boards take ten of the 52 cards, which leaves 42 for the
players, so at most `floor(42 / X)` players fit — eight at `X = 5`.

## Fairness

Every card in a flip comes from a 32-byte seed. The bot posts the SHA-256 of that seed before
any card is shown and reveals the seed with the result, so anyone can recompute the whole deal
and check it was not changed afterwards. The exact procedure, down to the bytes that are hashed,
plus test vectors the test suite checks itself against, is in
[`docs/fairness-spec.md`](docs/fairness-spec.md). That document also states plainly what the
scheme does **not** prove.

`/verify 42` does that recomputation for you. It takes the stored seed, hashes it and compares
the result with the commitment the group was shown, deals the seed again and compares the deal
card for card, then re-scores and re-settles it and compares every player's points, net dollars
and scoop flag. A pass means the stored flip follows from the committed seed; a fail names the
part that does not, which is what a flip changed in the database afterwards looks like. It is
the bot checking its own records, so independent proof still means running the published spec
against the revealed seed yourself.

"""Every delay in a flip, in one place.

The pacing is a product requirement, not a tuning knob: the boards come out two
seconds apart, the showdown steps three, and the peek window is thirty seconds
or ten after everyone has looked. The Telegram layer renders those last two
numbers into the table message, so it reads them from here rather than writing
them out again.

Seconds throughout, as floats, because that is what :class:`~esgbot.engine.clock.Clock`
speaks.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BOARD_GAP_SECONDS",
    "FLIP_WATCHDOG_SECONDS",
    "LOBBY_IDLE_SECONDS",
    "LOBBY_MAX_AGE_SECONDS",
    "PEEK_ALL_SECONDS",
    "PEEK_EDIT_INTERVAL_SECONDS",
    "PEEK_WINDOW_SECONDS",
    "SHOWDOWN_STEP_SECONDS",
    "SHUTDOWN_TELLING_SECONDS",
    "TELLING_SECONDS",
]

BOARD_GAP_SECONDS: Final = 2.0
"""Between Board 1 and Board 2. The only gap in the deal."""

PEEK_WINDOW_SECONDS: Final = 30.0
"""From the last card dealt to the showdown, however little anyone peeks."""

PEEK_ALL_SECONDS: Final = 10.0
"""From the last needed peek to the showdown, never past :data:`PEEK_WINDOW_SECONDS`."""

PEEK_EDIT_INTERVAL_SECONDS: Final = 5.0
"""The least time between two table edits that only update the peek count."""

SHOWDOWN_STEP_SECONDS: Final = 3.0
"""Between showdown steps: the hands, each board's winner, then the points."""

LOBBY_IDLE_SECONDS: Final = 600.0
"""A lobby cancels itself after ten minutes with no join or leave."""

LOBBY_MAX_AGE_SECONDS: Final = 1800.0
"""And in any case thirty minutes after it opened, however busy it has been."""

FLIP_WATCHDOG_SECONDS: Final = 1800.0
"""A started flip that has not finished thirty minutes after Start is voided."""

TELLING_SECONDS: Final = 120.0
"""How long a flip keeps trying to say how it ended before it gives up.

Its own deadline, and the only thing that stops that one last message, so a
group is never held by a flip Telegram will not let finish -- a flood-control
wait it honours however long it is, say. Two minutes is many times what the
gateway's own three attempts take and far longer than any ordinary wait, so a
flip that reaches this has lost its group's attention anyway; the flip is still
recorded and ``/verify`` can still answer for it.
"""

SHUTDOWN_TELLING_SECONDS: Final = 5.0
"""How long shutting the bot down waits for those last messages, in real time.

Not on the flip's clock: what is being waited for is a database write in a
worker thread and a send on the wire, and a stop that cannot finish is worse
than a result that arrives nowhere. Long enough for a write and a send that are
merely slow, short enough that systemd never has to kill the process.
"""

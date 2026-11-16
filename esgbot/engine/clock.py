"""Time, behind one small interface, so tests can run a flip in milliseconds.

A flip is mostly waiting: two seconds between the boards, thirty for the peek
window, three between showdown steps, ten or thirty minutes for the timers. The
engine never calls :func:`asyncio.sleep` or reads the clock directly; it asks a
:class:`Clock` instead, so a test can hand it a fake one, advance time in jumps
and watch the whole flip play out instantly.

Two readings of time, for two jobs:

* :meth:`Clock.monotonic` and :meth:`Clock.sleep` drive every deadline in the
  engine. They are monotonic, so a system clock change cannot shorten a peek
  window or stall a watchdog.
* :meth:`Clock.now` is wall-clock UTC, and is only used for the timestamps that
  go into storage, which requires timezone-aware datetimes.

:class:`SystemClock` is the real thing and is what the bot runs with.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol, final, runtime_checkable

__all__ = ["Clock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """What the engine needs from time. :class:`SystemClock` is the real one."""

    def now(self) -> datetime:
        """The current wall-clock time, timezone-aware, for stored timestamps."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed point, never going backwards."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait this many seconds. Zero or less returns promptly."""
        ...


@final
class SystemClock:
    """The real clock: UTC wall time, :func:`time.monotonic` and :func:`asyncio.sleep`."""

    __slots__ = ()

    def now(self) -> datetime:
        """The current time in UTC."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """:func:`time.monotonic`."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """:func:`asyncio.sleep`, which returns immediately for zero or less."""
        await asyncio.sleep(seconds)

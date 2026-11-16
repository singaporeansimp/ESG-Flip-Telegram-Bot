"""Awaiting work that cannot be called back once it has begun.

Two things the bot starts cannot be stopped by cancelling the await on them. A
database call runs in a worker thread, and :func:`asyncio.to_thread` has no way
to interrupt a thread that is already in sqlite; and the one storage write that
decides a flip's result either commits or does not, whatever the caller does
next. Letting go of either at the moment it is cancelled is what produces the
two failures this module exists to prevent: a second worker entering the same
connection while the first is still using it, and a watchdog announcing a flip
voided that storage has just recorded.

:func:`see_through` is how both are awaited. It is a plain ``await`` until the
caller is cancelled, and then it keeps waiting until the work is finished before
it passes the cancellation on, so by the time the caller unwinds, whatever the
work was going to do has already happened and can be reasoned about.
"""

from __future__ import annotations

import asyncio

__all__ = ["see_through"]


async def see_through[T](work: asyncio.Future[T]) -> T:
    """Await ``work`` to completion, cancellation included, and return its result.

    Being cancelled does not cancel ``work``: the wait resumes and the
    :exc:`asyncio.CancelledError` is raised only once ``work`` is done, so a
    caller that is unwinding has still seen it through. Cancellation wins over
    the result, so a cancelled caller is never told the work succeeded; what the
    work did is then read from wherever it recorded it.

    Raises:
        asyncio.CancelledError: if this wait was cancelled, after ``work`` has
            finished.
        BaseException: whatever ``work`` raised, if this wait was not cancelled.
    """
    cancelled: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as stop:
            cancelled = stop
        except BaseException:
            # The work's own failure. It is done, so the loop ends here and
            # ``work.result()`` raises it below -- unless we were cancelled too,
            # in which case the cancellation is the answer.
            break
    if cancelled is not None:
        raise cancelled
    return work.result()

"""``see_through``: awaiting work to completion, whatever happens to the waiter."""

from __future__ import annotations

import asyncio

import pytest

from esgbot.tasks import see_through


def test_see_through_returns_the_works_result() -> None:
    async def work() -> str:
        await asyncio.sleep(0)
        return "done"

    async def scenario() -> None:
        assert await see_through(asyncio.ensure_future(work())) == "done"

    asyncio.run(scenario())


def test_see_through_propagates_the_works_own_exception() -> None:
    async def work() -> None:
        await asyncio.sleep(0)
        raise KeyError("the work failed")

    async def scenario() -> None:
        with pytest.raises(KeyError, match="the work failed"):
            await see_through(asyncio.ensure_future(work()))

    asyncio.run(scenario())


def test_a_cancelled_wait_lets_the_work_finish_before_it_raises() -> None:
    log: list[str] = []

    async def work() -> str:
        log.append("started")
        await asyncio.sleep(0.05)
        log.append("finished")
        return "done"

    async def scenario() -> None:
        future = asyncio.ensure_future(work())
        waiter = asyncio.ensure_future(see_through(future))
        await asyncio.sleep(0.01)
        assert log == ["started"]

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # By the time the cancellation arrived the work had run to the end, and
        # it was not cancelled on the way.
        assert log == ["started", "finished"]
        assert future.done()
        assert not future.cancelled()
        assert future.result() == "done"

    asyncio.run(scenario())


def test_a_cancelled_wait_does_not_finish_while_the_work_is_still_running() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def work() -> None:
            await release.wait()

        waiter = asyncio.ensure_future(see_through(asyncio.ensure_future(work())))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0.02)
        assert not waiter.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    asyncio.run(scenario())


def test_cancellation_wins_over_a_successful_result() -> None:
    async def work() -> str:
        await asyncio.sleep(0.02)
        return "never seen"

    async def scenario() -> None:
        future = asyncio.ensure_future(work())
        waiter = asyncio.ensure_future(see_through(future))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert future.result() == "never seen"

    asyncio.run(scenario())


def test_cancellation_wins_over_the_works_own_exception() -> None:
    async def work() -> None:
        await asyncio.sleep(0.02)
        raise KeyError("the work failed")

    async def scenario() -> None:
        future = asyncio.ensure_future(work())
        waiter = asyncio.ensure_future(see_through(future))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with pytest.raises(KeyError):
            future.result()

    asyncio.run(scenario())

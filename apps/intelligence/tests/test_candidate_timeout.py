"""The per-candidate deadline ignores time spent queued for shared capacity."""

import asyncio

import pytest
from auralisy_intelligence.agents.progressive import run_with_active_timeout


def test_queue_wait_extends_the_candidate_deadline():
    async def scenario():
        async def work():
            await asyncio.sleep(0.15)
            return "done"

        # 0.1 s active budget, but 0.1 s of the run was spent queued: the work finishes.
        result = await run_with_active_timeout(work(), 0.1, lambda: 0.1, 1)
        assert result == "done"

    asyncio.run(scenario())


def test_active_time_over_the_budget_still_times_out_and_cancels():
    async def scenario():
        cancelled = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(asyncio.TimeoutError):
            await run_with_active_timeout(work(), 0.05, lambda: 0.0, 1)
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_queue_credit_is_capped():
    async def scenario():
        async def work():
            await asyncio.sleep(1)

        with pytest.raises(asyncio.TimeoutError):
            await run_with_active_timeout(work(), 0.05, lambda: 100.0, 0.05)

    asyncio.run(scenario())


def test_outer_cancellation_cancels_the_work():
    async def scenario():
        cancelled = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        outer = asyncio.create_task(run_with_active_timeout(work(), 1, lambda: 0.0, 1))
        await asyncio.sleep(0.01)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert cancelled.is_set()

    asyncio.run(scenario())

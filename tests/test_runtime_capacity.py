from __future__ import annotations

import asyncio

import pytest

from app.core.engine import run_capacity


@pytest.mark.asyncio
async def test_bounded_agent_run_limits_parallel_work(monkeypatch):
    monkeypatch.setattr(run_capacity, "_slots", asyncio.BoundedSemaphore(2))

    active = 0
    max_active = 0
    state_lock = asyncio.Lock()

    @run_capacity.bounded_agent_run
    async def work(value: int) -> int:
        nonlocal active, max_active
        async with state_lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        async with state_lock:
            active -= 1
        return value

    assert await asyncio.gather(*(work(value) for value in range(8))) == list(range(8))
    assert max_active == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_capacity(monkeypatch):
    slots = asyncio.BoundedSemaphore(1)
    monkeypatch.setattr(run_capacity, "_slots", slots)

    await slots.acquire()
    waiter = asyncio.create_task(run_capacity.agent_run_slot().__aenter__())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    slots.release()
    async with asyncio.timeout(0.2):
        async with run_capacity.agent_run_slot():
            pass


def test_production_capacity_defaults_cover_database_pool():
    settings = run_capacity.get_settings()
    assert settings.max_concurrent_agent_runs >= 1
    assert settings.db_pool_size >= 1
    assert settings.db_pool_size + settings.db_max_overflow >= settings.max_concurrent_agent_runs


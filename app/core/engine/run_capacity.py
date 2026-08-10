"""Process-local backpressure for expensive agent runs.

The API intentionally stays on one Uvicorn worker while session locks and the
active-task registry are process-local. This limiter lets different users run
concurrently, but prevents a burst from starting an unbounded number of LLM,
database, and sandbox workloads at once.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from app.config import get_settings
from app.core.utils.metrics import agent_run_queue_wait, agent_runs_active, agent_runs_queued

_settings = get_settings()
_capacity = max(1, int(_settings.max_concurrent_agent_runs))
_slots = asyncio.BoundedSemaphore(_capacity)

T = TypeVar("T")


@asynccontextmanager
async def agent_run_slot() -> AsyncIterator[None]:
    """Wait fairly for a bounded run slot and always release it on cancellation."""
    queued_at = time.monotonic()
    acquired = False
    agent_runs_queued.inc()
    try:
        await _slots.acquire()
        acquired = True
    finally:
        agent_runs_queued.dec()
        agent_run_queue_wait.observe(max(0.0, time.monotonic() - queued_at))

    agent_runs_active.inc()
    try:
        yield
    finally:
        agent_runs_active.dec()
        if acquired:
            _slots.release()


def bounded_agent_run(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Decorator used by the public runner so every entry point gets backpressure."""

    @wraps(func)
    async def wrapped(*args: Any, **kwargs: Any) -> T:
        async with agent_run_slot():
            return await func(*args, **kwargs)

    return wrapped

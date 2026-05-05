"""
session_lock.py — Per-session asyncio lock to prevent concurrent runs.

Concurrent POST on same session_id → two parallel runs on same history
baseline → corrupted message log + doubled reply.

Usage:
    async with session_run_lock(session_id):
        result = await run_agent(...)

The second request on the same session_id will wait until the first
completes.  Different sessions run fully in parallel.

Memory: locks are evicted after 5 minutes of inactivity to prevent unbounded growth.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# session_id -> (Lock, last_used_timestamp)
_locks: dict[UUID, tuple[asyncio.Lock, float]] = {}
_manager_lock = asyncio.Lock()  # protects _locks dict itself

# Evict locks not used for this many seconds
_EVICTION_TTL = 300  # 5 minutes


async def _get_lock(session_id: UUID) -> asyncio.Lock:
    """Get or create a lock for a session, updating last-used timestamp."""
    async with _manager_lock:
        entry = _locks.get(session_id)
        if entry:
            lock, _ = entry
            _locks[session_id] = (lock, time.monotonic())
            return lock
        lock = asyncio.Lock()
        _locks[session_id] = (lock, time.monotonic())
        return lock


async def _maybe_evict() -> None:
    """Remove stale locks that haven't been used recently."""
    now = time.monotonic()
    async with _manager_lock:
        stale = [
            sid for sid, (lock, ts) in _locks.items()
            if now - ts > _EVICTION_TTL and not lock.locked()
        ]
        for sid in stale:
            del _locks[sid]
        if stale:
            logger.debug("session_lock.evicted", count=len(stale))


@asynccontextmanager
async def session_run_lock(session_id: UUID) -> AsyncIterator[None]:
    """Acquire per-session lock. Concurrent callers on same session wait in queue.

    Raises asyncio.TimeoutError if lock not acquired within 600 seconds
    (prevents infinite queue buildup from buggy callers).
    """
    from app.config import get_settings
    _lock_timeout = get_settings().agent_timeout_seconds + 60

    lock = await _get_lock(session_id)

    if lock.locked():
        logger.info(
            "session_lock.waiting",
            session_id=str(session_id),
        )

    try:
        async with asyncio.timeout(_lock_timeout):
            async with lock:
                yield
    finally:
        # Opportunistic eviction — don't block on it
        asyncio.create_task(_maybe_evict())

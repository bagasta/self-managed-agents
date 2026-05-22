"""
session_lock.py — Per-session asyncio lock + active-task registry.

Lock: prevents concurrent runs on the same session (corrupts message history).
Task registry: allows the current run to be cancelled when the user sends a new
message mid-run (human interrupt).

Usage — normal run:
    async with session_run_lock(session_id):
        result = await run_agent(...)

Usage — interrupt-aware (messages.py):
    await cancel_active_run(session_id)          # cancel previous, if any
    await register_active_task(session_id, asyncio.current_task())
    try:
        async with session_run_lock(session_id):
            result = await run_agent(...)
    finally:
        await unregister_active_task(session_id)
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# ------------------------------------------------------------------ #
# Per-session asyncio.Lock (prevents concurrent runs)                 #
# ------------------------------------------------------------------ #
# Each entry: (lock, last_access_ts, acquired_at_ts | None)
# acquired_at_ts is set when the lock is acquired and cleared on release.
# Used to detect and force-evict stuck locks.
_locks: dict[UUID, tuple[asyncio.Lock, float, float | None]] = {}
_manager_lock = asyncio.Lock()
_EVICTION_TTL = 300  # seconds — evict idle (unlocked) entries


async def _get_lock(session_id: UUID) -> asyncio.Lock:
    async with _manager_lock:
        entry = _locks.get(session_id)
        if entry:
            lock, _, acquired_at = entry
            _locks[session_id] = (lock, time.monotonic(), acquired_at)
            return lock
        lock = asyncio.Lock()
        _locks[session_id] = (lock, time.monotonic(), None)
        return lock


async def _set_acquired(session_id: UUID, ts: float | None) -> None:
    """Record or clear the lock acquisition timestamp."""
    async with _manager_lock:
        entry = _locks.get(session_id)
        if entry:
            lock, last_ts, _ = entry
            _locks[session_id] = (lock, last_ts, ts)


async def force_release_session_lock(session_id: UUID) -> None:
    """Replace the lock entry so a new user message can proceed after interrupt.

    The old asyncio.Lock object may still be held by a cancellation-stuck task.
    Replacing the registry entry lets the next request acquire a fresh lock while
    the old task unwinds and releases its stale lock object harmlessly.
    """
    async with _manager_lock:
        new_lock = asyncio.Lock()
        _locks[session_id] = (new_lock, time.monotonic(), None)
    logger.warning("session_lock.force_released", session_id=str(session_id))


async def _maybe_evict() -> None:
    now = time.monotonic()
    async with _manager_lock:
        stale = [
            sid for sid, (lock, ts, _) in _locks.items()
            if now - ts > _EVICTION_TTL and not lock.locked()
        ]
        for sid in stale:
            del _locks[sid]
        if stale:
            logger.debug("session_lock.evicted", count=len(stale))


async def is_session_busy(session_id: UUID) -> bool:
    async with _manager_lock:
        entry = _locks.get(session_id)
        if entry:
            lock, _, _ = entry
            return lock.locked()
    return False


@asynccontextmanager
async def session_run_lock(session_id: UUID) -> AsyncIterator[None]:
    """Acquire per-session lock. Waits if another run is active.

    Lock timeout is set to cover the maximum possible run duration
    (3× base timeout for builder/subagent agents) plus a generous
    cancellation-propagation buffer.  If the lock has been held longer
    than the max run time, it is force-evicted so a stuck previous run
    cannot block the session indefinitely.
    """
    from app.config import get_settings
    settings = get_settings()
    # Maximum time we wait to acquire the lock.
    # Builder/system agents use 8x multiplier — lock must cover that ceiling.
    _lock_timeout = settings.agent_timeout_seconds * 8 + 120  # e.g. 2520s

    # Maximum time a single run is allowed to hold the lock before we
    # force-evict it (prevents permanently stuck sessions).
    _max_lock_age = settings.agent_timeout_seconds * 8 + 60   # e.g. 2460s

    lock = await _get_lock(session_id)

    if lock.locked():
        logger.info("session_lock.waiting", session_id=str(session_id))

        # Check if the lock is stale (held beyond max run time).
        # If so, force-create a new lock so this request doesn't block forever.
        # The old task may still be running; it will release its stale lock
        # object harmlessly once it finishes.
        async with _manager_lock:
            entry = _locks.get(session_id)
            if entry:
                _, _, acquired_at = entry
                if acquired_at is not None and time.monotonic() - acquired_at > _max_lock_age:
                    logger.warning(
                        "session_lock.force_evict_stuck",
                        session_id=str(session_id),
                        held_seconds=round(time.monotonic() - acquired_at),
                    )
                    new_lock = asyncio.Lock()
                    _locks[session_id] = (new_lock, time.monotonic(), None)
                    lock = new_lock

    try:
        async with asyncio.timeout(_lock_timeout):
            async with lock:
                await _set_acquired(session_id, time.monotonic())
                try:
                    yield
                finally:
                    await _set_acquired(session_id, None)
    finally:
        asyncio.create_task(_maybe_evict())


# ------------------------------------------------------------------ #
# Active-task registry (enables human interrupt / cancellation)       #
# ------------------------------------------------------------------ #
_active_tasks: dict[UUID, asyncio.Task] = {}
_task_registry_lock = asyncio.Lock()


async def register_active_task(session_id: UUID, task: asyncio.Task) -> None:
    """Register the asyncio.Task currently handling a session run."""
    async with _task_registry_lock:
        _active_tasks[session_id] = task


async def unregister_active_task(session_id: UUID, task: asyncio.Task | None = None) -> None:
    """Remove the task entry when a run finishes."""
    async with _task_registry_lock:
        if task is None or _active_tasks.get(session_id) is task:
            _active_tasks.pop(session_id, None)


async def cancel_active_run(session_id: UUID) -> bool:
    """Cancel the running task for this session, if any.

    Waits briefly for the cancelled task to clean up (close sandboxes,
    release the session lock). If it does not stop quickly, force-release the
    session lock so the newest user message can be handled. Returns True
    if a task was found and cancellation was requested.
    """
    async with _task_registry_lock:
        task = _active_tasks.get(session_id)

    if task is None or task.done():
        return False

    logger.info("session_lock.cancelling_active_run", session_id=str(session_id))
    task.cancel()

    # Keep WhatsApp responsive: acknowledge the new message quickly. If an
    # in-flight HTTP/tool call does not unwind promptly, release the session
    # lock and let the cancelled task finish cleanup in the background.
    cancel_grace_seconds = 1.5
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=cancel_grace_seconds,
        )
    except asyncio.TimeoutError:
        await force_release_session_lock(session_id)
    except asyncio.CancelledError:
        pass

    return True

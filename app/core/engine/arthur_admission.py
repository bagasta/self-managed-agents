"""Bound concurrent Arthur builder runs without affecting managed agents."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import structlog

from app.config import get_settings
from app.models.agent import Agent

logger = structlog.get_logger(__name__)


class ArthurQueueFull(RuntimeError):
    """Raised when the bounded Arthur wait queue has reached capacity."""


@dataclass(frozen=True)
class ArthurAdmission:
    queued: bool
    wait_seconds: float


_state_lock = asyncio.Lock()
_semaphore: asyncio.Semaphore | None = None
_configured_limit = 0
_active_runs = 0
_waiting_runs = 0


def is_arthur_builder(agent: Agent) -> bool:
    """Identify Arthur by platform role, not by a mutable display name."""
    capabilities = set(getattr(agent, "capabilities", None) or [])
    tools_config = getattr(agent, "tools_config", None) or {}
    return bool(
        "builder" in capabilities
        or "system" in capabilities
        or (isinstance(tools_config, dict) and tools_config.get("builder"))
    )


def is_arthur_capacity_saturated(agent: Agent) -> bool:
    """Cheap hint used only to decide whether an immediate queue reply helps."""
    if not is_arthur_builder(agent):
        return False
    limit = max(1, int(get_settings().arthur_max_concurrent_runs))
    return _active_runs >= limit or bool(_semaphore and _semaphore.locked())


def is_heavy_arthur_request(message: str) -> bool:
    """Recognize requests that normally require a multi-step builder workflow."""
    normalized = " ".join((message or "").casefold().split())
    agent_terms = ("agent", "asisten", "assistant")
    build_terms = (
        "buat",
        "bikin",
        "create",
        "tambahkan",
        "tambah",
        "ubah agent",
        "update agent",
        "perbaiki agent",
        "setup",
    )
    return any(term in normalized for term in agent_terms) and any(
        term in normalized for term in build_terms
    )


async def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore, _configured_limit

    limit = max(1, int(get_settings().arthur_max_concurrent_runs))
    async with _state_lock:
        if _semaphore is None:
            _semaphore = asyncio.Semaphore(limit)
            _configured_limit = limit
        elif _configured_limit != limit and _active_runs == 0 and _waiting_runs == 0:
            _semaphore = asyncio.Semaphore(limit)
            _configured_limit = limit
        return _semaphore


@asynccontextmanager
async def arthur_run_slot(agent: Agent) -> AsyncIterator[ArthurAdmission]:
    """Limit only expensive builder runs and bound how many may wait."""
    global _active_runs, _waiting_runs

    if not is_arthur_builder(agent):
        yield ArthurAdmission(queued=False, wait_seconds=0.0)
        return

    semaphore = await _get_semaphore()
    max_queue = max(0, int(get_settings().arthur_max_queued_runs))
    queued = semaphore.locked()

    async with _state_lock:
        if queued and _waiting_runs >= max_queue:
            raise ArthurQueueFull("Arthur queue capacity reached")
        _waiting_runs += 1

    started_waiting = time.monotonic()
    try:
        await semaphore.acquire()
    finally:
        # No await here: cancellation after acquiring a permit must not leak it
        # before the context manager reaches its release block.
        _waiting_runs = max(0, _waiting_runs - 1)

    wait_seconds = time.monotonic() - started_waiting
    _active_runs += 1
    active = _active_runs
    waiting = _waiting_runs
    logger.info(
        "arthur_admission.acquired",
        active=active,
        waiting=waiting,
        wait_seconds=round(wait_seconds, 3),
    )

    try:
        yield ArthurAdmission(queued=queued, wait_seconds=wait_seconds)
    finally:
        _active_runs = max(0, _active_runs - 1)
        active = _active_runs
        waiting = _waiting_runs
        semaphore.release()
        logger.info("arthur_admission.released", active=active, waiting=waiting)


async def reset_arthur_admission_for_tests() -> None:
    """Reset module state between concurrency tests."""
    global _semaphore, _configured_limit, _active_runs, _waiting_runs
    async with _state_lock:
        if _active_runs or _waiting_runs:
            raise RuntimeError("Cannot reset Arthur admission while runs are active")
        _semaphore = None
        _configured_limit = 0
        _active_runs = 0
        _waiting_runs = 0

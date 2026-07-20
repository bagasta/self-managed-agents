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
    lane: str = "legacy"


@dataclass
class _LaneState:
    semaphore: asyncio.Semaphore
    configured_limit: int
    active_runs: int = 0
    waiting_runs: int = 0


_state_lock = asyncio.Lock()
_lane_states: dict[str, _LaneState] = {}


def is_arthur_builder(agent: Agent) -> bool:
    """Identify Arthur by platform role, not by a mutable display name."""
    capabilities = set(getattr(agent, "capabilities", None) or [])
    tools_config = getattr(agent, "tools_config", None) or {}
    return bool(
        "builder" in capabilities
        or "system" in capabilities
        or (isinstance(tools_config, dict) and tools_config.get("builder"))
    )


def _request_lane(message: str | None) -> str:
    if message is None:
        return "legacy"
    return "builder" if is_heavy_arthur_request(message) else "fast"


def _lane_limit(lane: str) -> int:
    settings = get_settings()
    if lane == "builder":
        return max(1, int(settings.arthur_max_concurrent_builder_runs))
    if lane == "fast":
        return max(1, int(settings.arthur_max_concurrent_fast_runs))
    return max(1, int(settings.arthur_max_concurrent_runs))


def is_arthur_capacity_saturated(agent: Agent, message: str | None = None) -> bool:
    """Cheap hint used only to decide whether an immediate queue reply helps."""
    if not is_arthur_builder(agent):
        return False
    lane = _request_lane(message)
    state = _lane_states.get(lane)
    limit = _lane_limit(lane)
    return bool(state and (state.active_runs >= limit or state.semaphore.locked()))


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


async def _get_lane_state(lane: str) -> _LaneState:
    limit = _lane_limit(lane)
    async with _state_lock:
        state = _lane_states.get(lane)
        if state is None:
            state = _LaneState(asyncio.Semaphore(limit), limit)
            _lane_states[lane] = state
        elif (
            state.configured_limit != limit
            and state.active_runs == 0
            and state.waiting_runs == 0
        ):
            state = _LaneState(asyncio.Semaphore(limit), limit)
            _lane_states[lane] = state
        return state


@asynccontextmanager
async def arthur_run_slot(
    agent: Agent,
    message: str | None = None,
) -> AsyncIterator[ArthurAdmission]:
    """Limit only expensive builder runs and bound how many may wait."""
    if not is_arthur_builder(agent):
        yield ArthurAdmission(queued=False, wait_seconds=0.0, lane="managed_agent")
        return

    lane = _request_lane(message)
    state = await _get_lane_state(lane)
    semaphore = state.semaphore
    max_queue = max(0, int(get_settings().arthur_max_queued_runs))
    queued = semaphore.locked()

    async with _state_lock:
        if queued and state.waiting_runs >= max_queue:
            raise ArthurQueueFull("Arthur queue capacity reached")
        state.waiting_runs += 1

    started_waiting = time.monotonic()
    try:
        await semaphore.acquire()
    finally:
        # No await here: cancellation after acquiring a permit must not leak it
        # before the context manager reaches its release block.
        state.waiting_runs = max(0, state.waiting_runs - 1)

    wait_seconds = time.monotonic() - started_waiting
    state.active_runs += 1
    active = state.active_runs
    waiting = state.waiting_runs
    logger.info(
        "arthur_admission.acquired",
        lane=lane,
        active=active,
        waiting=waiting,
        wait_seconds=round(wait_seconds, 3),
    )

    try:
        yield ArthurAdmission(queued=queued, wait_seconds=wait_seconds, lane=lane)
    finally:
        state.active_runs = max(0, state.active_runs - 1)
        active = state.active_runs
        waiting = state.waiting_runs
        semaphore.release()
        logger.info("arthur_admission.released", lane=lane, active=active, waiting=waiting)


async def reset_arthur_admission_for_tests() -> None:
    """Reset module state between concurrency tests."""
    async with _state_lock:
        if any(state.active_runs or state.waiting_runs for state in _lane_states.values()):
            raise RuntimeError("Cannot reset Arthur admission while runs are active")
        _lane_states.clear()

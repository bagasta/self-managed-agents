"""
interrupt_store.py — Per-session HITL interrupt state.

When Deep Agents graph pauses on interrupt_on, we store:
  - graph: compiled graph (needed to resume)
  - checkpointer: MemorySaver (holds graph state keyed by thread_id)
  - thread_id: str (== str(session_id), used in configurable)
  - action_requests: list of pending interrupt requests from result.interrupts

Resume: pass Command(resume={...}) as input + same thread_id config.
Clear: call clear_interrupt() after resume or on /reset.
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

_store: dict[UUID, dict[str, Any]] = {}
_lock = asyncio.Lock()


async def save_interrupt(
    session_id: UUID,
    *,
    graph: Any,
    checkpointer: Any,
    thread_id: str,
    action_requests: list[dict],
) -> None:
    async with _lock:
        _store[session_id] = {
            "graph": graph,
            "checkpointer": checkpointer,
            "thread_id": thread_id,
            "action_requests": action_requests,
        }


async def get_interrupt(session_id: UUID) -> dict[str, Any] | None:
    async with _lock:
        return _store.get(session_id)


async def clear_interrupt(session_id: UUID) -> None:
    async with _lock:
        _store.pop(session_id, None)

"""
In-memory event bus — pub/sub per session menggunakan asyncio.Queue.

Scheduler (dan komponen lain) publish event ke sini.
SSE endpoint subscribe dan stream ke client.

Note: ini in-memory, tidak persist restart. Cukup untuk single-server deployment.
"""
from __future__ import annotations

import asyncio
from typing import Any

# session_id (str) → list of subscriber Queues
_subscribers: dict[str, list[asyncio.Queue]] = {}


def subscribe(session_id: str) -> asyncio.Queue:
    """Daftarkan subscriber baru untuk session_id. Return Queue-nya."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.setdefault(session_id, []).append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    """Hapus subscriber dari daftar."""
    subs = _subscribers.get(session_id, [])
    try:
        subs.remove(q)
    except ValueError:
        pass
    if not subs:
        _subscribers.pop(session_id, None)


async def publish(session_id: str, event: dict[str, Any]) -> None:
    """Kirim event ke semua subscriber session_id. Non-blocking (drop jika queue penuh)."""
    for q in list(_subscribers.get(session_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # subscriber lambat — skip, jangan block

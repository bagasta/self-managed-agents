"""
Redis-backed event bus — menggantikan in-memory event_bus.py untuk multi-process deployment.

Dipakai saat REDIS_URL di-set di environment. Fallback ke in-memory jika Redis tidak tersedia.

Cara pakai (SSE endpoint):
    async for event in subscribe_generator(session_id):
        yield event

Cara publish:
    await publish(session_id, {"type": "reminder", "text": "..."})
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

log = logging.getLogger(__name__)


from app.core.redis_client import get_redis

async def publish(session_id: str, event: dict[str, Any]) -> None:
    """Publish event ke channel Redis. Fallback ke in-memory jika Redis tidak tersedia."""
    r = await get_redis()
    if r:
        try:
            async with r:
                await r.publish(f"session:{session_id}", json.dumps(event))
            return
        except Exception as exc:
            log.warning("event_bus_redis.publish_failed: %s, falling back to in-memory", exc)

    # Fallback ke in-memory bus
    from app.core import event_bus
    await event_bus.publish(session_id, event)


async def subscribe_generator(session_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """
    AsyncGenerator untuk SSE endpoint.
    Pakai Redis pub/sub jika tersedia, fallback ke polling in-memory Queue.

    Usage:
        async for event in subscribe_generator(session_id):
            yield f"data: {json.dumps(event)}\n\n"
    """
    r = await get_redis()
    if r:
        try:
            async with r.pubsub() as pubsub:
                await pubsub.subscribe(f"session:{session_id}")
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        try:
                            yield json.loads(message["data"])
                        except json.JSONDecodeError:
                            pass
            return
        except Exception as exc:
            log.warning("event_bus_redis.subscribe_failed: %s, falling back to in-memory", exc)

    # Fallback ke in-memory bus
    from app.core import event_bus
    q = event_bus.subscribe(session_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
                yield event
            except asyncio.TimeoutError:
                # Kirim keepalive ping agar koneksi SSE tidak timeout
                yield {"type": "ping"}
    finally:
        event_bus.unsubscribe(session_id, q)

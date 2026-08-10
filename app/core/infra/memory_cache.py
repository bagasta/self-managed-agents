"""
memory_cache.py — Redis-backed cache for hot-path agent memory reads.

Design:
  - Transparent write-through cache: reads hit Redis first, fall through to DB on miss.
  - Graceful degradation: if Redis is unavailable (empty redis_url, connection error),
    all operations fall through to DB silently. No errors bubble up to callers.
  - TTLs are tuned per memory layer based on update frequency:
      active_context  → 60s  (updated every run, must be fresh)
      daily:*         → 30s  (updated frequently during active sessions)
      longterm        → 300s (updated every N runs, can tolerate staleness)
      last_turn       → 60s
      user_profile    → 600s (rarely changes)
      soul            → 900s (almost never changes)
  - Cache key format: "mem:{agent_id}:{scope_or_global}:{key}"
  - Invalidation: call invalidate_memory_cache() after every upsert_memory().

Usage:
    from app.core.infra.memory_cache import get_memory_cached, invalidate_memory_cache

    # In memory_service.py — transparent read-through:
    value = await get_memory_cached(agent_id, key, db, scope=scope)

    # After any write — invalidate:
    await invalidate_memory_cache(agent_id, key, scope=scope)
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# TTL per memory key pattern (seconds)
_TTL_MAP: dict[str, int] = {
    "active_context": 60,
    "last_turn": 60,
    "last_attachment": 60,
    "last_generated_artifact": 60,
    "longterm": 300,
    "user_profile": 600,
    "soul": 900,
    "agent_context_version": 900,
}
_TTL_DAILY = 30       # daily:* keys
_TTL_DEFAULT = 120    # everything else


def _get_ttl(key: str) -> int:
    if key.startswith("daily:"):
        return _TTL_DAILY
    return _TTL_MAP.get(key, _TTL_DEFAULT)


def _cache_key(agent_id: uuid.UUID, key: str, scope: str | None) -> str:
    """Build a namespaced Redis key for a memory entry."""
    scope_part = scope or "__global__"
    return f"mem:{agent_id}:{scope_part}:{key}"


def _conversation_summary_cache_key(session_id: uuid.UUID) -> str:
    """Redis key for the active conversation summary of a session."""
    return f"conv_summary:{session_id}:active"


async def _get_redis() -> Any | None:
    """Return a Redis client, or None if Redis is not configured/available."""
    try:
        from app.config import get_settings
        settings = get_settings()
        if not settings.redis_url:
            return None
        import redis.asyncio as aioredis  # type: ignore[import]
        # Reuse a module-level pool to avoid creating a new connection per call.
        # The pool is created lazily on first use.
        if not hasattr(_get_redis, "_pool"):
            _get_redis._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                max_connections=settings.redis_max_connections,
                socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
                socket_timeout=settings.redis_socket_timeout_seconds,
                health_check_interval=settings.redis_health_check_interval_seconds,
            )
        return aioredis.Redis(connection_pool=_get_redis._pool)
    except Exception as exc:
        logger.debug("memory_cache.redis_unavailable", error=str(exc))
        return None


async def get_memory_cached(
    agent_id: uuid.UUID,
    key: str,
    db: Any,
    scope: str | None = None,
) -> Any | None:
    """
    Read-through cache for agent memory.

    1. Try Redis (fast path).
    2. On miss or Redis unavailable, fall through to PostgreSQL.
    3. Write DB result back to Redis for future reads.

    Returns the Memory ORM object or None (same contract as get_memory()).
    """
    from app.core.domain.memory_service import get_memory

    redis = await _get_redis()
    if redis is not None:
        try:
            rk = _cache_key(agent_id, key, scope)
            cached = await redis.get(rk)
            if cached is not None:
                # Reconstruct a lightweight dict-like object for callers that
                # only read `.value_data`. Real ORM objects are returned on cache miss.
                data = json.loads(cached)
                if data is None:
                    return None
                # Return a simple namespace so callers can do `mem.value_data`
                class _CachedMemory:
                    value_data = data.get("value_data", "")
                return _CachedMemory()
        except Exception as exc:
            logger.debug("memory_cache.redis_read_failed", key=key, error=str(exc))

    # DB fallback
    mem = await get_memory(agent_id, key, db, scope=scope)

    # Write-through to Redis
    if redis is not None:
        try:
            rk = _cache_key(agent_id, key, scope)
            payload = json.dumps({"value_data": mem.value_data} if mem else None)
            await redis.setex(rk, _get_ttl(key), payload)
        except Exception as exc:
            logger.debug("memory_cache.redis_write_failed", key=key, error=str(exc))

    return mem


async def invalidate_memory_cache(
    agent_id: uuid.UUID,
    key: str,
    scope: str | None = None,
) -> None:
    """
    Invalidate a cached memory entry after a write.

    Called automatically by upsert_memory() — callers don't need to invoke this directly.
    Silently no-ops if Redis is unavailable.
    """
    redis = await _get_redis()
    if redis is None:
        return
    try:
        rk = _cache_key(agent_id, key, scope)
        await redis.delete(rk)
    except Exception as exc:
        logger.debug("memory_cache.invalidate_failed", key=key, error=str(exc))


async def get_conversation_summary_cached(
    session_id: uuid.UUID,
    db: Any,
) -> str | None:
    """
    Read-through cache for the active conversation summary.

    Returns the summary_text string or None.
    TTL: 300s (summaries are regenerated every ~10 messages, not every run).
    """
    redis = await _get_redis()
    if redis is not None:
        try:
            rk = _conversation_summary_cache_key(session_id)
            cached = await redis.get(rk)
            if cached is not None:
                data = json.loads(cached)
                return data.get("summary_text") if data else None
        except Exception as exc:
            logger.debug("memory_cache.summary_read_failed", session_id=str(session_id), error=str(exc))

    # DB fallback — import here to avoid circular imports
    from app.core.domain.conversation_memory_service import get_active_summary
    summary = await get_active_summary(session_id, db)
    summary_text = summary.summary_text if summary else None

    if redis is not None:
        try:
            rk = _conversation_summary_cache_key(session_id)
            payload = json.dumps({"summary_text": summary_text})
            await redis.setex(rk, 300, payload)
        except Exception as exc:
            logger.debug("memory_cache.summary_write_failed", session_id=str(session_id), error=str(exc))

    return summary_text


async def invalidate_conversation_summary_cache(session_id: uuid.UUID) -> None:
    """Invalidate cached summary after a new one is generated."""
    redis = await _get_redis()
    if redis is None:
        return
    try:
        rk = _conversation_summary_cache_key(session_id)
        await redis.delete(rk)
    except Exception as exc:
        logger.debug("memory_cache.summary_invalidate_failed", session_id=str(session_id), error=str(exc))

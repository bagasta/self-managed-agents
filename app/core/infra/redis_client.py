"""
Redis client for shared caching, rate limiting, and deduplication.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

from app.config import get_settings

log = logging.getLogger(__name__)

_redis_pool: Optional[aioredis.Redis] = None
_redis_init_lock = asyncio.Lock()


async def get_redis() -> Optional[aioredis.Redis]:
    """
    Dapatkan global Redis client instance.
    Menggunakan Connection Pool internal dari aioredis.
    Returns None jika redis_url tidak dikonfigurasi / tidak terinstall.
    """
    global _redis_pool
    if _redis_pool is not None:
        return _redis_pool

    if aioredis is None:
        return None

    settings = get_settings()
    redis_url = getattr(settings, "redis_url", "")
    if not redis_url:
        return None

    async with _redis_init_lock:
        if _redis_pool is not None:
            return _redis_pool

        candidate = None
        try:
            pool = aioredis.BlockingConnectionPool.from_url(
                redis_url,
                max_connections=max(1, settings.redis_max_connections),
                timeout=max(0.1, settings.redis_pool_timeout_seconds),
                socket_connect_timeout=max(0.1, settings.redis_socket_connect_timeout_seconds),
                socket_timeout=max(0.1, settings.redis_socket_timeout_seconds),
                health_check_interval=max(0, settings.redis_health_check_interval_seconds),
                decode_responses=True,
            )
            candidate = aioredis.Redis(connection_pool=pool)
            await candidate.ping()
            _redis_pool = candidate
            log.info(
                "redis_client: connected with bounded pool (max_connections=%s)",
                settings.redis_max_connections,
            )
            return _redis_pool
        except Exception as exc:
            log.warning("redis_client: Warning, Redis connection failed. %s", exc)
            if candidate is not None:
                await candidate.aclose(close_connection_pool=True)
            _redis_pool = None
            return None


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose(close_connection_pool=True)
        _redis_pool = None

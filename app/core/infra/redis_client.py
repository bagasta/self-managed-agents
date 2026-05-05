"""
Redis client for shared caching, rate limiting, and deduplication.
"""
from __future__ import annotations

import logging
from typing import Optional

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

from app.config import get_settings

log = logging.getLogger(__name__)

_redis_pool: Optional[aioredis.Redis] = None


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

    try:
        # aioredis.from_url menggunakan default connection pool.
        _redis_pool = aioredis.from_url(redis_url, decode_responses=True)
        await _redis_pool.ping()
        log.info("redis_client: Successfully connected to Redis.")
        return _redis_pool
    except Exception as exc:
        log.warning("redis_client: Warning, Redis connection failed. %s", exc)
        _redis_pool = None
        return None


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None

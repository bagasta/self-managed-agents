"""Container healthcheck for the standalone scheduler worker."""
from __future__ import annotations

import asyncio

from app.core.infra.redis_client import close_redis
from app.core.workers.scheduler_service import get_external_scheduler_health


async def _main() -> int:
    try:
        return 0 if await get_external_scheduler_health() == "ok" else 1
    finally:
        await close_redis()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

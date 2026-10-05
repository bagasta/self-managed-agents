"""Run the durable workforce consumer independently: python -m app.workforce_worker."""
import asyncio

from app.database import AsyncSessionLocal, engine
from app.core.domain.workforce_jobs import workforce_queue_loop


async def main():
    try:
        await workforce_queue_loop(AsyncSessionLocal)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

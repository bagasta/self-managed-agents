"""Standalone scheduler worker — run as a separate process in production.

Usage:
    python -m app.scheduler_worker

In Docker Compose, add a 'scheduler' service with this command so that
reminders are not duplicated when running multiple API workers.
"""
import asyncio

from app.core.scheduler_service import run_scheduler_loop

if __name__ == "__main__":
    asyncio.run(run_scheduler_loop())

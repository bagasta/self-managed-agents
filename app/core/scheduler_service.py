"""
APScheduler service — proactive agent engine.

Setiap menit, cek scheduled_jobs yang sudah waktunya dijalankan,
inject payload-nya sebagai user message ke session agent yang bersangkutan,
lalu kirim hasilnya via channel_service.

Lifecycle:
  start_scheduler() dipanggil di FastAPI lifespan startup
  stop_scheduler()  dipanggil di FastAPI lifespan shutdown
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

logger = structlog.get_logger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def _tick() -> None:
    """Cek dan jalankan semua scheduled_jobs yang sudah waktunya."""
    from app.database import AsyncSessionLocal
    from app.models.agent import Agent
    from app.models.scheduled_job import ScheduledJob
    from app.models.session import Session

    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.status == "active",
                ScheduledJob.next_run_at <= now,
            )
        )
        jobs = list(result.scalars().all())

    if not jobs:
        return

    logger.info("scheduler_service.tick", due_jobs=len(jobs))

    for job in jobs:
        asyncio.create_task(_run_job(job.id))


async def _run_job(job_id) -> None:
    """Jalankan satu scheduled job: inject payload ke agent, kirim reply ke channel."""
    from app.core.agent_runner import run_agent
    from app.core.channel_service import send_message
    from app.database import AsyncSessionLocal
    from app.models.agent import Agent
    from app.models.scheduled_job import ScheduledJob
    from app.models.session import Session

    async with AsyncSessionLocal() as db:
        job_result = await db.execute(select(ScheduledJob).where(ScheduledJob.id == job_id))
        job = job_result.scalar_one_or_none()
        if not job or job.status != "active":
            return

        session_result = await db.execute(select(Session).where(Session.id == job.session_id))
        session = session_result.scalar_one_or_none()
        if not session:
            logger.warning("scheduler_service.session_not_found", job_id=str(job_id))
            job.status = "cancelled"
            await db.commit()
            return

        agent_result = await db.execute(select(Agent).where(Agent.id == job.agent_id))
        agent_model = agent_result.scalar_one_or_none()
        if not agent_model:
            logger.warning("scheduler_service.agent_not_found", job_id=str(job_id))
            job.status = "cancelled"
            await db.commit()
            return

        log = logger.bind(job_id=str(job_id), label=job.label, session_id=str(job.session_id))
        log.info("scheduler_service.running_job")

        try:
            result = await run_agent(
                agent_model=agent_model,
                session=session,
                user_message=f"[SCHEDULED] {job.payload}",
                db=db,
            )
            reply = result.get("reply", "")

            # Publish ke SSE event bus (in-app / UI real-time)
            if reply:
                from app.core import event_bus
                await event_bus.publish(str(job.session_id), {
                    "_event_type": "message",
                    "type": "scheduled_message",
                    "label": job.label,
                    "reply": reply,
                    "run_id": str(result.get("run_id", "")),
                })
                log.info("scheduler_service.event_published")

            # Kirim reply ke channel eksternal jika dikonfigurasi
            if session.channel_type and reply:
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
                    text=reply,
                )
                log.info("scheduler_service.reply_sent", channel=session.channel_type)

        except Exception as exc:
            log.error("scheduler_service.job_error", error=str(exc))

        finally:
            # Update job: next_run atau done
            now = datetime.now(timezone.utc)
            job.last_run_at = now

            if job.cron_expr:
                try:
                    from datetime import timedelta
                    from croniter import croniter
                    # Cron dievaluasi dalam WIB (UTC+7), lalu konversi ke UTC untuk disimpan
                    local_tz = timezone(timedelta(hours=7))
                    now_local = now.astimezone(local_tz)
                    next_local = croniter(job.cron_expr, now_local).get_next(datetime)
                    job.next_run_at = next_local.astimezone(timezone.utc)
                except ImportError:
                    from datetime import timedelta
                    job.next_run_at = now + timedelta(hours=1)
            else:
                # One-time job selesai
                job.status = "done"
                job.next_run_at = None

            await db.commit()
            log.info("scheduler_service.job_done", next_run=str(job.next_run_at))


def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(_tick, "interval", minutes=1, id="proactive_tick", replace_existing=True)
    _scheduler.start()
    logger.info("scheduler_service.started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler_service.stopped")


def is_scheduler_running() -> bool:
    return _scheduler is not None and _scheduler.running


async def _tick_with_lock() -> None:
    """Advisory-lock-guarded tick — safe for multi-instance deployments."""
    from sqlalchemy import text
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(text("SELECT pg_try_advisory_lock(12345)"))
        acquired = result.scalar()
        if not acquired:
            return  # another instance is already ticking

    try:
        await _tick()
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT pg_advisory_unlock(12345)"))
            await db.commit()


async def run_scheduler_loop() -> None:
    """Entry point for the standalone scheduler worker process."""
    import signal

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    logger.info("scheduler_worker.started")

    while not stop_event.is_set():
        try:
            await _tick_with_lock()
        except Exception as exc:
            logger.error("scheduler_worker.tick_error", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    logger.info("scheduler_worker.stopped")

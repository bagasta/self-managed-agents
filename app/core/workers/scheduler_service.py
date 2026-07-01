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
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

logger = structlog.get_logger(__name__)
_scheduler: AsyncIOScheduler | None = None

# Concurrency limit: max parallel scheduler jobs to prevent resource exhaustion
_MAX_CONCURRENT_JOBS = 5
_job_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_JOBS)


def _scheduled_channel_config(session: Any, agent_model: Any) -> dict[str, Any]:
    """Return channel config with safe WhatsApp fallbacks for proactive sends."""
    raw_cfg = getattr(session, "channel_config", None)
    cfg: dict[str, Any] = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
    if getattr(session, "channel_type", None) != "whatsapp":
        return cfg

    if not cfg.get("device_id"):
        agent_device_id = str(getattr(agent_model, "wa_device_id", "") or "").strip()
        cfg["device_id"] = agent_device_id or f"wadev_{getattr(agent_model, 'id', getattr(session, 'agent_id', ''))}"

    if not cfg.get("user_phone"):
        cfg["user_phone"] = str(getattr(session, "external_user_id", "") or "")

    return cfg


async def _send_scheduled_channel_message(session: Any, agent_model: Any, text: str, log: Any) -> None:
    from app.core.infra.channel_service import send_message

    cfg = _scheduled_channel_config(session, agent_model)
    device_id = cfg.get("device_id", "")
    log.info(
        "scheduler_service.sending_reply",
        channel=session.channel_type,
        device_id=device_id,
        is_wadev=str(device_id).startswith("wadev_"),
    )
    result = await send_message(
        channel_type=session.channel_type,
        channel_config=cfg,
        text=text,
    )
    if session.channel_type == "whatsapp" and result is None:
        raise RuntimeError("WhatsApp reminder send returned no result")
    log.info("scheduler_service.reply_sent", channel=session.channel_type, device_id=device_id)


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

    tasks = []
    for job in jobs:
        tasks.append(asyncio.create_task(_run_job_guarded(job.id)))

    # Wait for all jobs to finish (or fail) before returning
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_job_guarded(job_id) -> None:
    """Acquire semaphore before running job — bounds concurrent agent runs."""
    async with _job_semaphore:
        await _run_job(job_id)


async def _run_heartbeat_job(job, agent_model, db) -> None:
    """Jalankan heartbeat job: find last active session, run checklist, kirim jika perlu."""
    from app.core.engine.agent_runner import run_agent
    from app.core.domain.memory_service import get_memory
    from app.models.session import Session
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # label = "heartbeat:{external_user_id}"
    label_prefix = "heartbeat:"
    external_user_id = job.label[len(label_prefix):] if job.label.startswith(label_prefix) else None
    if external_user_id == "_global":
        external_user_id = None

    log = logger.bind(job_id=str(job.id), label=job.label, user=external_user_id)

    # Load heartbeat config untuk quiet hours check
    config = {"quiet_start": "23:00", "quiet_end": "08:00"}
    try:
        cfg_mem = await get_memory(job.agent_id, "heartbeat:config", db, scope=external_user_id)
        if cfg_mem:
            config = _json.loads(cfg_mem.value_data)
    except Exception:
        pass

    # Quiet hours check (WIB = UTC+7)
    local_tz = _tz(_td(hours=7))
    now_local = _dt.now(local_tz)
    now_time_str = now_local.strftime("%H:%M")
    q_start = config.get("quiet_start", "23:00")
    q_end = config.get("quiet_end", "08:00")

    def _in_quiet(t: str, start: str, end: str) -> bool:
        if start > end:  # overnight: e.g. 23:00 – 08:00
            return t >= start or t < end
        return start <= t < end

    if _in_quiet(now_time_str, q_start, q_end):
        log.info("heartbeat.quiet_hours", time=now_time_str)
        return

    # Find latest session for this (agent_id, external_user_id)
    from sqlalchemy import select as _sel, desc as _desc
    session_q = _sel(Session).where(Session.agent_id == job.agent_id)
    if external_user_id:
        session_q = session_q.where(Session.external_user_id == external_user_id)
    session_q = session_q.order_by(_desc(Session.updated_at)).limit(1)
    session_result = await db.execute(session_q)
    session = session_result.scalar_one_or_none()
    if not session:
        log.warning("heartbeat.no_session")
        return

    # Load checklist
    checklist_mem = await get_memory(job.agent_id, "heartbeat:checklist", db, scope=external_user_id)
    checklist = (
        checklist_mem.value_data if checklist_mem
        else "- Update daily memory jika belum ditulis hari ini\n- Cek apakah ada hal penting yang perlu disampaikan ke user"
    )

    log.info("heartbeat.running", session_id=str(session.id))
    result = await run_agent(
        agent_model=agent_model,
        session=session,
        user_message=f"[HEARTBEAT] Jalankan checklist berikut:\n{checklist}",
        db=db,
    )
    reply = (result.get("reply") or "").strip()

    # HEARTBEAT_OK → diam
    if not reply or reply.upper().startswith("HEARTBEAT_OK"):
        log.info("heartbeat.ok")
        return

    log.info("heartbeat.notify", reply_len=len(reply), channel=session.channel_type)

    # Kirim notifikasi via channel
    if session.channel_type:
        try:
            await _send_scheduled_channel_message(session, agent_model, reply, log)
            log.info("heartbeat.sent", channel=session.channel_type)
        except Exception as send_exc:
            log.error("heartbeat.send_failed", error=str(send_exc))
    else:
        # Webchat / API → push via SSE
        try:
            from app.core.workers import event_bus
            await event_bus.publish(str(session.id), {
                "_event_type": "message",
                "type": "heartbeat_message",
                "reply": reply,
            })
            log.info("heartbeat.sse_published")
        except Exception as bus_exc:
            log.warning("heartbeat.sse_failed", error=str(bus_exc))


async def _run_job(job_id) -> None:
    """Jalankan satu scheduled job: inject payload ke agent, kirim reply ke channel."""
    from app.core.engine.agent_runner import run_agent
    from app.core.infra.channel_service import send_message
    from app.database import AsyncSessionLocal
    from app.models.agent import Agent
    from app.models.scheduled_job import ScheduledJob
    from app.models.session import Session

    async with AsyncSessionLocal() as db:
        job_result = await db.execute(select(ScheduledJob).where(ScheduledJob.id == job_id))
        job = job_result.scalar_one_or_none()
        if not job or job.status != "active":
            return

        agent_result = await db.execute(select(Agent).where(Agent.id == job.agent_id))
        agent_model = agent_result.scalar_one_or_none()
        if not agent_model:
            logger.warning("scheduler_service.agent_not_found", job_id=str(job_id))
            job.status = "cancelled"
            await db.commit()
            return

        # Heartbeat jobs ditangani terpisah
        if job.payload == "[HEARTBEAT]":
            try:
                await _run_heartbeat_job(job, agent_model, db)
            except Exception as exc:
                logger.error("heartbeat.error", job_id=str(job_id), error=str(exc), exc_info=True)
            finally:
                # Update next_run
                from datetime import timedelta
                from croniter import croniter
                now = datetime.now(timezone.utc)
                job.last_run_at = now
                if job.cron_expr:
                    try:
                        local_tz = timezone(timedelta(hours=7))
                        now_local = now.astimezone(local_tz)
                        next_local = croniter(job.cron_expr, now_local).get_next(datetime)
                        job.next_run_at = next_local.astimezone(timezone.utc)
                    except Exception:
                        job.next_run_at = now + timedelta(minutes=30)
                else:
                    job.status = "done"
                await db.commit()
            return

        session_result = await db.execute(select(Session).where(Session.id == job.session_id))
        session = session_result.scalar_one_or_none()
        if not session:
            logger.warning("scheduler_service.session_not_found", job_id=str(job_id))
            job.status = "cancelled"
            await db.commit()
            return

        log = logger.bind(job_id=str(job_id), label=job.label, session_id=str(job.session_id))
        log.info("scheduler_service.running_job")
        delivery_failed = False

        try:
            # Kirim payload reminder langsung tanpa LLM — menghilangkan latensi 2-3 menit
            # dari full agent run. Reminder harus tepat waktu; formatting natural nomor dua.
            reply = job.payload

            # Publish ke SSE event bus (in-app / UI real-time)
            # Diisolasi: error event_bus tidak boleh memblokir pengiriman ke channel eksternal
            try:
                from app.core.workers import event_bus
                await event_bus.publish(str(job.session_id), {
                    "_event_type": "message",
                    "type": "scheduled_message",
                    "label": job.label,
                    "reply": reply,
                })
                log.info("scheduler_service.event_published")
            except Exception as bus_exc:
                log.warning("scheduler_service.event_bus_failed", error=str(bus_exc))

            # Kirim reply ke channel eksternal (WhatsApp / wa-dev / dll).
            if session.channel_type:
                try:
                    await _send_scheduled_channel_message(session, agent_model, reply, log)
                except Exception as send_exc:
                    delivery_failed = True
                    log.error(
                        "scheduler_service.reply_send_failed",
                        channel=session.channel_type,
                        error=str(send_exc),
                    )

        except Exception as exc:
            log.error("scheduler_service.job_error", error=str(exc), exc_info=True)

        finally:
            # Update job: next_run atau done
            now = datetime.now(timezone.utc)
            job.last_run_at = now

            if delivery_failed:
                from datetime import timedelta
                job.next_run_at = now + timedelta(minutes=1)
                log.warning("scheduler_service.job_retry_scheduled", next_run=str(job.next_run_at))
            elif job.cron_expr:
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

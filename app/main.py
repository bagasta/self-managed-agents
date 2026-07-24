import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import agents, auth, channels, custom_tools, documents, history, memory, messages, models, runs, sessions, skills, stream, subscriptions, users
from app.config import get_settings
from app.database import engine, get_db
from app.middleware.request_id import RequestIDMiddleware
from app.models.agent import Agent
from app.models.skill import Skill

settings = get_settings()
HEALTH_DB_TIMEOUT_SECONDS = 2.0
STARTUP_TASK_TIMEOUT_SECONDS = 5.0

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer()
        if settings.log_level == "DEBUG"
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

if settings.sentry_dsn:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.1,
        environment=settings.environment,
    )

limiter_opts = {"key_func": get_remote_address}
if getattr(settings, "redis_url", ""):
    limiter_opts["storage_uri"] = settings.redis_url

limiter = Limiter(**limiter_opts)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.environ.get("PYTEST_CURRENT_TEST") or settings.environment == "test":
        yield
        return

    # Pre-load embedding model so the first request isn't slow.
    # Downloads model files (~130MB) on first run, cached after that.
    from app.core.domain.embedding_service import warmup_embedding_model
    try:
        await asyncio.wait_for(
            warmup_embedding_model(),
            timeout=STARTUP_TASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        structlog.get_logger(__name__).warning("startup.embedding_warmup.timeout")

    # Runs cannot survive process restarts because in-memory graph state,
    # sandboxes, active task registry, and locks are gone. Mark them explicitly
    # so the next user message does not replay the unfinished prompt from DB
    # history as if it still needs to be completed.
    from sqlalchemy import update
    from app.database import AsyncSessionLocal
    from app.models.run import Run

    async def _abandon_running_runs() -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Run)
                .where(Run.status == "running")
                .values(
                    status="abandoned",
                    completed_at=datetime.now(timezone.utc),
                    error_message="Process restarted before this run completed.",
                )
                .returning(Run.id)
            )
            abandoned = result.all()
            await db.commit()
            if abandoned:
                structlog.get_logger(__name__).warning("startup.abandoned_running_runs", count=len(abandoned))

    try:
        await asyncio.wait_for(
            _abandon_running_runs(),
            timeout=STARTUP_TASK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        structlog.get_logger(__name__).warning("startup.abandon_running_runs.timeout")

    # Create the bounded Redis pool before burst traffic reaches the process.
    # Consumers still degrade to their existing in-memory fallbacks when Redis
    # is unavailable.
    from app.core.infra.redis_client import close_redis, get_redis
    await get_redis()

    # Local/dev deployments can keep the scheduler embedded. Production runs a
    # dedicated scheduler container so API capacity stays focused on chat.
    from app.core.workers.scheduler_service import start_scheduler, stop_scheduler

    if settings.embedded_scheduler_enabled:
        start_scheduler()

    from app.core.infra.deployment_service import (
        cleanup_expired_deployments,
        deployment_cleanup_interval_seconds,
    )

    async def _deployment_cleanup_loop() -> None:
        cleanup_log = structlog.get_logger(__name__)
        interval = deployment_cleanup_interval_seconds()
        while True:
            try:
                result = await asyncio.to_thread(cleanup_expired_deployments)
                if result.get("evicted"):
                    cleanup_log.info("deployment_cleanup.evicted", **result)
            except Exception as exc:
                cleanup_log.warning("deployment_cleanup.error", error=str(exc))
            await asyncio.sleep(interval)

    deployment_cleanup_task = asyncio.create_task(_deployment_cleanup_loop())

    from app.core.infra.sandbox import cleanup_orphan_sandboxes

    async def _sandbox_reaper_loop() -> None:
        reaper_log = structlog.get_logger(__name__)
        while True:
            await asyncio.sleep(600)  # every 10 minutes
            try:
                result = await asyncio.to_thread(cleanup_orphan_sandboxes)
                if result.get("containers_killed") or result.get("workspace_dirs_removed"):
                    reaper_log.info("sandbox_reaper.cleaned", **result)
            except Exception as exc:
                reaper_log.warning("sandbox_reaper.error", error=str(exc))

    sandbox_reaper_task = asyncio.create_task(_sandbox_reaper_loop())

    yield

    deployment_cleanup_task.cancel()
    sandbox_reaper_task.cancel()
    for _t in (deployment_cleanup_task, sandbox_reaper_task):
        try:
            await _t
        except asyncio.CancelledError:
            pass
    if settings.embedded_scheduler_enabled:
        stop_scheduler()
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="Managed Agent Platform",
    description="Self-hosted multi-model agent platform powered by LangChain + OpenRouter",
    version="0.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(subscriptions.router)
app.include_router(agents.router)
app.include_router(channels.router)
app.include_router(sessions.router)
app.include_router(messages.router)
app.include_router(history.router)
app.include_router(memory.router)
app.include_router(skills.router)
app.include_router(custom_tools.router)
app.include_router(documents.router)
app.include_router(models.router)
app.include_router(runs.router)
app.include_router(stream.router)
from app.api import integrations
app.include_router(integrations.router)

app.mount("/ui", StaticFiles(directory="UI-DEV", html=True), name="ui")

Instrumentator().instrument(app).expose(app, endpoint="/metrics", tags=["meta"])


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {
        "status": "ok",
        "version": "0.2.0",
        "commit": settings.app_commit_sha,
        "arthur_runtime": {
            "engine_version": settings.arthur_engine_version,
            "prompt_version": settings.arthur_prompt_version,
            "primary_model": settings.arthur_primary_model,
            "document_model": settings.arthur_document_model,
            "image_model": settings.arthur_image_model,
        },
    }


@app.get("/health/detailed", tags=["meta"])
async def health_detailed(db: AsyncSession = Depends(get_db)) -> dict:
    checks: dict[str, str] = {}
    arthur_runtime: dict[str, object] = {
        "engine_version": settings.arthur_engine_version,
        "prompt_version": settings.arthur_prompt_version,
        "primary_model": settings.arthur_primary_model,
        "document_model": settings.arthur_document_model,
        "image_model": settings.arthur_image_model,
        "active_system_skills": 0,
    }

    try:
        await asyncio.wait_for(
            db.execute(text("SELECT 1")),
            timeout=HEALTH_DB_TIMEOUT_SECONDS,
        )
        checks["database"] = "ok"
        _arthur_result = await db.execute(
            select(Agent).where(
                Agent.name == "Arthur",
                Agent.capabilities.contains(["system"]),
                Agent.is_deleted.is_(False),
            )
        )
        arthur = (
            _arthur_result.scalar_one_or_none()
            if hasattr(_arthur_result, "scalar_one_or_none")
            else None
        )
        if arthur is not None:
            active_skills = list(
                (
                    await db.execute(
                        select(Skill).where(
                            Skill.agent_id == arthur.id,
                            Skill.trust_level == "system",
                            Skill.enabled.is_(True),
                        )
                    )
                ).scalars()
            )
            runtime_cfg = (
                arthur.tools_config.get("arthur_runtime", {})
                if isinstance(arthur.tools_config, dict)
                else {}
            )
            arthur_runtime.update({
                "primary_model": arthur.model,
                "engine_version": runtime_cfg.get("engine_version", settings.arthur_engine_version),
                "prompt_version": runtime_cfg.get("prompt_version", settings.arthur_prompt_version),
                "skill_bundle_version": runtime_cfg.get("skill_bundle_version"),
                "active_system_skills": len(active_skills),
            })
    except asyncio.TimeoutError:
        checks["database"] = "error: timeout"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    from app.core.workers.scheduler_service import is_scheduler_running
    if settings.embedded_scheduler_enabled:
        checks["scheduler"] = "ok" if is_scheduler_running() else "stopped"
    else:
        checks["scheduler"] = "external"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.wa_service_url}/health")
            checks["wa_service"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception:
        checks["wa_service"] = "unreachable"

    all_ok = all(v in {"ok", "external"} for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ok" if all_ok else "degraded",
            "checks": checks,
            "version": "0.2.0",
            "commit": settings.app_commit_sha,
            "arthur_runtime": arthur_runtime,
        },
    )


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

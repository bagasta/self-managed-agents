import logging
from contextlib import asynccontextmanager

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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import agents, channels, custom_tools, documents, history, memory, messages, models, runs, sessions, skills, stream
from app.config import get_settings
from app.database import get_db
from app.middleware.request_id import RequestIDMiddleware

settings = get_settings()

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

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-load embedding model so the first request isn't slow.
    # Downloads model files (~130MB) on first run, cached after that.
    from app.core.embedding_service import warmup_embedding_model
    await warmup_embedding_model()

    # Start proactive agent scheduler (only in non-worker deployments)
    from app.core.scheduler_service import start_scheduler, stop_scheduler
    start_scheduler()

    yield

    stop_scheduler()


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

app.mount("/ui", StaticFiles(directory="UI-DEV", html=True), name="ui")

Instrumentator().instrument(app).expose(app, endpoint="/metrics", tags=["meta"])


@app.get("/health", tags=["meta"])
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    if not db_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": "unreachable", "version": "0.2.0"},
        )
    return {"status": "ok", "version": "0.2.0", "db": "ok"}


@app.get("/health/detailed", tags=["meta"])
async def health_detailed(db: AsyncSession = Depends(get_db)) -> dict:
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    from app.core.scheduler_service import is_scheduler_running
    checks["scheduler"] = "ok" if is_scheduler_running() else "stopped"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.wa_service_url}/health")
            checks["wa_service"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception:
        checks["wa_service"] = "unreachable"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "degraded", "checks": checks, "version": "0.2.0"},
    )


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

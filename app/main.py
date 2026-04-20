import logging
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import agents, channels, custom_tools, documents, history, memory, messages, models, runs, sessions, skills, stream
from app.config import get_settings

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Pre-load embedding model so the first request isn't slow.
    # Downloads model files (~130MB) on first run, cached after that.
    from app.core.embedding_service import warmup_embedding_model
    await warmup_embedding_model()

    # Start proactive agent scheduler
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # lock down in production
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


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

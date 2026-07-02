# Tech Stack

Tanggal snapshot: 2026-07-02

## Frontend Stack
- `UI-DEV/`: static HTML/CSS/JS dashboard/dev UI mounted at `/ui`.
- No production frontend framework is currently the source of truth in this repo.

## Backend Stack
- Python 3.12.
- FastAPI `0.115.6`.
- Uvicorn.
- Pydantic v2 + pydantic-settings.
- SQLAlchemy async `2.0.36`.
- Alembic migrations.
- structlog JSON logging.
- slowapi for rate limiting.

## Agent Runtime
- DeepAgents `>=0.5.0`.
- LangGraph `>=1.2.5,<1.3.0`.
- LangChain `>=1.3.0`.
- LangChain OpenAI adapter with OpenRouter base URL.
- LangChain MCP adapters for external MCP tools.

## Database
- PostgreSQL.
- pgvector extension for document embeddings.
- asyncpg primary async driver.
- psycopg2-binary available for tooling/compatibility.

## Cache / Event Bus
- Redis optional via `REDIS_URL`.
- In-memory fallback for local/single-worker mode.
- Redis also used by slowapi storage when configured.

## Queue / Scheduler
- APScheduler for scheduled jobs/reminders.
- Production has a separate `scheduler` service using `python -m app.scheduler_worker`.
- No dedicated broker queue like Celery/RQ yet.

## Infrastructure
- Docker Compose local and production.
- Docker socket mounted for sibling sandbox/deployment containers.
- Traefik labels in production compose.
- pgbouncer in production compose.
- Sandbox image from `sandbox.Dockerfile`.

## Cloud Provider
- Not hard-bound to one cloud.
- Current production compose expects a VPS with Docker, Traefik network `root_default`, host PostgreSQL, and DNS.

## AI Models
- OpenRouter as LLM gateway.
- Per-agent model selection, default currently `anthropic/claude-sonnet-4-6`.
- Common models referenced: `openai/gpt-4.1-mini`, `openai/gpt-4o-mini`, DeepSeek variants, Gemini/Claude via OpenRouter.
- Embeddings: local/domain service references 1536 dimension embeddings; docs/comment mention text embedding dimension and previous README mentions sentence-transformers. Verify implementation before changing embedding provider.

## Third-Party Services
- OpenRouter: LLM execution.
- Mistral OCR: PDF text extraction.
- Tavily: web search/extract agent tools.
- Google Workspace MCP + integration OAuth service.
- WhatsApp Web via whatsmeow.
- Sentry optional.
- Cloudflare tunnel used by deployment tooling for temporary public URLs.

## Justification and Trade-offs
- FastAPI + async SQLAlchemy: good fit for API-heavy orchestration, but long agent runs require careful timeout and locking.
- OpenRouter: broad model choice with one API key, but model behavior and pricing are external dependencies.
- PostgreSQL + pgvector: one durable store for config, history, and RAG; vector performance may need indexes/tuning as corpus grows.
- Docker sandbox: pragmatic for code/file tasks; high security risk if Docker socket and containers are not hardened.
- WhatsApp Web service: fast to integrate, but operationally sensitive because sessions can break and protocol behavior can change.
- MCP: clean connector abstraction, but every MCP needs auth, scope, and false-success guards.


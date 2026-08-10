# Deployment Guide

Tanggal snapshot: 2026-07-02

## Environment Overview
- Local dev: Python app on host, PostgreSQL via Docker Compose, optional Go WA services.
- Local full stack: `docker compose up --build`.
- Production: `deploy/docker-compose.prod.yml` with API, scheduler, Redis, WA services, pgbouncer, Traefik labels, host DB.

## Infrastructure Layout
```text
VPS / Docker host
  |-- Traefik on root_default network
  |-- managed-agents api container
  |-- scheduler container
  |-- redis container
  |-- wa-service container
  |-- wa-dev-service container
  |-- pgbouncer container
  |-- host PostgreSQL or external DB
  |-- /tmp/agent-sandboxes mounted into API
  |-- /var/run/docker.sock mounted into API
```

## Environment Variables
Core:
- `DATABASE_URL`
- `API_KEY`
- `OPENROUTER_API_KEY`
- `LOG_LEVEL`
- `ENVIRONMENT`
- `ALLOWED_ORIGINS`

Agent limits:
- `AGENT_MAX_STEPS`
- `AGENT_TIMEOUT_SECONDS`
- `LLM_MAX_TOKENS`
- `MESSAGE_MAX_LENGTH`
- `MEDIA_MAX_LENGTH`

Sandbox:
- `SANDBOX_BASE_DIR`
- `SANDBOX_HOST_BASE_DIR`
- `DOCKER_SANDBOX_IMAGE`
- `DOCKER_HOST`
- `SANDBOX_SUBAGENTS_ENABLED`
- `SANDBOX_MEM_LIMIT`
- `SANDBOX_NANO_CPUS`
- `MAX_CONCURRENT_SANDBOXES`
- `SANDBOX_CONTAINER_TTL_SECONDS`
- `SANDBOX_WORKSPACE_TTL_SECONDS`

WhatsApp:
- `WA_SERVICE_URL`
- `WA_DEV_SERVICE_URL`
- `WA_DEV_PUBLIC_PHONE`
- `WA_DEV_PUBLIC_NAME`

Integrations:
- `TAVILY_API_KEY`
- `MISTRAL_API_KEY`
- `WORKSPACE_MCP_URL`
- `WORKSPACE_MCP_RUNTIME_URL`
- `WORKSPACE_MCP_PREFER_LOCAL`
- `WORKSPACE_MCP_TOKEN`
- `GOOGLE_INTEGRATION_SERVICE_URL`
- `SENTRY_DSN`
- `REDIS_URL`

## Build Process
Local:
```bash
make install
make sandbox-build
make wa-build
make wa-dev-build
```

Docker:
```bash
docker compose up --build
```

Production:
```bash
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

## Deployment Process
1. Verify `.env.prod` and required secrets.
2. Build or pull latest images.
3. Apply migrations: `alembic upgrade head`.
4. Seed Arthur/system agents if required.
5. Start API, scheduler, Redis, WA services.
6. Check `/health` and `/health/detailed`.
7. Run smoke tests for API, WA, Google MCP if enabled.
8. Monitor logs and metrics during rollout.

## Rollback Strategy
- Keep previous image/commit reference.
- Stop new stack and restart previous image.
- For DB migrations, only run backward-compatible migrations in production unless rollback SQL is tested.
- If Arthur prompt/seed caused regression, reseed previous `system-message-builder.md` state.
- If WA services break, do not delete WA store volumes; rollback binary/container only.

## Backup Strategy
- PostgreSQL scheduled dumps with retention.
- WA store volumes: `wa_store`, `wa_dev_store`.
- `.env.prod` stored in secure secret backup.
- Sandbox workspaces are ephemeral and usually excluded.
- Export critical agent config and operating manuals before risky migrations.

## Disaster Recovery
- Restore DB dump.
- Restore WA store volumes if WhatsApp sessions are needed.
- Recreate Docker network and compose stack.
- Re-run migrations to expected version.
- Verify Arthur dedicated WA session and shared trial WA service are not swapped.
- Re-auth Google Workspace integrations as needed.


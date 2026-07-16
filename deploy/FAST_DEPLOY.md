# Fast and Stable Production Deploy

The production Compose stack uses one shared Python image for `api` and
`scheduler`. Docker BuildKit caches Python and Go dependencies, while
`.dockerignore` files keep local virtualenvs, Git history, tests, docs, databases,
and compiled binaries out of build contexts.

## API-only changes

Use this for Python API, Arthur runtime, prompt, or builder changes. It leaves
the scheduler and both WhatsApp services running:

```bash
make deploy-api-fast
docker exec deploy-api-1 alembic upgrade head
```

If `system-message-builder.md` or `scripts/seed_arthur.py` changed:

```bash
docker exec deploy-api-1 python scripts/seed_arthur.py --dry-run
docker exec deploy-api-1 python scripts/seed_arthur.py
```

## API and scheduler changes

Build the shared Python image once, then recreate both consumers:

```bash
make deploy-app
docker exec deploy-api-1 alembic upgrade head
```

## Dockerfile or WhatsApp Go changes

Rebuild the complete stack only when the container definitions or Go services
changed:

```bash
make deploy-all
docker exec deploy-api-1 alembic upgrade head
```

Never use `docker compose down -v` in production; the WhatsApp authentication
volumes would be deleted.

## Burst-traffic safety

Keep Uvicorn on one worker until the session lock and active-task registry are
distributed. Different sessions still execute concurrently through asyncio,
with `MAX_CONCURRENT_AGENT_RUNS` providing backpressure. Relevant production
tuning variables are documented in `.env.example`. Production Compose also sets
`EMBEDDED_SCHEDULER_ENABLED=false` because scheduled jobs run in the dedicated
`scheduler` service; this prevents the API from polling the same jobs twice.

The defaults allow 24 expensive agent runs, 48 in-flight requests from
`wa-service`, and 48 from `wa-dev-service` under an API concurrency limit of
128. This leaves capacity for health checks and dashboard calls. Keep the sum
of both WhatsApp in-flight limits below `API_LIMIT_CONCURRENCY`; queued messages
then wait in the Go services instead of being rejected by Uvicorn during a
traffic burst.

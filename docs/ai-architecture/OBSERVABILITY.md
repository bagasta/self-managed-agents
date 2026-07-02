# Observability

Tanggal snapshot: 2026-07-02

## Logging Strategy
- structlog configured globally in `app/main.py`.
- JSON logs in non-debug mode.
- Include request IDs through `RequestIDMiddleware`.
- Log key lifecycle events: startup warmup, abandoned runs, scheduler, sandbox cleanup, deployment cleanup, MCP load/errors, WA send failures, quota failures.
- Avoid logging secrets, tokens, full API keys, OAuth tokens, or raw user media.

## Metrics Collection
- `prometheus-fastapi-instrumentator` exposes `/metrics`.
- Existing metrics cover HTTP request/latency basics.
- Recommended custom metrics:
  - agent run duration by model/tool group.
  - token usage and OpenRouter cost by owner/agent.
  - WA inbound-to-reply latency.
  - sandbox queue wait and execution duration.
  - MCP success/error by server/tool.
  - scheduler job success/failure.
  - quota denials.

## Distributed Tracing
- No OpenTelemetry tracing is currently configured.
- Recommended: add trace IDs across API -> agent run -> tool call -> external HTTP/WA/MCP call.
- Use request ID as a minimal correlation ID until tracing is added.

## Dashboards
Recommended dashboard panels:
- API RPS, p95/p99 latency, 4xx/5xx.
- Active/running/failed/abandoned runs.
- Token usage and cost per day.
- WA service health and send error count.
- Redis availability and rate limit errors.
- Sandbox active containers, OOM/errors, cleanup counts.
- MCP auth failures and unavailable errors.
- DB pool saturation and slow queries.

## Alert Rules
- `/health/detailed` degraded.
- API 5xx rate above threshold.
- WA service unreachable.
- OpenRouter/API provider error spike.
- Google MCP auth/unavailable error spike.
- Sandbox queue saturation or container cleanup failures.
- Redis down in production.
- Token usage/cost anomaly.
- Disk space low on DB/WA store/sandbox host paths.

## Error Monitoring
- Sentry is optional via `SENTRY_DSN`.
- FastAPI and SQLAlchemy integrations are enabled when configured.
- Recommended: tag events by environment, agent_id, owner_external_id hash, run_id, request_id.

## Performance Monitoring
- Watch agent run duration against `AGENT_TIMEOUT_SECONDS`.
- Watch embedding warmup and document upload latency.
- Track model-specific latency/cost.
- Track long-running subagent and deployment workflows separately.

## Incident Response
1. Check `/health/detailed`.
2. Inspect API and WA service logs by request/run/session ID.
3. Check Redis and DB connectivity.
4. For WA incidents, verify device status and WA store volume.
5. For Google incidents, generate re-auth link and run MCP smoke test.
6. For sandbox incidents, inspect running labeled containers and cleanup.
7. Record incident timeline, impact, root cause, and follow-up tests.


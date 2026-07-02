# Implementation Guide for AI Coding Agents

Tanggal snapshot: 2026-07-02

## Architecture Summary
This repo is a FastAPI-based managed AI agent platform. The core execution path is:
```text
app/api/messages.py
  -> app/core/engine/agent_runner.py
  -> prompt_builder + agent_tool_setup + tool_builder + MCP runtime
  -> DeepAgents/LangGraph
  -> DB persistence + channel delivery
```

Persistent state lives in PostgreSQL models under `app/models`. Runtime domain logic lives under `app/core/domain`. External integrations live under `app/core/infra`. Tools are defined under `app/core/tools`.

## Development Order
1. Read the relevant API route and schema.
2. Read the domain/runtime module it calls.
3. Check tests for that feature.
4. Make the smallest code change in the existing module boundary.
5. Add or update tests.
6. Run focused tests.
7. Update docs if API/env/tool/schema/deployment behavior changed.

## Critical Rules
- Do not collapse Arthur's dedicated WhatsApp identity into `wa-dev-service`.
- `wa-dev-service` is only the shared trial number for user-created agents.
- Runtime message endpoint uses `X-Agent-Key`; management endpoints use `X-API-Key`.
- Do not expose new tools without thinking through owner/operator/customer authorization.
- Do not make MCP/Google actions appear successful unless a tool call actually succeeded.
- Do not let sandbox or subagent produce customer-visible final delivery without parent verification.
- Do not silently swallow exceptions in critical paths.
- Do not introduce secrets into repo, logs, docs, Docker image, or tests.

## Technical Constraints
- Python async stack: avoid blocking the event loop unless wrapped in thread/offload.
- SQLAlchemy async sessions must be committed/flushed intentionally.
- Alembic migration required for DB schema changes.
- Tool config must pass `ToolsConfig`.
- Sandbox execution requires Docker image and host path mapping to be correct.
- Production can run API and scheduler separately; avoid in-memory-only assumptions.

## Coding Standards Reference
See `CODING_STANDARDS.md`.

## Testing Requirements
- Add focused tests for behavior you change.
- For security fixes, add negative tests.
- For agent runtime changes, test guard and fallback behavior.
- For migrations, test upgrade path with model usage.
- For Google MCP, prefer mocked tests unless using explicit live-smoke env flags.

## Acceptance Requirements
- Behavior satisfies `ACCEPTANCE_CRITERIA.md`.
- API contracts match `API_SPEC.md`.
- Tool exposure matches `AGENT_ARCHITECTURE.md`.
- Data changes match `DATABASE_SCHEMA.md`.
- Deployment/env changes update `DEPLOYMENT.md` and `.env.example`.

## Deployment Considerations
- Run migrations before new code depends on columns.
- Reseed Arthur if `system-message-builder.md` changes.
- Verify `/health/detailed`.
- Run WA and Google MCP smoke tests if touched.
- Watch metrics/logs after deploy.
- Keep rollback path ready for DB and app image.

## Recommended First Files to Inspect
- `README.md`
- `CLAUDE.md`
- `app/main.py`
- `app/config.py`
- `app/api/messages.py`
- `app/core/engine/agent_runner.py`
- `app/core/engine/agent_tool_setup.py`
- `app/core/engine/prompt_builder.py`
- `app/core/engine/google_mcp_support.py`
- `app/core/tools/tool_builder.py`
- `app/models/*`
- `tests/test_*` matching the feature.


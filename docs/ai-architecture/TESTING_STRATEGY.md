# Testing Strategy

Tanggal snapshot: 2026-07-02

## Unit Testing
- Test pure helpers and policy logic in isolation.
- Targets: phone/WA identity, input sanitizer, tool config validation, prompt guards, Google MCP arg normalizers, subscription calculations.
- Use deterministic fixtures and avoid network.

## Integration Testing
- Test FastAPI routes with DB session fixtures.
- Cover agents, sessions, messages, users, subscriptions, memory, skills, documents, channels.
- Mock OpenRouter, WA service, Docker, MCP, Tavily, Mistral where possible.

## End-to-End Testing
- Arthur create/update/verify agent flow.
- WA inbound -> agent run -> WA reply.
- WA dev trial claim/disconnect route.
- Google MCP safe live smoke behind env flags.
- Generated file flow: subagent writes `/workspace/shared`, parent sends media.

## Performance Testing
- Use `locust-load/` for load scenarios.
- Measure API latency, agent run duration, WA round-trip, DB query pressure.
- Test sandbox concurrency with `MAX_CONCURRENT_SANDBOXES`.

## Security Testing
- Webhook auth bypass tests.
- SSRF tests for HTTP tools against localhost/private/metadata IP.
- IDOR tests: user A cannot access user B resources.
- Upload size/content tests.
- Sandbox hardening configuration tests.
- Prompt injection tests for user input, RAG, and memory.
- Secret scanning in CI.

## Load Testing
- Simulate:
  - many short WhatsApp messages.
  - long-running agent runs.
  - concurrent sandbox tasks.
  - scheduler event fanout.
  - document uploads/search.
- Track DB pool, Redis, Docker container count, memory, and OpenRouter error rate.

## Test Coverage Requirements
- P0 security paths must have regression tests before production.
- API resource CRUD should cover auth, success, validation, and not-found.
- Runtime guards should cover both allow and deny cases.
- Google MCP should have mocked tests plus optional live safe smoke.
- Full suite must finish reliably; use timeouts to isolate hangs.

## Test Commands
```bash
pytest
make lint
make format
make mcp-smoke-live
make mcp-smoke-live-strict
```

Current note: `Makefile` has no generic `make test` target; add one if the release process standardizes on it.


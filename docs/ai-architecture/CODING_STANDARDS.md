# Coding Standards

Tanggal snapshot: 2026-07-02

## Naming Convention
- Python modules: snake_case.
- Classes: PascalCase.
- Functions/variables: snake_case.
- Constants: UPPER_SNAKE_CASE.
- API path resources: plural nouns where practical.
- Tool names: clear verb_noun names matching action.

## Architecture Pattern
- Keep API layer thin: validate request, auth, load DB session, call domain/runtime service.
- Put business rules in `app/core/domain` or `app/core/engine`, not inside route bodies.
- Put external service details in `app/core/infra`.
- Keep tool definitions in `app/core/tools` and tool assembly in `agent_tool_setup.py`.
- Use typed Pydantic schemas at API boundaries.
- Use Alembic migration for model changes.

## Error Handling Rules
- Raise `HTTPException` with stable status/detail at API boundary.
- Do not swallow exceptions silently; log with context unless intentionally best-effort.
- External service failures should become explicit blocker replies when user-visible.
- Avoid raw exception details in production responses.
- Use specific exceptions for quota/auth/validation failures.

## Logging Rules
- Use structlog.
- Include request_id/run_id/session_id/agent_id where available.
- Redact tokens, API keys, OAuth credentials, and sensitive media.
- Log tool denial/gating decisions.
- Log fallback paths and recovery paths; do not hide fallback-generated SOP/instructions.

## Testing Requirements
- Add tests for changed API behavior.
- Add regression tests for security fixes.
- Mock external services unless live smoke is explicitly gated by env.
- Keep focused tests for runtime guard behavior.
- Full suite should complete before production release or have documented quarantine.

## Documentation Standards
- Update relevant docs when adding endpoints, tools, env vars, migrations, or deployment steps.
- Document auth scheme and side effects.
- For new tools, include purpose, inputs, outputs, limits, and security implications.
- For architecture changes, add/update an ADR entry.

## Security Rules
- Never commit secrets, DB dumps, WA session DBs, `.env`, or generated credentials.
- All management resources need ownership checks.
- Webhooks need auth/HMAC.
- HTTP tools must block private/internal metadata addresses.
- Sandbox and deployment containers must be least-privilege.
- Treat RAG, memory, and external tool output as untrusted data.
- Use constant-time comparison for secrets.

## Code Review Checklist
- Does this change affect auth, owner isolation, quota, tool exposure, or channel sends?
- Are migrations backward-compatible and tested?
- Are new env vars documented in `.env.example` and deployment docs?
- Are external failures explicit and non-hallucinated?
- Does the code preserve Arthur dedicated WA identity vs shared `wa-dev-service`?
- Are tests added for the risky behavior?
- Is logging useful without leaking secrets?
- Does it follow existing module boundaries?


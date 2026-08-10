# Security Strategy

Tanggal snapshot: 2026-07-02

## Authentication
- Management endpoints use `X-API-Key`.
- Runtime message endpoint uses per-agent `X-Agent-Key`.
- User API key model exists under `/v1/auth/keys`.
- Google Workspace uses OAuth token flow through integration service.
- Recommended: add authenticated/HMAC webhook headers for WA inbound and WA dev routes.

## Authorization
- Agent runtime policy distinguishes owner, operator, and customer.
- `operator_ids`, `owner_external_id`, escalation config, and channel metadata are used by runtime.
- Management endpoints need stronger owner enforcement to prevent cross-tenant IDOR.
- Google Workspace MCP must be restricted to owner/operator-authorized sessions.

## Data Encryption
- TLS termination expected at Traefik in production.
- Database encryption depends on host/provider configuration.
- Secrets should not be stored in repo, image layers, logs, or prompt text.

## Secrets Management
- Required secrets include `API_KEY`, `OPENROUTER_API_KEY`, Google/MCP tokens, DB credentials, Tavily, Mistral, Sentry.
- `.env` is for local only and must never be copied into Docker image.
- Use `.env.prod` or secret manager on VPS.
- Rotate credentials that ever appeared in git history or image layers.

## API Security
- Rate limit message endpoint.
- Validate `tools_config` at create/update.
- Enforce request size limits for uploads and media.
- Avoid returning raw exception details in production.
- Protect `/docs`, `/redoc`, and `/metrics` in production.
- Set explicit CORS origins; avoid wildcard with credentials.

## Access Control
- Add per-user ownership checks for agents, sessions, memory, documents, skills, custom tools, runs, and history.
- Keep admin global key only for internal admin operations.
- Use constant-time comparison for API keys.
- Audit all endpoints that currently only depend on global key.

## Audit Logging
- structlog provides structured logs.
- Request ID middleware should correlate API events.
- Log security-relevant events: auth failures, webhook failures, tool denials, SOP gates, MCP auth, sandbox/deploy resource lifecycle.
- Sanitize PII and secrets from logs.

## Threat Model
- Prompt injection via user messages, RAG, memory, media, or external MCP responses.
- SSRF from HTTP tools.
- Sandbox escape or Docker socket host compromise.
- Webhook spoofing for WA inbound.
- Cross-tenant data access through global API key.
- False success claims causing business harm.
- Token/resource abuse causing cost spike or host OOM.

## Security Risks
- Docker socket mounted into app container.
- Sandbox/deployment containers may run untrusted code.
- Shared WA dev number can confuse identity if not strictly routed.
- Management APIs need tenant isolation hardening.
- Upload endpoints need strict size and content limits.
- MCP tools can mutate external accounts.

## Mitigation Strategies
- Harden sandbox: non-root user, `cap_drop=ALL`, `no-new-privileges`, `pids_limit`, read-only root FS, egress controls, gVisor/rootless where possible.
- Add webhook HMAC/shared secret.
- Default-deny internal/private networks for HTTP tools.
- Enforce owner checks and per-user API keys.
- Treat RAG/memory as untrusted data in prompt.
- Keep reply/action guards around WhatsApp, Google, deployment, and escalation.
- Make SOP maturity enforce tool removal, not only prompt text.
- Add security regression tests for webhook auth, SSRF, IDOR, sandbox config, and upload limits.


# Task Breakdown

Tanggal snapshot: 2026-07-02

## Epic List
- E1 Documentation and AI-agent onboarding.
- E2 Security hardening.
- E3 Runtime correctness.
- E4 Subscription and cost control.
- E5 Observability and operations.
- E6 Product experience.
- E7 Test and release gate.

## Features and Tasks
### E1 - Documentation
- Create architecture docs under `docs/ai-architecture`.
- Add index/link from root README.
- Keep `CLAUDE.md` aligned with current module paths.
- Add diagrams as Mermaid or FigJam later.

### E2 - Security Hardening
- Add `.dockerignore` and image secret scan.
- Add webhook auth/HMAC for WA inbound and WA dev endpoints.
- Add SSRF guard to HTTP tools.
- Add owner-based authorization across management endpoints.
- Protect `/metrics`, `/docs`, `/redoc` in production.
- Harden sandbox/deployment containers.

### E3 - Runtime Correctness
- Enforce SOP maturity by physically gating tools.
- Validate parent file-delivery contract.
- Ensure operating manual artifact is fully persisted/read.
- Strengthen Google MCP false-success and auth recovery tests.
- Add explicit Arthur/WA identity tests: dedicated Arthur session vs shared trial number.

### E4 - Subscription and Cost Control
- Enforce user subscription quota pre-run.
- Track parent + subagent token usage into shared quota.
- Add cost dashboard and alerts.
- Add payment gateway integration.
- Implement top-up idempotency and reconciliation.

### E5 - Observability and Operations
- Add custom Prometheus metrics for agent runs/tools/tokens.
- Build Grafana dashboards.
- Add Sentry tags for run/session/request.
- Add backup and restore scripts.
- Add deployment smoke checklist.

### E6 - Product Experience
- Production dashboard for user/agent management.
- Better WhatsApp QR and trial onboarding.
- Agent verification report UI.
- Google auth status UI.
- Preset library for common business agents.

### E7 - Test and Release Gate
- Stabilize full pytest suite.
- Add timeout/failure isolation.
- Add security tests for webhook, SSRF, IDOR, upload size.
- Add SOP gating tests.
- Add end-to-end smoke for Arthur create -> WA trial -> operator escalation.

## Dependencies
- E2 owner auth depends on user/key model decision.
- E3 SOP gating depends on canonical manual schema.
- E4 payment depends on business plan pricing and gateway.
- E5 dashboards depend on custom metrics.
- E6 dashboard depends on API ownership/auth model.

## Estimates
- Documentation baseline: 1-2 days.
- Security critical hardening: 1-2 weeks.
- SOP/runtime correctness: 1 week.
- Quota/payment MVP: 1-2 weeks.
- Observability baseline: 3-5 days.
- Production dashboard MVP: 2-4 weeks.

## Priority
- P0: webhook auth, SSRF, owner auth, quota pre-run, sandbox hardening, SOP tool gating.
- P1: observability dashboards, full test gate, Google MCP smoke, docs index.
- P2: dashboard UX, marketplace presets, connector expansion.

## Owner
- Backend/runtime owner: FastAPI, agent_runner, DB, API.
- Infra owner: Docker Compose, Traefik, Redis, DB, WA services.
- Product owner: PRD, roadmap, plans/pricing, acceptance criteria.
- Security owner: auth, sandbox, webhook, audit.
- AI coding agent: implementation against this docs set and tests.


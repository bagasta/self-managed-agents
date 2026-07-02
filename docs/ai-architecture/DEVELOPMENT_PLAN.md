# Development Plan

Tanggal snapshot: 2026-07-02

## Development Phases
### Phase 0 - Baseline
- Freeze current architecture snapshot in docs.
- Confirm migration head and production runtime status.
- Define release gate.

### Phase 1 - Security and Cost Guardrails
- Add `.dockerignore` and secret scanning.
- Webhook HMAC/shared secret.
- SSRF deny rules.
- Owner authorization.
- Quota pre-run enforcement.
- Sandbox hardening MVP.

### Phase 2 - Agent Reliability
- SOP canonical artifact handling.
- Runtime tool gating by SOP maturity.
- Parent-delivery contract validator.
- Reply guard coverage.
- Google MCP auth/retry smoke reliability.

### Phase 3 - Production Operations
- Metrics and dashboards.
- Backup/restore runbook.
- Staging deploy pipeline.
- Full suite stabilization.
- Incident response checklist.

### Phase 4 - Monetization
- Payment integration.
- Plan upgrade/downgrade flow.
- Top-up reconciliation.
- Cost reports per owner.

### Phase 5 - Product UX
- Dashboard.
- Agent templates/presets.
- Better onboarding and QR/connect flows.
- Connector marketplace.

## Sprint Plan
- Sprint 1: docs, `.dockerignore`, webhook auth, SSRF tests.
- Sprint 2: owner auth, quota pre-run, sandbox cap/hardening tests.
- Sprint 3: SOP artifact and tool gate, parent delivery validation.
- Sprint 4: observability, release gate, migration smoke.
- Sprint 5: payment and dashboard MVP.

## Feature Dependencies
- Owner auth before public dashboard.
- Quota enforcement before paid launch.
- Webhook auth before exposing WA endpoints publicly.
- SOP gating before business agent launch.
- Observability before scaling tenants.

## Priority Matrix
| Priority | Work |
|---|---|
| Urgent/High | security hardening, quota enforcement, SOP gating |
| Urgent/Medium | test stabilization, migration verification |
| High/Not Urgent | observability dashboards, payment |
| Medium/Not Urgent | UI polish, extra MCP connectors |

## Technical Milestones
- M1: all critical security tests pass.
- M2: Arthur can create and verify agent with deterministic SOP readiness.
- M3: quota/cost enforcement blocks over-limit runs.
- M4: production deploy has health, metrics, backups, and smoke tests.
- M5: dashboard can safely manage only owned resources.

## Release Strategy
- Use small, reversible changes.
- Prefer migrations that are backward-compatible.
- Run focused tests for changed subsystems plus release smoke.
- Reseed Arthur after prompt/system-message changes.
- Keep rollback commit/image ready for production deploys.


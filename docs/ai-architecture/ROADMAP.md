# Roadmap

Tanggal snapshot: 2026-07-02

## Current Scope
- FastAPI agent platform.
- Config-driven agent CRUD.
- WhatsApp production and shared trial services.
- Arthur builder workflow.
- Memory, skills, custom tools, RAG.
- Sandbox and subagents.
- Scheduler, escalation, WA media.
- Google Workspace MCP integration.
- Subscription and quota primitives.
- Production Docker Compose.

## Future Features
- Tenant-safe dashboard production UI.
- Payment gateway integration and automated plan activation.
- Strong owner-based authorization on all management APIs.
- Hardened sandbox runtime.
- Full OpenTelemetry tracing.
- Per-agent analytics and cost dashboards.
- Agent marketplace/presets.
- More MCP connectors.
- Rich file delivery workflows.
- Automated QA smoke tests per newly created agent.

## Milestones
### M0 - Documentation and Baseline
- Complete architecture docs.
- Align README/CLAUDE docs with current module paths.
- Define release gate.

### M1 - Security Hardening
- Webhook auth/HMAC.
- SSRF guard.
- Owner authorization.
- Docker image `.dockerignore` and secret scanning.
- Sandbox hardening.

### M2 - Agent Reliability
- Deterministic SOP maturity gating.
- Verify agent readiness signals.
- Fix false success and file-delivery regressions.
- Full test suite stabilization.

### M3 - Monetization
- Payment gateway.
- Plan enforcement.
- Usage billing dashboard.
- Trial-to-paid conversion flow.

### M4 - Production Operations
- Observability dashboards and alerts.
- Backup/restore drills.
- Staging migration workflow.
- Incident runbooks.

### M5 - Platform Expansion
- More connectors.
- Improved frontend.
- Enterprise tenant controls.

## Release Plan
- Patch releases for security and runtime guard fixes.
- Minor releases for new tools/connectors.
- Major releases for API/auth/schema breaking changes.

## Dependencies
- OpenRouter stability.
- WhatsApp service session reliability.
- Google MCP integration service.
- Docker host resource capacity.
- PostgreSQL migration health.

## Prioritization
1. Security and data isolation.
2. Cost/quota enforcement.
3. Agent correctness/SOP compliance.
4. Observability.
5. Product UX and new integrations.

## Risks
- Token/resource cost spikes.
- WhatsApp session instability.
- Prompt injection or false success.
- Sandbox/container host compromise.
- Cross-tenant access if global key is overused.

## Timeline
- Immediate: documentation, critical security backlog, release gates.
- 2-4 weeks: security hardening and test stabilization.
- 1-2 months: payment/subscription automation and observability dashboards.
- 3+ months: connector expansion and enterprise-grade isolation.


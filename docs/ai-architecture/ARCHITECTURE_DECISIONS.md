# Architecture Decisions

Tanggal snapshot: 2026-07-02

## ADR-001 - Config-Driven Agent Model
- Context: User needs to create many agent types without code deploy.
- Decision: Store agent behavior in `agents` table: instructions, model, tools_config, safety_policy, escalation_config, channel metadata.
- Alternatives Considered: hardcoded Python subclasses per agent.
- Pros: scalable creation, Arthur can create/update agents.
- Cons: runtime validation must be strong; misconfiguration can expose tools.
- Consequences: `ToolsConfig` validation and `verify_agent`/SOP checks are critical.
- Future Considerations: add typed profile presets and migration-safe config versioning.

## ADR-002 - OpenRouter as LLM Gateway
- Context: Need multi-model support.
- Decision: Use LangChain OpenAI adapter against OpenRouter.
- Alternatives Considered: direct provider SDKs.
- Pros: one integration, per-agent model choice.
- Cons: provider-specific quirks still leak through; pricing/availability external.
- Consequences: model capability guards needed for image input and token limits.
- Future Considerations: add provider fallback policy and cost-aware routing.

## ADR-003 - PostgreSQL as Source of Truth
- Context: Need durable agent config, users, messages, runs, memory, documents, subscriptions.
- Decision: Use PostgreSQL with JSONB and pgvector.
- Alternatives Considered: separate vector DB and document store.
- Pros: simpler operations, transactional consistency.
- Cons: large vector/search scale may need tuning or dedicated service.
- Consequences: migrations are release-critical.
- Future Considerations: add vector indexes and archival partitions.

## ADR-004 - Per-Agent Runtime Key for Message Execution
- Context: Runtime message endpoint may be embedded or exposed beyond admin clients.
- Decision: `/messages` uses `X-Agent-Key`; management endpoints use `X-API-Key`.
- Alternatives Considered: one global API key for all endpoints.
- Pros: limits blast radius for runtime use.
- Cons: management endpoints still need stronger tenant authorization.
- Consequences: docs and client SDKs must distinguish both auth schemes.
- Future Considerations: per-user auth and ownership enforcement for all management routes.

## ADR-005 - Docker Sandbox for Code/File Tools
- Context: Agents need code execution and file generation.
- Decision: Per-session workspace with ephemeral Docker container per command.
- Alternatives Considered: no execution, local subprocess, remote sandbox provider.
- Pros: strong developer velocity and useful agent capability.
- Cons: Docker socket + root containers are high-risk.
- Consequences: resource cap, cleanup, and security hardening are non-negotiable.
- Future Considerations: rootless Docker, gVisor/runsc, egress proxy, non-root images.

## ADR-006 - Two WhatsApp Services
- Context: Need production dedicated WA and shared demo/trial WA.
- Decision: `wa-service` handles production device per agent; `wa-dev-service` handles shared trial number routing.
- Alternatives Considered: one service for both.
- Pros: clearer operational roles.
- Cons: identity confusion risk.
- Consequences: Arthur must never be routed through `wa-dev-service`; shared trial vCard/contact should be sent from Arthur's dedicated session.
- Future Considerations: explicit runtime identity registry.

## ADR-007 - MCP for External App Integrations
- Context: Google Workspace and future tools need structured external actions.
- Decision: Load MCP tools per agent via `tools_config.mcp`.
- Alternatives Considered: custom hardcoded HTTP tools per service.
- Pros: connector extensibility.
- Cons: auth/scope/token lifecycle complexity.
- Consequences: Google-specific guard code exists to prevent fallback and false success.
- Future Considerations: generic MCP auth registry and capability policy.

## ADR-008 - SOP/Operating Manual as Runtime Contract
- Context: Business agents must follow SOP, not just generic instructions.
- Decision: Store and inject agent operating manual into runtime prompt; apply SOP runtime gate.
- Alternatives Considered: instructions-only prompt.
- Pros: separates business procedure from style/persona.
- Cons: if maturity/gating is weak, agent can still over-act.
- Consequences: `agent_operating_manuals.artifact` and `filter_tools_by_sop` are critical.
- Future Considerations: make action gating fully deterministic by SOP maturity and approval state.

## ADR-009 - Redis Optional
- Context: Local dev should run simply, production needs multi-process coordination.
- Decision: Redis is optional; in-memory fallback for dev.
- Alternatives Considered: mandatory Redis.
- Pros: easy local setup.
- Cons: single-worker assumptions can hide prod behavior.
- Consequences: production compose sets `REDIS_URL`.
- Future Considerations: require Redis in non-development environments.

## ADR-010 - Prometheus Metrics + Structured Logs
- Context: Need baseline observability without heavy stack.
- Decision: expose `/metrics`, structured JSON logs, Sentry optional.
- Alternatives Considered: vendor-only monitoring.
- Pros: flexible and self-hostable.
- Cons: metrics endpoint must be protected in production.
- Consequences: dashboards/alerts still need to be built around exposed metrics.
- Future Considerations: add OpenTelemetry traces.


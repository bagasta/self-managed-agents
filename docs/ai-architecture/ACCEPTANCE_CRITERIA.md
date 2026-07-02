# Acceptance Criteria

Tanggal snapshot: 2026-07-02

## Feature: Agent CRUD
- User Scenario: Admin or Arthur creates an agent.
- Expected Behaviour: Agent is persisted with validated tools_config, unique api_key, owner metadata, quota, and optional WA device.
- Validation Rules: invalid tools_config returns 422; tool_creator without sandbox is rejected.
- Edge Cases: WA service down should not prevent agent creation, but QR should be retrievable later.
- Success Conditions: create/list/get/update/delete/renew endpoints pass tests.
- Testing Requirements: API tests for auth, validation, soft delete, owner filter.

## Feature: Message Execution
- User Scenario: Customer sends message to an agent session.
- Expected Behaviour: Runtime validates agent key, quota, session, locks active run, executes agent, persists run/messages, returns reply.
- Validation Rules: invalid `X-Agent-Key` returns 401; over quota returns 402.
- Edge Cases: `/reset`, interrupted prior run, empty model reply, channel send failure.
- Success Conditions: no duplicate concurrent run per session; reply is non-empty or guarded.
- Testing Requirements: unit/integration tests around locks, quota, reply guard, history.

## Feature: WhatsApp Production Channel
- User Scenario: Agent receives WA message.
- Expected Behaviour: WA inbound maps device to agent/session, runs agent, sends reply via WA service.
- Validation Rules: device must belong to agent; sender allowlist and operator rules must apply.
- Edge Cases: LID vs phone, disconnected device, media, operator envelope.
- Success Conditions: inbound-to-reply path works and cannot spoof without webhook auth.
- Testing Requirements: WA identity, direct send, progress, webhook auth tests.

## Feature: WA Dev Trial
- User Scenario: Arthur offers shared trial number.
- Expected Behaviour: New user agent can be tried through shared `wa-dev-service`; Arthur itself remains on dedicated WA.
- Validation Rules: Arthur must not use `wadev_` device/session as its identity.
- Edge Cases: contact/vCard sharing for shared trial number.
- Success Conditions: shared number contact is sent from Arthur dedicated session.
- Testing Requirements: trial routing and Arthur identity regression tests.

## Feature: SOP / Operating Manual
- User Scenario: Arthur creates business agent.
- Expected Behaviour: SOP artifact is persisted fully and injected into runtime.
- Validation Rules: draft/needs_review blocks high-risk tools; launch_ready requires validated manual.
- Edge Cases: fallback SOP, DB read failure, missing approval points.
- Success Conditions: agent cannot perform irreversible action without mature SOP.
- Testing Requirements: SOP persistence, maturity gate, verify_agent tests.

## Feature: Google Workspace MCP
- User Scenario: Owner asks agent to create/edit Google artifact.
- Expected Behaviour: runtime uses MCP tools, injects token when authorized, returns auth blocker if not.
- Validation Rules: customer sessions cannot mutate owner Google without authorization.
- Edge Cases: expired token, insufficient scope, API disabled, wrong tool args.
- Success Conditions: no false success claim when MCP fails.
- Testing Requirements: live safe smoke and mocked auth/scope failure tests.

## Feature: RAG Documents
- User Scenario: Admin uploads docs and agent answers using knowledge base.
- Expected Behaviour: file is parsed, chunked, embedded, searchable.
- Validation Rules: supported extensions only; size limit should be enforced.
- Edge Cases: empty file, OCR failure, embedding failure fallback to keyword search.
- Success Conditions: search returns relevant chunks and answer cites/uses document context.
- Testing Requirements: upload, search, fallback, size-limit tests.

## Feature: Escalation
- User Scenario: Agent needs human approval or handoff.
- Expected Behaviour: agent calls `escalate_to_human`; operator can reply/send after escalation evidence.
- Validation Rules: `reply_to_user` and `send_to_number` blocked without prior escalation.
- Edge Cases: operator message while run active, wrong operator ID.
- Success Conditions: no accidental direct send without explicit handoff path.
- Testing Requirements: escalation and direct WA send guard tests.

## Feature: Sandbox/Subagents
- User Scenario: Agent generates code/file or delegates specialist task.
- Expected Behaviour: sandbox workspace persists per session; subagents use isolated workspaces/shared output contract.
- Validation Rules: sandbox disabled kill-switch respected; resource caps applied.
- Edge Cases: container image missing, timeout, OOM, parent delivery.
- Success Conditions: generated artifact is returned/sent by parent according to contract.
- Testing Requirements: sandbox path, deploy path, parent-delivery, resource config tests.

## Feature: Subscription/Quota
- User Scenario: User consumes tokens under plan.
- Expected Behaviour: token usage tracked and over-limit run blocked.
- Validation Rules: plan max_agents, token_quota, grace rules.
- Edge Cases: top-up duplicate reference, expired subscription, builder exempt path.
- Success Conditions: cost cannot exceed configured quota without admin action.
- Testing Requirements: quota service, subscription API, token recording tests.


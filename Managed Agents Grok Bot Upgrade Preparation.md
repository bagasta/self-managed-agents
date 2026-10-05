# Managed Agents — Grok Bot-Inspired Upgrade Preparation

**Status:** product brief updated to reflect the owner-orchestrator direction; an initial workforce-task foundation is implemented locally. It is uncommitted and has not passed end-to-end or production validation.
**Prepared:** 25 September 2026
**Target repository:** `/home/bagas/managed-agents-project`
**Current source baseline:** branch `codex/meta-embedded-signup` at `5b3f361` (`feat: add outbound message queue and delivery guards`). The implementation is present as local uncommitted changes, including additive migrations 029 and 030, workforce task/API/runner modules, owner-bound user keys, router registration, and a basic workforce UI. The local database was migrated to revision 030 during implementation. Existing WhatsApp customer routing has not been changed.
**Validation caveat:** syntax/static checks, UI route smoke checks, and OpenAPI inspection were reported successful; end-to-end owner-to-Artur-to-specialist behavior, customer-facing WhatsApp routing, visual acceptance, restart recovery, and production behavior remain unverified. Do not describe the current slice as a complete Grok Bot-style product.

## 1. Executive decision

**The project can move toward the publicly described Grok Bot experience while keeping LangChain Deep Agents and LangGraph.** Deep Agents remains the execution harness. Artur V2 is the default, persistent owner-facing orchestrator for each workspace: it knows the authenticated owner's roster and capabilities, helps design or expand that team, coordinates work, and reports progress/results. It is not merely an agent builder and is not optional for the owner-control lane. The product needs durable workforce state around it: named roles, channel and identity routing, long-running tasks, recorded handoffs, shared-but-scoped work context, an activity timeline, and approval-aware delivery.

“Same as Grok Bot” is treated as a product-parity ambition based on xAI’s public design article, not a claim that Clevio can reproduce Grok’s unpublished internals or proprietary implementation. The design should use Clevio’s own UI, brand, data model, and operating rules.

### Core interaction model

```text
Owner ── dashboard / verified private owner channel ──> Artur V2 (workspace orchestrator)
                                                           ├── knows this owner's roster and capabilities
                                                           ├── designs/recommends team additions with the owner
                                                           ├── assigns durable work and gathers specialist handoffs
                                                           └── reports progress and requests approval for gated actions

Customer ── business WhatsApp ──> designated front-line specialist
                                      │
                                      ├── requests internal help from other specialists
                                      ├── retains ownership of the customer conversation
                                      └── returns one coherent reply to the customer
```

WhatsApp is a channel, not an agent role. The same business may have Artur as its owner-facing control plane and one or more customer-facing specialists, but their verified identities, route bindings, sessions, memories, and permissions must remain distinct. A customer message must never become an Artur owner command merely because the sender's text claims to be the owner.

### Current implementation slice (local)

- The repository contains a persistent workforce task/step/event foundation, owner-scoped workforce API, internal manager-to-specialist dispatch, owner-bound API keys, and a basic `/ui/` surface.
- The current dispatch executes a manager plan followed by one bounded specialist handoff. This provides a foundation for Artur coordinating work; it does not yet implement persistent Artur conversations, natural-language team discovery/provisioning, WhatsApp owner routing, customer-to-specialist routing, cross-run collaboration policy, generic approvals, or daily scheduled reporting.
- The code and migration changes are local and uncommitted. Keep existing customer WhatsApp routing as the default until the separate identity/channel routing and release gates below are implemented and validated.

## 2. Public Grok Bot benchmark

xAI’s [Designing Grok Bot](https://x.ai/news/designing-grok-bot) article publicly describes these product patterns:

| Publicly described pattern | Product requirement for Managed Agents |
|---|---|
| Bots are persistent agents with identity, memory, runtime, and tools; product navigation centers Bots rather than disposable chats | A persistent agent roster with title, role, purpose, channel bindings, capabilities, and visible state |
| Presence shows idle, thinking, working, waiting, blocked, and done; users can inspect the current action | A concise roster/task status surface with an inspectable activity timeline and blocker reason |
| Bots have a workspace and can create durable artifacts | Per-task artifact/result links and an inspectable agent workspace, respecting tenant permissions |
| Responses use structured cards/widgets when prose is not the best representation | Render assignments, approvals, artifacts, handoffs, and task status as structured UI objects |
| xAI says some users create a Chief-of-Staff Bot to coordinate specialists, reducing manual dispatch | Make Artur V2 the default owner-facing orchestrator in Clevio; this is Clevio's product decision, not a claim that xAI mandates this hierarchy |
| Tools/skills can be shared at account level while memory and routines follow role boundaries; group chats provide shared project context | Explicit capability sharing plus least-privilege, tenant/project/task-scoped context; no unrestricted global memory sharing |
| Routines can start work on a schedule or event and report results | Later phase: visible, pausable, auditable routines with bounded permissions and owner controls |

These are observable product design ideas. The article does not establish xAI’s full backend implementation, so no private architecture is assumed.

## 3. Product requirements document

### 3.1 Problem

Managed Agents already supports business agents, WhatsApp conversations, operator escalation, runs, and Deep Agents subagents. The current product model is still centered on configuring and running agents/conversations. Owners need to delegate outcomes to a persistent team, while customers need a stable specialist-facing conversation. Today, transient subagent work does not by itself provide a durable workforce task, cross-run handoff, team activity history, or a unified review surface.

### 3.2 Product outcome

An owner always has one persistent, workspace-scoped front door in Artur V2. Artur can explain the owner's current staff roster and each agent's remit, accept a natural-language outcome, coordinate the right specialists, and report what is working, waiting, blocked, or done. When the roster lacks a capability, Artur discovers the need and proposes an agent/team design—including role, boundaries, required tools or knowledge, and whether any role needs a channel connection—then waits for owner approval before creating/configuring it. A customer instead messages the configured business specialist and never sees or manages internal delegation. Work, results, sessions, memories, and external actions remain attributable, isolated by tenant and role, recoverable, and governed by permissions.

### 3.3 Personas and jobs

- **Business owner / manager:** “Talk to Artur as my persistent front door; let it understand my team, coordinate work, help me shape the team when needed, report progress, and request approval for consequential actions.”
- **Customer:** “Talk to the right business specialist and receive one consistent response.”
- **Front-line specialist:** “Resolve the customer’s request; ask another specialist for bounded help while retaining conversation ownership.”
- **Operator / human reviewer:** “Take over a case or review an outbound draft with enough context to decide.”
- **Platform administrator:** “Configure agent membership, routing, capabilities, integrations, retention, and plan limits safely.”

### 3.3.1 Core journeys

**Daily owner coordination (MVP priority):** authenticate the owner to a workspace → owner gives Artur V2 an outcome in natural language → Artur checks that workspace's existing agents/capabilities and creates one durable parent task with bounded assignments to multiple relevant specialists → an assigned specialist can request one bounded peer-support handoff when it needs another capability → the peer returns a task-scoped result to the requesting specialist and Artur's task timeline → Artur tracks each assignment and consolidates results, conflicts, and owner decisions into a concise report. The owner can inspect the parent task, individual specialist statuses, handoffs, artifacts, and approvals. Use already-configured agent capabilities; this journey does not require new MCP or business-system integrations.

**Team design and channel setup (later increment):** Artur learns goals/workflows → asks focused discovery questions → proposes missing roles, boundaries, tools/knowledge, and channel needs → owner approves the exact design → agent creation/configuration and each channel connection require separate approval → routing preview confirms owner-to-Artur and customer-to-specialist paths before activation.

**Customer service with internal collaboration:** customer messages the business channel → the stored binding selects the designated front-line specialist → that specialist owns the customer session and reply → specialist may request bounded internal help from other agents → helper returns a task-scoped result to the front-line specialist → front-line specialist sends the coherent customer reply or escalates to a human → human takeover pauses bot replies until ownership is explicitly returned. Customer context is not added to Artur's owner conversation or other tenant/agent memories by default.

**Recovery and rollback:** owner sees a blocked/failed task with reason and last successful step → chooses retry, reassign, cancel, or resolve manually → changes are recorded in the event history. Admin disables the new routing flag → new inbound traffic uses the documented legacy route while task history stays available and queued actions are revalidated.

### 3.4 Goals

1. Separate owner control-plane conversations from customer service conversations.
2. Make specialist roles addressable and durable across tasks and sessions.
3. Persist tasks and handoffs independently of a single model run.
4. Keep the front-line specialist as the customer-visible owner when internal collaboration occurs.
5. Show useful progress, blockers, artifacts, and decisions without forcing the owner to read raw traces.
6. Prevent unauthorized access, cross-tenant context leakage, duplicate replies, and unapproved external actions.
7. Preserve existing WhatsApp and agent behavior during migration, with a tested rollback path.
8. Make Artur V2 the default owner entry point in every enabled workspace while keeping customer entry points explicitly bound to specialist roles.
9. Enforce hard tenant, principal, session, and memory isolation. Owner-to-Artur context, customer sessions, specialist-private memory, and task-shared context are separate scopes; sharing requires explicit policy and minimum necessary context.

### 3.5 Non-goals for the first release

- Replacing LangChain Deep Agents, LangGraph, FastAPI, PostgreSQL, or existing WhatsApp providers.
- Reproducing Grok Bot’s private code, visual design, branding, or unknown behavior.
- Unbounded autonomous collaboration, unrestricted group chat, or agents messaging customers whenever they choose.
- Automatic deployment or changes to live customer systems.
- Broad routine/event automation before task persistence, permissions, approvals, and recovery are proven.
- Enabling new WhatsApp owner/customer routing for every existing tenant without opt-in migration and route verification.
- New MCP servers or business-system integrations in the first coordination MVP; it uses the agents and capabilities already configured in the workspace.

### 3.6 MVP functional requirements

**P0 — Agent roster and identity**

- Owner can see agents available in their organization/workspace, each with a stable ID, display name, role, status, permitted tools, and channel bindings.
- Agent role is platform configuration, not inferred from prompt text or a sender’s display name.
- A task can invoke an existing Deep Agents subagent initially; the product must separately record the durable task and assigned role.
- Artur is the stable default orchestrator identity for the authenticated owner/workspace. The owner can ask Artur what agents exist, their assigned work, permitted capabilities, and channel status without switching to an agent-builder screen.

**P0 — Owner-to-Artur path**

- Authenticated owner starts owner work with Artur through the dashboard/API. Verified, explicitly enabled private owner WhatsApp may be an additional entry point; when enabled, it routes to the same workspace's Artur owner session.
- Artur uses the owner's tenant-scoped roster, creates a durable parent task, delegates bounded work to multiple existing specialists, records each handoff/result/blocker, and returns a concise consolidated result/artifact list.
- A single owner request can produce multiple specialist assignments in one task. Each assignment has its own status, assignee, run/result reference, and event history; Artur can sequence dependent work or run independent assignments concurrently under configured limits.
- An assigned specialist may request bounded help from another existing specialist through the task coordinator. Artur remains the top-level orchestrator and owner-facing reporter; peer handoffs do not create a new parent task or change the owner session.
- Peer handoffs are internal and task-scoped: record requester, recipient, reason, minimum context/artifact references, result, and status. The helper returns to the requesting specialist and Artur's task timeline; no peer may contact the owner/customer directly or see unrelated conversations.
- Bound delegation to at most two handoff levels from Artur's parent task (Artur → assigned specialist → one peer helper) and at most eight specialist steps/assignments total per parent task. Reject or mark blocked when a limit is reached; do not silently execute an over-limit request. These are initial product caps and can be lowered by workspace policy.
- Artur compares specialist outputs, identifies missing/conflicting results, and reports the synthesis plus owner decisions needed. It must not present a planned handoff as completed work.
- No new MCP/business-system integration is required for this MVP. Artur may use only the existing roster and capabilities already permitted for those agents.
- Owner can pause/cancel a task and inspect its event history. Retrying a failed step must not silently repeat external side effects.
- Artur can provide an on-demand daily summary of active/completed/blocked assignments and required owner decisions. Scheduled proactive summaries are a later opt-in routine requiring explicit timing, delivery channel, pause controls, and audit history.

**P1 — Customer-to-specialist WhatsApp path (Phase 2)**

- Each inbound business channel binding selects a front-line specialist or explicitly configured router.
- A customer message opens/continues a customer-scoped session with that specialist.
- The specialist may delegate bounded internal work. The designated front-line specialist owns the final customer response unless ownership is explicitly handed to a human.
- If identity/routing is ambiguous, fail closed to the existing safe route or human review; do not guess based on message wording.
- A WhatsApp channel binding identifies the workspace, customer-facing purpose, and one named front-line specialist/router. It does not route ordinary customer messages into Artur's owner session.
- A specialist's internal delegation can return results to the owning specialist/Artur task context but cannot directly create a customer-facing reply or send a new customer message.

**P0 — Tenant, session, and memory isolation (hard acceptance gate)**

- Every Artur conversation, roster query, task, run, artifact, and memory read/write is authorized against the authenticated workspace and owner identity on the server. A caller-supplied workspace ID is never sufficient authorization.
- Owner-to-Artur sessions are distinct from every customer-to-specialist session, even if they share a phone number, model, agent process, or transport provider. Session lookup includes verified route/principal purpose and tenant binding.
- Memory retrieval and writes are scoped by tenant and explicit owner, agent, customer, project, or task boundary. Customer facts are not available to Artur or another customer by default; no unrestricted agent-global or cross-tenant memory is allowed.
- Cross-agent context sharing uses explicit minimum-necessary task summaries/artifacts and is recorded. Full customer transcripts, credentials, unrelated memories, or arbitrary filesystem paths are not passed implicitly.
- Isolation tests must prove that forged workspace IDs, reused external sender IDs, session collisions, and cross-tenant artifact/memory references are denied. Any isolation failure blocks release.

**P0 — Multi-agent task, handoff, and activity**

- A task is a product-level object, distinct from a run. It persists through multiple runs, delegation, waiting, approval, and recovery.
- Record parent/child task relation, assignee, requester, tenant/workspace, status, timestamps, correlation IDs, status reason, and result/artifact references.
- A specialist-to-specialist help request is a child handoff under the same parent task. It must pass coordinator checks for same tenant, active roster membership, allowed role/capability, remaining depth/fan-out budget, and minimum context before dispatch.
- Append task events for assignment, peer-help request/accept/reject, run start/finish, handoff, artifact creation, approval decision, failure, retry, pause, resume, cancel, and completion.
- Keep the initial collaboration bound at maximum two handoff levels and eight specialist steps per parent task. Enforce limits centrally, propagate cancellation, and prevent delegation cycles or a helper from recursively dispatching more work.
- Status lifecycle: `queued → running → waiting/blocked → running → completed`; terminal alternatives include `failed` and `cancelled`. A failed run may be retried under bounded policy.

**P1 — Approval and outbound safety (required before enabling external actions)**

- Any external send or consequential action identified by policy is staged as a reviewable draft/action with recipient, channel, content/payload, source task, and requested permissions.
- Approval is tied to the exact staged payload and authorized reviewer. Editing the payload invalidates the prior approval.
- The delivery boundary enforces approval and idempotency; a prompt instruction alone is not a security control.
- In customer support, ordinary replies may follow tenant policy; promotional/proactive or sensitive actions require explicit approval. The product owner must define the exact action taxonomy before launch.

**P1 — Conversation ownership and human takeover (customer-channel rollout)**

- Existing operator escalation remains available.
- When a human takes over a customer case, bot delivery is paused or clearly suppressed until ownership is returned.
- The timeline shows whether an agent or human currently owns the reply.

### 3.7 Later requirements

- Artur-led workforce design and agent-builder discovery: ask focused workflow questions, recommend missing roles and capabilities, preview the proposed roster, and provision only after owner approval. Agent creation, capability grants, and channel connections remain distinct approval actions.
- MCP servers and new CRM, spreadsheet, ticketing, or other business-system integrations, after the existing-agent multi-agent coordination loop is reliable.
- Proactive scheduled daily summaries and agent-specific routines triggered by schedule or approved events, with visible run history, pause controls, least-privilege tools, and owner-level audit. Keep an on-demand daily summary in the owner coordination MVP; automatic delivery is opt-in later.
- Rich workspace previews, structured task widgets, multi-agent project rooms, configurable availability, and agent-level usage/cost budgets.
- More channel types after WhatsApp roles and identity boundaries are proven.
- Optional workspace status/preview/takeover affordances inspired by xAI’s public design article; evaluate user need and access controls before committing to MVP.

### 3.8 Success metrics to baseline before beta

Do not invent targets without production baselines. Measure and set thresholds during internal testing:

- Correct owner/customer route rate; false owner-route rate (target zero for unauthorized senders).
- Task completion, blocked, failure, retry, and recovery rates; time to resolution.
- Delegation usefulness and excess delegation/cost per completed task.
- Duplicate/stale outbound rate; approval bypass count (target zero); time waiting for review.
- WhatsApp delivery/latency and human takeover collision rates.
- Tenant isolation/security incidents (target zero).

Define formula, denominator, cohort, and observation window before setting targets. Suggested definitions: **owner route correctness** = verified owner events routed to Manager / all verified owner events; **unauthorized owner-route count** = unverified events reaching Manager (target zero); **delegation success** = child tasks with accepted/usable result / child tasks started; **delegation overhead** = added latency and model/tool cost per completed task compared with a non-delegated baseline; **recovery rate** = interrupted tasks resumed to terminal state / interrupted tasks eligible for recovery; **approval wait** = time from pending approval creation to decision. Pair automation measures with resolution quality, escalation appropriateness, and human/bot collision rate. Do not optimize “automation rate” alone.

Keep this proposal as a focused extension/crosswalk to the existing Managed Agents PRD, which already names teams of agents as a later product stage and defines customer-service, operator, and trust outcomes. Preserve those service goals while adding the owner workforce control plane.

## 4. Target architecture

### 4.1 Keep the existing execution stack

Keep FastAPI, LangChain Deep Agents/LangGraph, PostgreSQL/SQLAlchemy, existing model integrations, sandbox, and WhatsApp adapters. Add a **workforce control plane** around Deep Agents rather than replacing its executor. Deep Agents can continue to plan and invoke specialist subagents within a run; the product control plane gives that work durable identity, task state, events, permissions, and recovery.

### 4.2 Component diagram

```text
Dashboard / API / WhatsApp adapters
                 │
                 ▼
        Channel & Identity Gateway
   verify event → tenant/channel binding → authenticated principal
                 │
                 ▼
          Conversation Router
     ┌───────────┴─────────────┐
     │                         │
owner/admin route         customer route
     │                         │
Artur V2                 Front-line Specialist
     │                         │
     └──── Task Coordinator ───┘
              │
     durable Tasks + Events
              │
     Deep Agents / LangGraph runs
              │
   specialist agents + scoped tools
              │
 artifacts / handoffs / approvals
              │
     Approval & Outbound Gate
              │
        WhatsApp dispatcher
```

**Data plane:** existing sessions, messages, and runs plus tenant-scoped tasks, task events, artifact references, approval requests, principal/channel bindings, and outbound delivery state. Each API query and tool call must authorize against tenant/workspace and object ownership.

### 4.3 Conceptual data model

Use existing identifiers/ownership relations where sound; confirm the true tenant/account model before migrations. Suggested entities or extensions:

- `AgentMembership` / `AgentRole`: an agent’s workspace membership, role, manager relationship, capability set, enabled status, and config version.
- `ChannelBinding`: provider + business phone/WABA ID + inbound route + front-line agent + status. Keep provider credentials encrypted and out of task events.
- `PrincipalBinding`: verified external identity (e.g., normalized owner phone) → tenant/workspace + role (`owner`, `operator`, `customer`). Customer access remains conversation-scoped.
- `Task`: tenant, creator, parent task, assigned agent, customer/session reference if applicable, status, priority, budget/deadline if configured, lifecycle timestamps, result summary, idempotency key.
- `TaskEvent`: append-only event type, actor, task/run IDs, safe metadata, timestamps, correlation ID.
- `Artifact`: task/run ownership, type, storage reference, version/hash, visibility, created by agent.
- `ApprovalRequest`: exact action/payload hash, requester, approver, status, expiry, decision reason/time, delivery receipt.
- `ConversationOwnership`: current owner (`agent`, `human`, `manager`), takeover status, and safe resume rules.
- `MemoryScope`: tenant + role/agent + project/customer/task boundary; legacy agent-global memories require explicit compatibility treatment.

Avoid copying entire chat history between agents. Pass the minimum summarized task context and access to explicitly authorized artifacts instead.

### 4.4 Routing and collaboration rules

1. Verify the webhook and resolve `phone_number_id` to a tenant and channel binding.
2. Resolve sender identity to an explicit owner/operator/customer binding; never trust `profile name` or natural-language claims for authorization.
3. Select owner-manager or customer-specialist path from stored binding and role policy.
4. Create/reuse the correct session and durable task correlation. Separate owner sessions from customer sessions even when the same model or WhatsApp integration is used.
5. Front-line specialist can request internal help through bounded task delegation. Specialist results return as structured handoff artifacts, not as direct customer messages by the subagent.
6. Apply conversation ownership and approval rules at outbound delivery. Record provider event/message IDs for deduplication and audit.

If an owner texts the same public business number as customers, identity mapping must still be explicit; the MVP should prefer dashboard/private verified owner route and require deliberate setup before owner commands are enabled on a shared customer number.

### 4.5 Existing-to-target mapping

| Existing capability | Preserve and extend |
|---|---|
| `Agent`, config, tools, safety and escalation configuration | Add workspace membership/role and explicitly bound capabilities where current fields do not express workforce relationships |
| `Session` and `Message` | Keep channel/customer conversation history; add explicit principal role and conversation ownership with strict tenant scope |
| `Run` | Continue tracking individual execution; make it a child execution of durable `Task` rather than treating it as the whole assignment |
| `build_subagents()` | Keep as an executor mechanism; wrap each delegated unit in a persistent task/handoff event when it must survive or be visible across runs |
| Meta/WhatsApp webhook | Preserve validation and media behavior; route through a binding/principal resolver before `run_agent()` |
| Operator escalation | Retain customer human handoff and add explicit pause/resume ownership semantics |
| Outbound queue | Extend with approval linkage, inbound idempotency/correlation, bounded attempts, expiry, delivery outcomes, and ownership check immediately before send |

Evidence anchors in the audited source snapshot: `app/core/engine/subagent_builder.py:496–514`; `app/models/run.py:19–53`; `app/api/runs.py:15–72`; `app/api/meta_webhooks.py:223–240`; `app/models/agent.py` (WhatsApp fields and allowlists); `app/models/memory.py:11–32`; `app/core/domain/outbound_queue_service.py`; `app/models/outbound_message.py`; `app/api/channels.py` (operator escalation). Verify exact line numbers against the chosen implementation base before coding.

## 5. Security, reliability, and policy requirements

- **Tenant isolation:** enforce tenant/workspace authorization in every task, agent, memory, artifact, approval, and channel query; do not rely on UI filtering.
- **Identity:** normalize and verify phone identities; support multiple owner/operator numbers; keep revocation and audit trails; deny by default when mapping is absent.
- **Least privilege:** tools are assigned per role; a front-line service agent should not inherit owner-only administrative tools just because the same Manager can call them.
- **Memory boundaries:** separate agent identity memory, workspace/project context, customer-specific facts, and task scratch data. Define retention/deletion and retrieval permissions before cross-agent memory sharing.
- **Outbound safety:** idempotency key, stale-message expiry, bounded retries/backoff, exact-payload approval, rate limits, and final authorization at send time.
- **Handoff safety:** one reply owner at a time; cancellation of obsolete queued replies when a newer inbound customer turn arrives; safe behavior when an operator takes over.
- **Delegation bounds:** max depth, fan-out, time, tool set, concurrency, token/cost budget, cancellation propagation, and loop detection.
- **Audit/observability:** trace inbound event → identity decision → task → run/handoff → approval → outbound provider ID, while redacting credentials and unnecessary personal data.
- **Resilience:** durable task/event writes before asynchronous work; recover abandoned runs; reconcile queue delivery after worker restart; show blocked/failed reasons and support retry/cancel.

## 6. Delivery roadmap and gates

### Phase 0 — baseline and decisions

- Source branch and commit are reconciled to `codex/meta-embedded-signup` at `5b3f361`; review the current uncommitted diff and confirm the intended deployed version before any release work.
- Confirm account/tenant owner mapping; channel binding model; whether owner uses dashboard, private WhatsApp, or both; human approval taxonomy; memory visibility and retention.
- Draw sequence diagrams and data migrations against the reconciled code.
- **Exit:** approved identity, privacy, route, and action-policy decisions; local implementation scope reviewed; no production route changed.

### Phase 1 — internal MVP: Artur coordinates the existing team

- Current status: additive task/step/event tables, owner-scoped roster/task APIs, dispatch to one specialist, and basic workforce UI are present locally.
- Complete Artur's persistent owner front door and tenant-scoped roster/capability reads.
- Extend orchestration so one parent task can dispatch to multiple existing agents, allow bounded peer-support requests within that task, record each handoff/status/result, recover partial failures, and produce a consolidated owner report.
- Enforce a maximum depth of two handoff levels (Artur → specialist → peer helper) and eight specialist steps per parent task, including direct Artur assignments. Reject/mark blocked at the cap and test cycle, cross-tenant, and over-budget denial.
- Make context passed between agents explicit and minimum-necessary; apply the hard-tested tenant/session/memory isolation gate.
- No new MCP or business-system connections in this phase. Preserve current customer WhatsApp routing; validate it for regressions only.
- Add owner task pause/cancel/retry controls and approvals for consequential external actions. This MVP does not activate new customer messaging routes.
- **Exit:** multi-agent owner-task acceptance criteria below pass in an isolated tenant; results and partial failures are visible; no cross-tenant access; existing customer route remains unchanged.

### Phase 2 — team design and integration readiness

- Add Artur's natural-language agent/team discovery and reviewable proposals for new roles/capabilities.
- Require separate owner approval before creating agents, granting tools, connecting channels, or enabling a new WhatsApp route.
- Design and add MCP/business-system integrations only for approved use cases, with least-privilege scopes and clear per-agent assignment.
- Add customer-to-specialist role routing and verified owner WhatsApp entry behind explicit opt-in flags; preserve legacy route and rollback.
- **Exit:** team/configuration approval and route isolation UAT pass; each connection is bound to a purpose and named role; no customer message reaches Artur's owner session.

### Phase 3 — WhatsApp pilot

- Enable feature flag for one controlled internal/test tenant; shadow-log route decisions before switching.
- Enable verified owner WhatsApp only on an explicitly configured route; retain separate customer route.
- Exercise duplicate/out-of-order inbound, media, provider retry, takeover, and worker restart/recovery paths.
- Add metrics and alerts; define numeric thresholds from observed baseline.
- **Exit:** zero unauthorized routes/actions, rollback verified, tenant boundary review passed, pilot service targets met.

### Phase 4 — opt-in beta and GA readiness

- Small opt-in tenant cohort; publish admin setup and recovery guidance; add support/incident procedures.
- Add routines/events only after outbound approvals, permission model, observability, and pause/disable paths are proven.
- Expand progressively with feature flags and backward-compatible migration policy.
- **Exit:** sustained reliability, cost envelope, privacy/security review, support readiness, and documented rollback.

### Rollback approach

Routing/task data migrations should be additive. A feature flag must restore legacy per-agent channel routing without deleting durable task history. Do not deliver queued messages during rollback until ownership, approval, and message freshness are rechecked. Backfill existing connected WhatsApp agents as customer-facing only; owner-manager routing stays disabled until explicitly verified/configured.

## 7. Acceptance and UAT checklist

1. An authenticated owner request reaches only that workspace's persistent Artur V2 session. The existing customer route remains unchanged during Phase 1; after Phase 2, a verified customer route reaches only its configured front-line specialist.
2. Unauthorized number cannot access owner tools, owner task history, or owner memory; ambiguous mapping fails safely.
3. Customer context cannot leak into another customer, tenant, or owner conversation.
4. Artur can assign one owner task to multiple existing specialists; each assignee, status, run/handoff, result, and artifact is visible; partial success/failure is accurately represented; Artur provides one concise synthesis. An assigned specialist can request bounded help from another existing specialist, whose task-scoped result returns to the requester and Artur's timeline. No helper may message the owner/customer or make an external send through the coordination task.
5. Peer handoffs cannot exceed two levels or eight specialist steps per parent task; a third level, ninth step, cyclic request, inactive/out-of-tenant helper, or unrelated-context request is rejected and recorded without running.
6. Specialist failure/timeout/worker restart leaves a recoverable task state and does not fabricate completion.
7. (Phase 2) Human takeover suppresses conflicting bot replies; return-to-bot is explicit and audited.
8. (Before external actions are enabled) Unapproved gated outbound action is rejected by the delivery boundary; approving exact content permits one delivery; editing it invalidates approval.
9. (Phase 2) Duplicate webhook or retried delivery does not create duplicate customer-visible actions.
10. (Phase 2) Newer inbound turn cancels/reconciles stale queued response; expiry and retry cap work.
11. Existing Meta webhook verification, text/image/document/audio handling, typing indicator, session history, operator escalation, n8n exclusive route, and current customer agent behavior regress cleanly; MVP does not change these routes.
12. Phase 1 cross-tenant access tests cover task, event, artifact, memory, and run; Phase 2 also covers approval and outbound state.
13. Pause, cancel, retry, resume, blocked, waiting, failed, and completed statuses are clear to the owner; audit trail contains actor and timestamp.
14. Usage/cost/latency and delegation depth are observable; limits stop runaway fan-out.

The acceptance checks below have not yet been run end to end. Earlier static/syntax checks and `/ui/` plus OpenAPI smoke checks were reported passing; this does not establish owner/customer routing, memory isolation, or release readiness.

## 8. Risk register

| Risk | Treatment / gate |
|---|---|
| Owner is mistaken for customer or a customer gains owner access | Verified explicit principal mapping, separate sessions, deny-by-default fallback, route audit |
| Shared memories expose customer or tenant data | Define scopes and access checks before enabling cross-agent retrieval; never use global memory as shared project memory by default |
| Duplicate, stale, or conflicting WhatsApp sends | Inbound dedupe, outbound idempotency, expiry, bounded retries, latest-turn guard, single conversation owner |
| Approval bypass through alternate tool or integration | Central policy enforcement in outbound/action service, exact approved payload, immutable audit |
| Agent delegation loops or rising spend/latency | Bound depth/fan-out/time/budget, cancel propagation, task watchdog and alerts |
| Existing n8n or Meta behavior regresses | Preserve exclusive route semantics; feature flags, shadow routing, canary and rollback |
| Inconsistent task state after a crash | Durable transaction/outbox approach; reconcile run and task states on worker start |
| Product scope balloons into “clone all of Grok” | Ship staged parity against public patterns; routines, full group rooms, and elaborate workspace UI follow only when MVP data supports them |
| Local source differs from intended rollout/deployed version | Verify branch, commit, diff, and deployed version before release; do not infer runtime state from local code |

## 9. Decisions required before implementation

These are deliberately recorded rather than guessed:

1. Is the dashboard the required owner control plane for initial rollout, with verified owner WhatsApp as an optional additional channel? (Product direction: Artur is mandatory as the owner orchestrator; dashboard-versus-WhatsApp channel rollout remains a decision.)
2. Can an owner use a verified number on the same business phone-number binding, or must Manager access use a separate private number/channel?
3. Which customer-facing roles can be assigned in v1, and can a router choose among them or is routing always explicit per channel? (Every active customer channel still needs an explicit front-line role binding.)
4. Which customer replies are auto-send, and which actions require approval? Separate routine support replies from proactive, financial, sensitive, or irreversible actions.
5. Which context may specialists share: task summary only, project artifacts, customer conversation excerpts, or broader workspace knowledge?
6. What are retention/deletion requirements for customer sessions, task events, artifacts, and memories?
7. Which source commit is currently deployed, and what rollout mechanism/feature flag controls the owner and customer routes?
8. What AI disclosure should customer-facing agents provide, and how should customers reach a human?
9. Who completes first-run setup (owner, operator, or service provider), and what plan/usage limits constrain roster size, delegation, and model cost?
10. What degree of agent workspace preview/takeover is needed, and what data may appear in that view?

## 10. Implementation ownership and next handoff

- **Solution Architect:** reconcile baseline; finalize sequence/data architecture and API/UI contracts.
- **Automation Builder:** own code implementation after scope and base branch are approved; keep current worktree changes intact; implement migrations, services, routing, tests, and guarded rollout in dependency order.
- **Delivery Ops:** maintain milestones, risk register, UAT evidence, release gates, and rollback readiness.
- **Manager/owner:** decide the product-policy questions above and approve any external communication, deployment, or live routing changes.

## 11. Detailed implementation handoff from staff reviews

The Solution Architect, Automation Builder, and Delivery Ops completed independent read-only reviews. Their findings reinforce the proposed direction and add these implementation details:

### Data and runtime boundaries

- Existing `Agent` is a configurable profile with model/instructions/tools/safety settings and WhatsApp connection fields. On the reviewed branch, `wa_inbound_route` selects the current `ai_staff|n8n` processing path; it is not yet owner-vs-customer role routing.
- `build_subagents()` loads configured system/custom agents as Deep Agents subagents. This is useful for bounded within-run delegation, but source review found no persistent manager-to-specialist task/assignment entity.
- Existing `Run` is one execution record. Create a separate business-level `Task`, with specialist step/assignment records and append-only events; link Run to the task and preserve Run for usage and execution details.
- One audited runner path uses `MemorySaver`; confirm all graph/checkpoint paths before claiming restart-safe persistence. Use a database-backed checkpoint store for tasks that must resume after worker restart.
- Existing `Memory` uniqueness is `(agent_id, scope, key)`; `scope=None` is agent-global. Add a separate explicitly authorized workspace/project/task context namespace rather than using global memory as team memory.
- Existing sessions are keyed around an agent and external sender. Preserve customer histories; resolve the effective agent before creating or selecting a session so owner instructions and customer conversation do not share state.
- Existing scheduled jobs are useful for a later routine feature, but a schedule is not a durable workforce task by itself. Link any later routine run to a task/event and retain pause/cancel/audit semantics.

### WhatsApp route contract

Resolve route **before** creating the agent session and invoking a run:

`verified webhook/account → workspace + ChannelBinding → authenticated sender principal → route purpose → Manager OR named front-line specialist → separate Session → Task/Run correlation → policy-controlled reply owner`

The binding should keep transport credentials and business `phone_number_id` separate from the role/agent route. The sender’s number is not sufficient unless it has been verified and explicitly bound. Existing operator detection and customer testing behavior require a collision rule; being recognized as an operator must not implicitly make every message a Manager command.

### Side-effect delivery contract

- Approval and operator takeover are different workflows. Takeover assigns conversation ownership to a human. Approval authorizes a specific action/payload. Do not reuse one flag/state machine for both.
- Approval should occur before a message/action enters the sendable queue. At dispatch time recheck approver permission, exact payload hash, destination binding, expiry, latest conversation turn, and idempotency key.
- Model real delivery as at-least-once plus provider reconciliation unless the WhatsApp provider guarantees an idempotency key. A worker crash between provider acceptance and DB update can otherwise resend.
- Queue state should include attempt count/cap, expiry, provider message ID, last error category, and a recoverable `sending` lease/dead-letter path. The present queue is a useful base, not generic approval enforcement.

### Suggested resource and service decomposition

1. Add tenant/workspace-scoped roster/role configuration and `ChannelBinding`/`PrincipalBinding`; backfill legacy connected agents to their existing customer-facing route.
2. Add `Task`, `TaskStep`/assignment, `TaskEvent`, artifact metadata, and run/task correlation. Persist task before asynchronous dispatch.
3. Add a bounded Task Coordinator/worker around Deep Agents. Use Deep Agents subagents for same-run work; use explicit persisted child task/run handoffs when work must be visible, resumable, cancellable, or cross-run.
4. Add scoped context/artifact handles and least-privilege tool grants. Do not pass arbitrary local file paths or full customer histories to specialists.
5. Add generic action/approval services and enforce them centrally at external tool/channel boundaries, not only in final WhatsApp response code.
6. Add task and approval APIs and a dashboard surface for roster/presence, event timeline, status reason, artifact review, task control, approvals, and conversation ownership.
7. Reuse existing worker infrastructure where suitable; first confirm worker/queue topology rather than creating a second job system by default.

Potential API resources are sketched in §4.6. API shape and module ownership must be checked against the actual auth, workspace, UI, and plan-entitlement conventions before an implementation ticket is finalized.

### Release gates

- **Phase 1 MVP gate:** persistent Artur owner entry, one parent task delegated to multiple existing agents, bounded specialist-to-specialist help within that task, individually visible handoffs/status/results, concise synthesis, owner controls, tenant/session/memory isolation, and regression of the unchanged legacy customer route. No new MCP/business-system integrations or customer routing changes.
- **Phase 2 gate:** owner-approved team/agent provisioning, explicit customer-specialist WhatsApp routing, central approval for gated outbound actions, human takeover, route collision tests, and integration permission review.
- **Pilot gate:** shadow route audit, internal test identities, duplicate/out-of-order webhook scenarios, restart/recovery, delivery reconciliation, metrics and tested rollback.
- **GA gate:** opt-in rollout, support/runbook ownership, retention policy, bounded cost/latency, incident and recovery procedures; enable routines only after the safety and task layers are stable.

The existing SSE/event bus has a noted single-worker constraint unless Redis is configured. Use persisted task events as the source of truth; verify deployed event transport before promising cross-worker live progress.

The AI Product Architect review additionally recommends treating this document as an extension to the existing PRD. First-run team design, routing preview, recovery choices, explicit customer AI disclosure/human access, and formula-based metrics remain product requirements/discovery. xAI's Chief-of-Staff example is described as an emergent pattern used by some users; making Artur the default orchestrator is Clevio's explicit product direction. Workspace computer status/preview/takeover is a possible later experience, not an MVP assumption.

### Current source cross-check (25 September 2026)

- Verified source baseline: branch `codex/meta-embedded-signup` at `5b3f361`. Local uncommitted files include workforce tables/API/runner, owner-key binding, router registration, and a basic UI. Migration files 029 and 030 are present; the local database was reported at revision 030 during the implementation session.
- Current source supports owner-scoped roster/task APIs and one manager-selected specialist dispatch with recorded runs/messages/events. The internal workforce runner uses a separate internal session channel. These are implementation foundations only; the code does not yet establish a persistent conversational Artur, multi-specialist fan-out/handoff aggregation within an owner task, agent recommendation/provisioning flow, new WhatsApp role routing, generic approval enforcement, or proactive scheduled daily reporting.
- Static checks and UI/OpenAPI smoke checks were reported passing. End-to-end behavior, isolation adversarial tests, restart recovery, visual acceptance, and production state have not been verified. Keep these as release gates; do not infer deployed behavior from local source or a migration.
- The working tree is dirty by design. Preserve all existing edits and the `pre-grok-bot-upgrade` stash; do not commit, push, deploy, send WhatsApp messages, or alter live routing under this brief.

## References

- xAI, [Designing Grok Bot for a world of persistent agents](https://x.ai/news/designing-grok-bot), 3 September 2026.
- LangChain, [Deep Agents overview](https://docs.langchain.com/oss/javascript/deepagents/overview) — describes the SDK as an agent harness with task planning, specialized subagents, persistent memory, filesystem/context tools, and human-in-the-loop workflows. Framework capability does not replace this product’s tenant/routing/task schema.
- Managed Agents source anchors listed in §4.5 refer to the reviewed source baseline. Recheck relevant line numbers and deployed version before release decisions.

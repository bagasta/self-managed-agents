# Managed Agents: Goals and Project Status

**Updated:** 29 September 2026
**Project:** Managed Agents / Arthur V2
**Source repository:** `/home/bagas/managed-agents-project`
**Status:** The working tree contains substantial local, uncommitted changes. This document describes the intended product and the implementation status evidenced so far; it does not claim a production release or complete Grok Bot parity.

## 1. Product goal

Build Managed Agents into an AI staff platform where **Arthur V2 is the business owner's manager and main point of contact**, not just an agent builder. The owner should be able to explain a business goal in ordinary language. Arthur should understand the need, know the staff roster and available capabilities for that owner's workspace, propose or create suitable specialists, coordinate work, follow progress, and return a grounded combined result.

The product takes publicly described Grok Bot behaviors as a design reference: persistent named specialists, manager-led delegation, progress and result reporting, and a shared team-work context. The goal is a comparable ease of use for Managed Agents; Grok's unpublished internals are unknown and are not being claimed as replicated.

## 2. Intended operating model

1. **Owner to Arthur:** Each owner/workspace has a stable, private Arthur relationship. Arthur understands that workspace's business, roster, prior work, and permissions.
2. **Team design:** When the roster is missing a role, Arthur explains what is needed and proposes a specialist's remit, boundaries, tools/data needs, and whether the specialist should connect to a channel. Consequential setup or external connection remains subject to owner approval.
3. **Delegation:** Arthur creates a durable task and assigns bounded work to the appropriate specialists. Specialists can ask another authorized specialist for bounded help within the same workspace and task.
4. **Progress and reporting:** Specialists report actual execution progress and results to Arthur. Arthur tracks the work, handles blockers, and returns one concise, evidence-based response to the owner. Progress must come from execution events, not invented status narration.
5. **Customer conversations:** Customers can reach a designated customer-facing specialist, for example through WhatsApp. That specialist owns the customer reply and may request internal help. Customer conversations do not become owner commands to Arthur.
6. **Channels:** WhatsApp is a channel, not the manager. The owner-facing and customer-facing identities, routes, sessions, permissions, and memories must remain distinct. A specialist should only be connected to WhatsApp when the owner chooses that setup.
7. **Isolation:** Workspace roster, task context, memory, and artifacts are isolated by stable owner identity. One owner's data must never be available to another owner or customer by default.

## 3. Work completed or present in the local working tree

These items are present in the repository's local working tree. The changes have not been committed or deployed as part of this status update.

### Workforce and orchestration foundation

- Added persistent workforce tasks, assigned steps, statuses, and task events, with owner-scoped API routes and service logic.
- Added Arthur V2 workforce tools for reading a workspace roster and dispatching owner tasks to specialists.
- Added bounded, same-owner specialist-to-specialist help inside a task. This is controlled handoff, not unrestricted agent-to-agent chat.
- Arthur can queue a specialist team in parallel and return a task ID immediately; each specialist step persists queued/in-progress/completed/blocked state and result events in its own database session.
- Arthur can query recent workforce tasks and step summaries, read/post task team messages, cancel work, and retry blocked steps when requested. Progressive skill filtering preserves these tools.
- Dispatch commits the task and assignments atomically. A PostgreSQL-backed consumer finds persisted work, and a transaction advisory lock prevents simultaneous consumers from executing the same task. The consumer starts with the API and can also run independently with `python -m app.workforce_worker`.
- Restart recovery retains completed results and executes queued assignments. Interrupted in-progress steps become blocked for review rather than automatically replaying external effects. Explicit retry queues blocked root assignments and keeps completed results.
- Specialists can read shared messages and completed peer results, and post findings/questions to their task. These tools do not grant new external capabilities. The selected Workforce timeline receives server-sent snapshots, with polling retained as fallback.
- Added worker execution progress events for run start/activity/result/failure/block conditions. The recorded progress uses bounded metadata and avoids persisting prompts, tool arguments, and tool results in those event records.
- Bound workforce identity and access to a stable owner/user principal instead of treating a phone number as the workspace identity.
- Added local migrations for the workforce task control plane and owner binding (`029`–`031`). Database head and production migration state must be checked independently before release.

### Arthur session behavior and local Enterprise test plan

- The Arthur Manager UI creates an isolated local test session using the reserved Enterprise test owner and Enterprise test markers.
- The generic **Chat / Messages** new-session flow now also detects Arthur V2 and applies those reserved markers automatically. Other agents retain their normal External User ID prompt and request payload.
- The backend provisions the reserved Enterprise test owner only in development/test and gives that test owner an Enterprise entitlement with no agent-count ceiling. It does not upgrade a real owner's plan.
- Sessions created before this path was corrected remain Trial; the change applies to newly created Arthur sessions and does not retroactively modify old sessions.

## 4. Verification recorded

The earlier follow-up passed syntax checks only. The subsequent durable-worker implementation passed **22 focused tests**, including integration coverage using an isolated temporary PostgreSQL instance and deterministic specialist outputs: concurrent execution, exclusive task claims, shared messages, recovery, retry preserving completed results, cancellation, and discovery by a fresh consumer. This does not establish a real LLM/browser/deployment acceptance result. The local Docker socket was unavailable during this check, so no sandbox deployment scenario was run.

Fresh focused checks for the current local changes passed on 29 September 2026:

- Backend/session lifecycle, Enterprise entitlement, and Arthur memory-scope tests: **8 passed**.
- Chat / Messages and Arthur session lifecycle Node harness: **6 passed**, including Arthur Enterprise-marker behavior and unchanged non-Arthur behavior.
- `git diff --check`: passed.

These checks verify code and test-harness behavior. They do **not** verify a fresh browser-created Chat / Messages session against the live local database, a full corrected Dimsum workflow, or production deployment. The screenshot showing Trial is consistent with an older Trial session; a new Arthur session still needs direct UI confirmation after reload.

Prior local UI work demonstrated that Arthur could create two specialists, record two work steps, and receive two worker reports. That scenario also surfaced quality issues: a specialist classified stock status without sufficient supporting data, and Arthur retried agent creation. Prompt/contract protections and regression coverage were subsequently added, but a successful end-to-end rerun after those corrections has not been established here.

## 5. Current gaps and release risks

- Durable task execution and task-scoped conversation are implemented locally. Arbitrary group chats, automatic waking of a finished specialist when another agent messages it, and shared persistent browser/computer sessions are still absent. An interrupted tool execution is not automatically resumable at the instruction level.
- The Workforce UI receives server-sent task snapshots while the selected task is active. Guaranteed owner notifications outside this view and server-side stream revocation mid-connection are not implemented.
- Peer collaboration is bounded task handoff, not free-form private messaging between every specialist.
- The browser-visible roster/activity workspace and direct private chat with each specialist are not yet equivalent to the desired team experience. The user has prioritized reliable backend workflow before UI redesign.
- MCP and live business-system integrations are deferred. Workers cannot claim to read a real CRM or spreadsheet, deploy to Cloudflare, or execute WhatsApp actions unless the corresponding tool, credentials, permissions, and successful result are actually available.
- A complete synthetic business acceptance run must verify correct staffing, task assignments, real reports, calculations against supplied dummy data, honest handling of missing data, progress events, no duplicate agent creation, and tenant isolation.
- The generic Chat / Messages Enterprise path needs a fresh browser test. Confirm a new Arthur session is Enterprise, confirm a non-Arthur session remains unchanged, and leave the old Trial session intact unless separately requested.
- The local code is uncommitted and not deployed. Production routing, outbound actions, and live customer systems remain outside the verified scope.

## 6. Recommended next steps

1. Reload `/ui/` and create a **new** Arthur session from Chat / Messages. Verify the session uses the isolated Enterprise test owner; do not infer the plan from an old session.
2. Verify that creating a non-Arthur session still uses its ordinary identity prompt and entitlement behavior.
3. Run a full synthetic owner scenario with fabricated business data. Check agent roster, task and step rows, progress events, specialist reports, final arithmetic, and behavior when a needed fact is absent.
4. Fix any remaining quality or dispatch defect from that run before calling the Arthur workflow ready for owner testing.
5. Complete shared persistent computer/browser access, general group conversations and message-triggered agent turns, and external owner notification delivery before claiming full Grok Bot parity.
6. Review migrations, isolation, permissions, and actual runtime capabilities; then decide separately what is safe to commit or release. No production deployment is recorded here.

## 7. Reference document

The more detailed PRD, benchmark, target architecture, migration plan, and acceptance criteria are in [Managed Agents Grok Bot Upgrade Preparation](Managed%20Agents%20Grok%20Bot%20Upgrade%20Preparation.md).

Behavioral reference rechecked on 29 September 2026: [Grok Bot overview](https://docs.x.ai/grok-bot/overview). This is a behavioral comparison to public documentation, not a claim to reproduce proprietary implementation.

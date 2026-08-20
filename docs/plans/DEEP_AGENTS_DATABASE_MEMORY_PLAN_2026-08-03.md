# Deep Agents Database-Backed Memory Plan

**Date:** 2026-08-03  
**Status:** Proposed — no migration or runtime replacement has been applied by this document  
**Scope:** All customer agents and Arthur; Deep Agents `0.6.12`

## Objective

Give every agent persistent, isolated, compact memory without making Docker a
source of truth. The implementation must use the official Deep Agents memory
contract (`memory=["/memories/AGENTS.md"]`) for agents that run the Deep Agent
graph, while Arthur and other non-sandbox agents use the same canonical memory
through their normal system prompt.

This is a context-engineering change. It must not delete chat history, silently
rewrite final replies, or introduce a post-response “guard” that waits for the
user before continuing work.

## Decision summary

| Concern | Decision |
|---|---|
| Canonical storage | PostgreSQL, using the existing `agent_memories` and `sessions.metadata` data stores. |
| Deep Agent interface | A virtual Markdown path: `/memories/AGENTS.md`. |
| Docker / sandbox | Workspace and task artifacts only. Never canonical memory. |
| Isolation | Agent-global: `agent_id`; user: `agent_id + external_user_id`; task state: `agent_id + session_id`. |
| Arthur | Receives the same rendered memory in its React-agent system prompt; no Docker dependency. |
| Conversation history | Retained for audit. Raw history is no longer the primary durable memory projection. |
| Reads and writes | One bounded DB read at run start; write only when memory/state changed, in the same transaction as the run/session update. |

## What “virtual file” means

`/memories/AGENTS.md` is a path the agent can read, but it need not exist on a
container filesystem. A database-backed backend translates the path to a
PostgreSQL query and returns generated Markdown:

```text
Deep Agent reads /memories/AGENTS.md
              │
              ▼
DatabaseMemoryBackend (virtual path adapter)
              │
              ▼
agent_memories + sessions.metadata in PostgreSQL
```

For a sandbox-enabled agent, the backend is composed as follows:

```text
/memories/*       → DatabaseMemoryBackend → PostgreSQL
/workspace/*      → DockerBackend         → isolated task container
```

Docker may be restarted, replaced, or absent without losing memory. The memory
adapter must not download a Markdown file from Docker at the start of a run or
upload it there after a run.

## Existing data to preserve and normalize

The project already has the right canonical primitives:

- `agent_memories`: `agent_id`, `scope`, `key`, `value_data`, timestamps.
- `sessions.metadata`: session-specific transient state such as the current
  summary and active workflow.
- `messages`: immutable conversation/audit history.

The first implementation should **not** add a competing memory table. Instead,
it should define a stable namespace convention over `agent_memories` and only
add a migration if usage shows the current `scope` column needs a distinct
session namespace.

### Memory layers and keys

| Layer | Database owner | Examples | Included in AGENTS.md |
|---|---|---|---|
| Agent identity | `agent_id`, `scope=NULL` | `soul`, `agent_context_version`, versioned `soul:vN` | Yes |
| User durable preferences | `agent_id + external_user_id` | `user_profile`, `longterm` | Yes |
| Recent user context | `agent_id + external_user_id` | `active_context`, `last_turn`, daily keys, artifact references | Yes, bounded |
| Current task/session | `sessions.metadata` for its exact session | `context_summary`, `active_google_pdf_workflow`, task phase, verified artifacts | Yes, bounded |
| Raw transcript | `messages` | all inbound/outbound/internal trace records | No, except a small recent turn window |

`scope` must always be the resolved external-user identifier, never a shared
default. Agent-global rows are deliberately `scope=NULL`; they may contain the
agent’s identity and operating policy, but never one user’s information.

## Rendered AGENTS.md contract

The renderer is a pure, shared function. It accepts a fully scoped memory
snapshot and emits bounded Markdown. It must be used by both runtime paths,
not duplicated in separate prompts.

Example structure:

```md
# Operating memory

## Agent identity
...

## User profile
...

## Durable facts and preferences
...

## Current task
- Status: PDF report is in progress.
- Verified source: Google Spreadsheet `...`.
- Verified output: none yet.
- Next action: create a Google Doc, export it as PDF, then return the artifact.

## Recent context
...

## Reliability rules
- Treat prior assistant progress claims as unverified unless an artifact/tool result is recorded.
- Continue an active task; do not ask for acknowledgement merely to resume it.
- Use only tools available in this runtime.
```

Hard limits must be applied per section and to the total document. Large input,
file body, token, and tool trace data are represented by references and a short
verified fact, never copied verbatim. The task section wins over older general
memory when both conflict.

## Runtime flow

```text
1. Resolve agent_id, external_user_id, session_id and runtime policy.
2. Read one scoped memory snapshot from PostgreSQL.
3. Render bounded /memories/AGENTS.md.
4. Assemble a compact recent-turn window and active tool set.
5. Invoke the agent.
6. Persist tool evidence, session task state, and only changed durable memories.
7. Deliver the terminal result; intermediate tool-call text is not a terminal reply.
```

### Deep Agent path

1. Use a composite backend that routes `/memories/` to
   `DatabaseMemoryBackend` and `/workspace/` to `DockerBackend` when a sandbox
   is authorized.
2. Configure `create_deep_agent(..., memory=["/memories/AGENTS.md"],
   backend=composite_backend)`.
3. Confirm the adapter implements the backend methods Deep Agents `0.6.12`
   actually calls for memory loading; do not implement against a newer `0.7`
   API without upgrading and testing first.
4. Keep task artifacts in `/workspace/`; only save artifact metadata and stable
   references in database memory.

### Arthur / non-sandbox path

1. Load the identical scoped snapshot and call the same Markdown renderer.
2. Append the rendered content to Arthur’s compact system context.
3. Keep Arthur’s existing control-plane tool policy: no sandbox and no customer
   agent execution tools.
4. Never instantiate a Docker backend simply to provide memory.

## Conversation context policy

The present failure mode was caused by a stale summary plus many old assistant
progress messages being replayed as if they were reliable context. The new
policy is:

1. Keep every message row for audit and support investigation.
2. Pass only a bounded recent turn window to the model.
3. Exclude internal tool-call trace/progress content from the user-facing
   transcript projection. It remains in the database and run trace.
4. Store a session summary that is generated from the newest relevant window,
   not the oldest 6,000 characters of the entire chat.
5. Include unresolved task, verified inputs, verified artifacts, and next action
   in the session task section.
6. A model statement such as “I will generate the PDF” is never evidence that a
   PDF exists. Only successful tool output/artifact metadata can mark it done.

This prevents old “I am generating it” text from repeatedly becoming the next
answer while preserving the original records.

## Write policy and performance

PostgreSQL is contacted at the beginning of every agent run because the agent
needs the correct per-user/per-agent state. That is a small, indexed read—not a
full transcript scan or Docker file transfer.

- Fetch layered memory and session metadata in one bounded repository/service
  call where feasible.
- Cache the resulting snapshot in graph state for the rest of that invocation.
- Do not write on every token or every tool step.
- Upsert only when a durable fact changes; update session metadata only when
  task/summary state changes.
- Keep writes in the existing request/run transaction so an unsuccessful run
  cannot claim a nonexistent artifact.
- A future Redis cache may cache rendered snapshots, but invalidation must key
  on `(agent_id, scope, session_id, memory_version)` and PostgreSQL remains the
  source of truth.

## Rollout phases

### Phase 0 — Baseline and contracts

- Add a `MemorySnapshot` data object and a single Markdown renderer.
- Document max character/token budgets per section.
- Add structured run fields for `memory_snapshot_version`, `memory_chars`, and
  source section sizes; never log protected memory values.

### Phase 1 — Read-only shadow projection

- Build the database snapshot and Markdown projection without changing the
  prompt used in production.
- Compare it with existing prompt memory on representative Arthur and customer
  agent sessions.
- Verify hard isolation by testing two agents and two external users with the
  same key names.

### Phase 2 — Use shared projection

- Enable the renderer in Arthur/non-sandbox system contexts.
- Enable it for Deep Agents through the database virtual backend.
- Remove the current Docker upload of generated `AGENTS.md`; it is only a
  temporary compatibility bridge and must not remain a second source of truth.

### Phase 3 — Context reliability

- Stop projecting old internal assistant/tool traces as ordinary dialogue.
- Use the corrected tail-based session-summary input and persist explicit task
  evidence.
- Validate the Google Sheets-to-PDF workflow: no Python/sandbox route when the
  agent has only Google Workspace tools, and no terminal progress claim before
  a verified export exists.

### Phase 4 — Observability and gradual rollout

- Feature-flag per agent/runtime path.
- Start with internal test agents and a fresh test session; do not erase existing
  sessions.
- Measure prompt size, tool-call completion, artifact-verification rate,
  accidental cross-scope reads, and false “in progress” terminal replies.
- Roll back by disabling the projection flag; stored memory remains intact.

## Acceptance criteria

- A user’s memory cannot be read by a different `agent_id` or external user.
- Arthur has durable memory without a Docker sandbox.
- A sandbox agent can restart its container and retain memory.
- Deep Agents load `/memories/AGENTS.md` through the database backend, verified
  with the installed `0.6.12` integration tests.
- Every run reads a bounded snapshot; no full history is injected by default.
- Tool-call progress text cannot be delivered or replayed as a completed task.
- A PDF task stays active until a verified output artifact is recorded.
- Existing transcript rows are preserved throughout rollout.

## Test matrix

1. **Isolation:** same user, two agents; same agent, two users; two sessions for
   the same user. Assert only allowed layers appear.
2. **Arthur:** verify no `DockerBackend` construction and the shared renderer is
   present in the model context.
3. **Sandbox agent:** verify `/memories/AGENTS.md` reads from PostgreSQL and
   `/workspace/` reads/writes only inside the task sandbox.
4. **Restart:** replace the sandbox container and verify memory is unchanged.
5. **Transcript contamination:** seed old static progress rows; verify they are
   retained in DB but absent from the compact model transcript.
6. **PDF regression:** spreadsheet discovery → source read → document creation
   → PDF export → verified artifact reply, without an acknowledgement turn.
7. **Failure semantics:** failed export keeps task state unresolved and returns a
   truthful failure/retry result, never a fabricated completion.

## Non-goals

- Do not migrate all historical messages into durable facts automatically.
- Do not let agents freely write arbitrary private data into global memory.
- Do not use Docker volumes as a memory database.
- Do not replace model/tool orchestration with static reply templates.

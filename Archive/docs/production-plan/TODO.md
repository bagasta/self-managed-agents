# Platform Evolution TODO

Derived from architecture assessment (`10-update.md`) + Deep Agents SDK docs review.

Legend: `[x]` done · `[ ]` pending · `[~]` in progress · `[!]` risky (test carefully)

---

## Phase 1 — Immediate Fixes (1–3 days)

Critical bugs, zero behavior regression on happy path.

- [x] **1.1** Wrap `DockerSandbox.bash()` + `close()` in `asyncio.run_in_executor()`
  - File: `app/core/sandbox.py`
  - Fix: blocking docker SDK freezes event loop on every sandbox call
  - Label: Safe refactor
  - Test: concurrent requests don't stall during bash execution

- [x] **1.2** Add `asyncio.timeout()` around `graph.ainvoke()`
  - File: `app/core/agent_runner.py` (step 10)
  - Fix: `AGENT_TIMEOUT_SECONDS` documented but not enforced → hung LLM/Docker call leaks DB connection + handler forever
  - Label: Risky (intentional behavior change — verify timeout value is generous enough)
  - Test: mock slow `ainvoke` → must raise `TimeoutError`; real long runs must not be cut

- [x] **1.3** Add `ToolsConfig` Pydantic schema + validation on create/update
  - Files: new `app/core/config_schema.py`; `app/api/agents.py`
  - Fix: bad `tools_config` JSON fails silently at runtime, minutes into a run
  - Fields: `sandbox`, `tool_creator`, `scheduler`, `http`, `rag`, `mcp`, `subagents`, `deploy`; `model_config extra="allow"`
  - Label: Safe refactor
  - Test: invalid key → 422 at `POST/PATCH /v1/agents`

---

## Phase 2 — Short-term Stability (1–2 weeks)

Safe structural improvements, no external behavior change.

- [x] **2.1** Extract `result_parser` from `run_agent()`
  - Files: `app/core/engine/agent_runner.py` → `app/core/engine/result_parser.py`
  - Done: `parse_agent_result()`, `ensure_tool_messages_complete()`, `sanitize_input_messages()` extracted as pure functions; agent_runner imports and delegates
  - Label: Safe refactor
  - Test: unit test with fixture LangGraph output → assert correct DB records

- [x] **2.2** Fix DB session lifetime in tool closures
  - Files: `app/core/engine/tool_builder.py`, `app/core/tools/rag_tool.py`, `app/core/engine/agent_runner.py`
  - Fix: live `AsyncSession` captured in closures, executed minutes later → potential leaks/corruption; pass `AsyncSessionLocal` factory instead, each tool call opens+closes its own session
  - Done: all `build_*_tools()` now accept `db_factory: async_sessionmaker`; `build_wa_agent_manager_tools` migrated from `get_db()` generator to factory; `build_rag_tools` migrated from raw `AsyncSession` to factory
  - Label: Risky (changes transaction boundary — safer overall)
  - Test: memory/skill writes no double-write, no data loss under concurrent load

- [x] **2.3** Add session-level run lock
  - Files: new `app/core/engine/session_lock.py`; `app/api/messages.py`; `app/api/channels.py`
  - Done: per-session `asyncio.Lock` with 120s timeout + 5min eviction; applied to all 3 `run_agent()` call sites (messages, incoming channel, wa_incoming)
  - Label: Safe refactor
  - Test: two simultaneous requests on same `session_id` → second must queue, not race

- [x] **2.4** Add concurrency limit to scheduler tick
  - File: `app/core/workers/scheduler_service.py`
  - Done: `asyncio.Semaphore(5)` via `_run_job_guarded()` wrapper; `_tick()` now awaits `asyncio.gather()` instead of fire-and-forget `create_task`
  - Label: Safe refactor

---

## Phase 3 — Structural Cleanup (2–4 weeks)

Reduce `app/core/` flat-file sprawl, add observability primitives.

- [x] **3.1** Add run status to DB
  - Files: new `app/models/run.py`; alembic migration `19582ffb2441_add_runs_table.py`; `app/core/engine/agent_runner.py`; `app/api/runs.py`
  - Done: `Run` model with `status` (pending/running/completed/failed/timed_out), `started_at`, `completed_at`, `error_message`, `tokens_used`. Created at run start → updated at every exit (success, timeout, exception, dangling tool call). GET `/v1/runs/{run_id}` now returns status metadata.
  - Label: Safe refactor
  - Test: status transitions correct on success, timeout, and exception

- [x] **3.2** Move `_SYSTEM_SUBAGENTS` to DB-seeded records
  - Files: `app/core/engine/subagent_builder.py`; new `scripts/seed_system_agents.py`; `Makefile` (seed-agents target)
  - Done: `build_subagents()` loads `Agent` rows with `is_system_agent=True` from DB first, falls back to hardcoded `_SYSTEM_SUBAGENTS` if none found. Seed script upserts all 6 system agents. Run `make seed-agents` to populate.
  - Label: Intentional product change (behavior-compatible)
  - Test: all 6 system agents load from DB, same behavior as before

- [x] **3.3** Split `app/core/` into domain sub-packages
  - Move: infra → `core/infra/` (wa_client, sandbox, deployment_service), domain → `core/domain/` (memory, skill, custom_tool, document, embedding), runtime → `core/engine/` (agent_runner, tool_builder, prompt_builder, subagent_builder, context_service), background → `core/workers/` (scheduler_service, event_bus)
  - Label: Safe refactor (mechanical `git mv` + import rewrites, one PR)
  - Test: all existing API endpoints must respond identically after move

---

## Phase 4 — Agent Builder Foundation (1–2 months)

First-class `AgentBuilder` system agent.

- [x] **4.1** Seed `AgentBuilder` as system agent in DB
  - `is_system_agent=True`, `tools_config` includes builder tools only
  - System prompt: see `system-message-builder.md`
  - No sandbox by default

- [x] **4.2** Implement builder tools
  - File: `app/core/tools/builder_tools.py`
  - Tools: `draft_agent_config(spec)`, `confirm_and_create_agent(draft)`, `list_available_capabilities()`, `validate_agent_config(config)`
  - Draft flow: always produce draft first → show to user → user confirms → create via existing `POST /v1/agents`
  - Safety: `is_system_agent` always `false` in output, sandbox default `false`

- [x] **4.3** Capability profile model
  - File: `app/core/config_schema.py` (extend `ToolsConfig`)
  - Define named profiles: `assistant`, `support`, `research`, `knowledge`, `ops`, `builder`
  - Each profile = preset `tools_config` + default model + safety policy
  - `AgentBuilder` maps user intent → profile → concrete config

- [x] **4.4** Instruction template library per agent class
  - File: `app/core/builder/templates.py`
  - One base instruction template per class (support, research, ops, etc.)
  - `AgentBuilder` starts from template, customizes based on user input

---

## Phase 5 — Privileged Agent & RBAC (1–3 months)

Harden system agent access, enable horizontal scaling.

- [ ] **5.1** Proper RBAC for `is_system_agent`
  - Replace boolean with `capabilities: set[str]` on agent model
  - Gate builder tools on capability, not boolean flag
  - Audit log all agent-mutating tool calls
  - Alembic migration required
  - Label: Intentional product change (security hardening)

- [ ] **5.2** Replace in-process scheduler with task queue
  - Stack: `arq` + Redis (or Celery)
  - Scheduler tick → enqueue only; workers pull and execute
  - SSE stream behavior unchanged to client
  - Label: Intentional runtime change (enables horizontal scaling)

- [ ] **5.3** Replace in-memory event bus with Redis pub/sub
  - File: `app/core/event_bus.py` → Redis adapter
  - SSE stream behavior unchanged to client; backend swapped
  - Label: Intentional runtime change

- [ ] **5.4** Deprecate `DockerBackend` factory pattern
  - `app/core/deep_agent_backend.py` — check if using deprecated `backend=lambda rt: ...` pattern
  - Deep Agents SDK deprecated factory pattern in 0.5.0 → pass instance directly
  - Label: Safe refactor

---

## First PR (Recommended Starting Point)

**PR: `fix: safety & stability quickfixes`**

Scope: Phase 1 items (1.1 + 1.2 + 1.3). One PR, no API surface change, fixes 3 real production bugs.

Files to modify:
- `app/core/sandbox.py` — wrap Docker calls in `run_in_executor`
- `app/core/agent_runner.py` — add `asyncio.timeout()` around `graph.ainvoke()`
- `app/api/agents.py` — validate `tools_config` on create/update

Files to create:
- `app/core/config_schema.py` — `ToolsConfig` Pydantic model

Tests to write:
- Timeout enforced: mock `ainvoke` slow → must `TimeoutError`
- DB session not leaked: each tool call gets its own session
- Bad `tools_config` → 422 from API

---

## Deep Agents SDK Notes (for implementation reference)

Context indexed at: `ctx_search(source="deepagents-python-docs")`

Key facts relevant to this project:
- `DockerBackend` in `deep_agent_backend.py` implements `SandboxBackendProtocol` (custom, not SDK built-in)
- Factory pattern `backend=lambda rt: ...` deprecated since deepagents 0.5.0 — check `deep_agent_backend.py`
- `interrupt_on` param available for HITL flows (Phase 4 builder confirmation step)
- `FilesystemPermission` available for restricting agent FS access per capability profile (Phase 4)
- `HarnessProfile` (beta ≥0.5.4) can override system prompt + exclude tools per model — useful for capability profiles
- `CompositeBackend` routes `/memories/` to `StoreBackend` for cross-thread persistence — relevant for memory-heavy agents

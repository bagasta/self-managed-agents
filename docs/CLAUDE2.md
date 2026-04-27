# CLAUDE2.md — Updated Architecture (Post Deep Agents Migration)

> This file documents the architecture AFTER the Deep Agents SDK migration (April 2026).
> For development commands and environment setup, see CLAUDE.md.

## What Changed vs CLAUDE.md

| Area | Before | After |
|------|--------|-------|
| Agent executor | `langgraph.prebuilt.create_react_agent` | `deepagents.create_deep_agent` (LangGraph 1.x) |
| Planning | None | Built-in `write_todos` tool (deepagents) |
| Virtual FS | `write_file`, `read_file` (sandbox names) | deepagents always injects own `write_file`/`read_file`; sandbox tools renamed to `sandbox_write_file`/`sandbox_read_file` |
| Tool defaults | Most tools ON | Conservative: only memory, skills, escalation ON by default |
| Operator detection | Only `escalation_config.operator_phone` | `operator_ids` list (DB column) + legacy `operator_phone` fallback |
| System prompt | Raw instructions only | Agent Context Block prepended automatically |
| `send_agent_wa_qr` | Part of `whatsapp_media` group | Own group: `wa_agent_manager` (opt-in) |
| Sandbox init | Always created | Lazy: only if `tools_config.sandbox = true` |

---

## Request Flow (Updated)

```
Client (X-API-Key header required)
  → FastAPI router (app/api/)
    → agent_runner.run_agent()
      → Load agent config + session from DB
      → Lazy-init DockerSandbox (only if tools_config.sandbox=true)
      → Load short-term message history from DB
      → Build system prompt:
          1. Agent Context Block  ← NEW: auto-metadata header
          2. Base instructions (agent.instructions)
          3. Long-term memories (from agent_memories table, scoped by external_user_id)
          4. Custom tools list (active tools created by agent)
          5. RAG context (top-3 similar docs, pre-injected, no tool call needed)
          6. Safety policy rules
      → Assemble tool stack from tools_config
      → create_deep_agent(model=llm, tools=tools, system_prompt=system_prompt)
          ↳ deepagents injects: write_todos, ls, read_file, write_file, edit_file, grep, glob, execute
          ↳ Our tools added on top
      → graph.ainvoke({"messages": history})
      → Persist all messages/tool calls to DB (messages table)
      → Every Nth user message: extract long-term memories via LLM
      → Return final AI response text
```

---

## Tool Stack (Updated)

### Always-on by deepagents (cannot disable)
- `write_todos` — planning/task list
- `ls`, `glob`, `grep` — virtual filesystem browse
- `read_file`, `write_file`, `edit_file` — virtual in-memory filesystem (NOT Docker sandbox)
- `execute` — deepagents execute (separate from our bash tool)

### Our tools — ON by default (`tools_config` key omitted or `true`)
| Key | Tools | Notes |
|-----|-------|-------|
| `memory` | `remember`, `recall`, `forget` | Scoped by `external_user_id` |
| `skills` | `create_skill`, `use_skill`, `list_skills` | Prompt snippets |
| `escalation` | `escalate_to_human`, `reply_to_user`, `send_to_number` | Requires prior escalation in session |

### Our tools — OFF by default (must set `true` in `tools_config`)
| Key | Tools | Notes |
|-----|-------|-------|
| `sandbox` | `bash`, `sandbox_write_file`, `sandbox_read_file`, `sandbox_write_binary_file`, `list_files` | Docker containers, 512MB/1CPU |
| `tool_creator` | `create_tool`, `list_tools`, `run_custom_tool` | Agent creates Python tools at runtime |
| `scheduler` | `set_reminder`, `list_reminders`, `cancel_reminder` | APScheduler background jobs |
| `http` | `http_get`, `http_post` | Outbound HTTP with allowed_hosts |
| `mcp` | dynamic from MCP servers | External tool servers |
| `whatsapp_media` | `send_whatsapp_image`, `send_whatsapp_document` | WA channel only |
| `wa_agent_manager` | `send_agent_wa_qr` | For agent-manager bots only |
| `rag` | `search_documents` | pgvector similarity search (RAG also pre-injected automatically) |

### Tool name conflict resolution
deepagents' `FilesystemMiddleware` always registers `read_file` and `write_file` using its own
in-memory `StateBackend`. To avoid duplicate tool names, our Docker sandbox tools use:
- `sandbox_read_file` (was: `read_file`)
- `sandbox_write_file` (was: `write_file`)
- `sandbox_write_binary_file` (was: `write_binary_file`)

---

## Agent Context Block

Prepended automatically to every system prompt. Example:

```
## Platform Context
- Agent ID: 3f7a1c2e-...
- Agent Name: CS Bot Clevio
- Model: anthropic/claude-sonnet-4-6
- Active Tools: memory, skills, escalation, whatsapp_media
- Custom Tools: send_promo_email, get_stock_price
- Channel: whatsapp
- User Phone: 628111234567@s.whatsapp.net
- User Role: user
- Session ID: 9b2d4e...
```

Role is `operator` if the sender's phone number is in `agent.operator_ids` or matches `escalation_config.operator_phone`.

---

## Operator Detection (Updated)

Two sources are checked, both normalized (strip `+`, strip `@domain`):

1. **`agent.operator_ids`** — JSONB list on Agent model (migration 011).
   Example: `["+6281234", "6285678@s.whatsapp.net"]`
2. **`escalation_config.operator_phone`** — Legacy single string (backward compat).

Detection in `app/api/channels.py` (both `wa/incoming` and generic webhook):
```python
_normalized_operator_ids = {_normalize_phone(oid) for oid in agent.operator_ids}
if operator_phone:
    _normalized_operator_ids.add(_normalize_phone(operator_phone))
is_operator = _normalize_phone(sender) in _normalized_operator_ids
```

---

## Database Changes

### Migration 011 — `operator_ids` column
```sql
ALTER TABLE agents ADD COLUMN operator_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
```
Run: `alembic upgrade head`

---

## Dependencies (Current)

```
deepagents>=0.5.0
langgraph>=1.0.0
langchain>=1.0.0
langchain-openai>=1.0.0
langchain-mcp-adapters>=0.2.0
mcp>=1.0.0
```

> **Note**: deepagents 0.5.x requires langchain 1.x. If you see `langchain-core<1.0.0` conflicts,
> ensure ALL langchain-* packages are pinned to 1.x. deepagents may also pull in starlette 1.0.0
> which conflicts with sse-starlette; pin `sse-starlette>=1.0.0,<2.0.0`.

---

## Key Files (Updated Roles)

| File | Role |
|------|------|
| `app/core/agent_runner.py` | Main orchestration: builds LLM, tools, system prompt, runs deepagents graph, persists steps |
| `app/core/sandbox.py` | Docker sandbox (lazy-init); tools renamed to avoid deepagents conflict |
| `app/core/memory_service.py` | Long-term memory, scoped per `external_user_id` |
| `app/core/escalation_tool.py` | `escalate_to_human`, `reply_to_user`, `send_to_number` |
| `app/core/wa_client.py` | HTTP client for Go wa-service |
| `app/api/channels.py` | WhatsApp & generic webhooks; operator detection via `operator_ids` |
| `app/models/agent.py` | Agent ORM model, includes `operator_ids: Mapped[list]` |
| `app/schemas/agent.py` | Pydantic schemas, includes `operator_ids: list[str]` in Create/Update/Response |
| `alembic/versions/011_agent_operator_ids.py` | Migration adding `operator_ids` column |

---

## PRD2 Items Status

| Item | Status |
|------|--------|
| Agent Context Block in system prompt | ✅ Done |
| `operator_ids` field + operator awareness | ✅ Done |
| `send_agent_wa_qr` → `wa_agent_manager` opt-in | ✅ Done |
| Conservative tool defaults | ✅ Done |
| Custom tools listed in system prompt | ✅ Done |
| Sandbox lazy initialization | ✅ Done |
| Migrate to Deep Agents SDK | ✅ Done |
| WhatsApp API lengkap (4.1) | ⬜ Not started |
| Isolasi session WA lebih ketat (4.2) | ⬜ Not started |
| Memory scoping per group_id (4.3) | ⬜ Not started |

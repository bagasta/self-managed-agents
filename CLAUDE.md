# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Managed Agent Platform** — a self-hosted backend for managing and executing AI agents, built on Python/FastAPI + LangGraph. Primary integrations: WhatsApp (via Go microservice), webchat, and internal scripts ("King Bagas" internal tooling).

All agents are config-driven — new agent types are creatable via API with no Python code changes.

## Development Commands

```bash
# Setup
make install          # pip install -r requirements.txt
cp .env.example .env  # configure required env vars

# Database
make db-up            # start PostgreSQL via docker compose
make upgrade          # alembic upgrade head
make migrate MSG="description"  # generate new migration from model changes
make downgrade        # rollback one migration

# Run
make dev              # uvicorn app.main:app --reload (port 8000)
make wa-build         # compile wa-service Go binary
make wa               # run wa-service binary (port 8080); requires make wa-build first
make wa-dev-build     # compile wa-dev-service Go binary
make wa-dev           # run wa-dev-service (port 8081) with dashboard

# Code quality
make lint             # ruff check app/ alembic/
make format           # ruff format app/ alembic/

# Full stack (PostgreSQL + API)
docker compose up --build
```

There are no automated tests. Manual test scripts in `scripts/` (test_db.py, test_script.py, list_sessions.py). Postman collections at `docs/postman/`.

### Required Environment Variables

```
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/managed_agents
API_KEY=<random-secret>            # X-API-Key header for all requests
OPENROUTER_API_KEY=sk-or-v1-...   # LLM access (300+ models)
SANDBOX_BASE_DIR=/tmp/agent-sandboxes
AGENT_MAX_STEPS=12
AGENT_TIMEOUT_SECONDS=300
WA_SERVICE_URL=http://localhost:8080
```

## Architecture

### Request Flow

```
Client (X-API-Key header required)
  → FastAPI router (app/api/)
    → agent_runner.run_agent()
      → Load agent config + session from DB
      → Lazy-init DockerSandbox (only if tools_config.sandbox=true)
      → Load short-term message history from DB
      → Build system prompt:
          1. Agent Context Block (auto-metadata header)
          2. Base instructions (agent.instructions)
          3. Long-term memories (agent_memories table, scoped by external_user_id)
          4. Custom tools list (active tools created by agent)
          5. RAG context (top-3 similar docs, pre-injected, no tool call needed)
          6. Safety policy rules
      → Assemble tool stack from tools_config
      → create_deep_agent(model, tools, system_prompt)
          ↳ deepagents always injects: write_todos, ls, read_file, write_file, edit_file, grep, glob, execute
          ↳ Our tools added on top
      → graph.ainvoke({"messages": history})
      → Persist all messages/tool calls to DB
      → Every Nth user message: extract long-term memories via LLM
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `app/core/agent_runner.py` | Main orchestration: builds LLM, tools, system prompt, runs agent, persists steps |
| `app/core/sandbox.py` | Docker sandbox: ephemeral container per bash() call, workspace dir mounted at `/workspace`, persists across turns |
| `app/core/memory_service.py` | Long-term memory: auto-extracted facts keyed per `external_user_id` |
| `app/core/embedding_service.py` | Sentence-Transformers (all-MiniLM-L6-v2) + pgvector for RAG |
| `app/core/document_service.py` | Document parse (PDF, DOCX, PPTX) + similarity search |
| `app/core/scheduler_service.py` | APScheduler background reminders |
| `app/core/channel_service.py` | Multi-channel config (WhatsApp, WebChat) |
| `app/core/wa_client.py` | HTTP client calling Go wa-service |
| `app/core/event_bus.py` | In-memory asyncio pub/sub per session; used by scheduler → SSE stream |
| `app/core/file_processor.py` | Extracts text from uploaded files before embedding |
| `app/core/custom_tool_service.py` | CRUD for agent-created Python tools stored in DB |
| `app/core/tools/` | Self-contained tool modules; each returns LangChain-compatible tools |
| `app/core/deep_agent_backend.py` | `DockerBackend` — adapts `DockerSandbox` to Deep Agents `SandboxBackendProtocol`; activates built-in `write_file`, `read_file`, `edit_file`, `ls`, `glob`, `grep`, `execute` tools |
| `wa-dev-service/` | Go microservice for managing WA dev/test numbers (port 8081); includes web dashboard |

### Tool Stack (enabled per-agent via `tools_config`)

**Always-on by deepagents SDK** (cannot disable):
- `write_todos` — planning/task list
- `ls`, `glob`, `grep` — virtual filesystem browse
- `read_file`, `write_file`, `edit_file` — deepagents in-memory virtual FS (NOT the Docker sandbox)
- `execute` — deepagents execute (separate from our `bash` sandbox tool)

**Our tools — ON by default** (`tools_config` key omitted or `true`):
- **Memory**: `remember`, `recall`, `forget` — persisted to `agent_memories` table, scoped by `external_user_id`
- **Skills**: `create_skill`, `use_skill`, `list_skills`
- **Escalation**: `escalate_to_human`, `reply_to_user`, `send_to_number` — requires prior escalation in session

**Our tools — OFF by default** (must set `true` in `tools_config`):

| Key | Tools | Notes |
|-----|-------|-------|
| `sandbox` | `bash`, `sandbox_write_file`, `sandbox_read_file`, `sandbox_write_binary_file`, `list_files` | Docker containers, workspace at `{SANDBOX_BASE_DIR}/{session_id}/` |
| `tool_creator` | `create_tool`, `list_tools`, `run_custom_tool` | Agent creates Python tools; requires sandbox |
| `scheduler` | `set_reminder`, `list_reminders`, `cancel_reminder` | APScheduler background jobs |
| `http` | `http_get`, `http_post` | Outbound HTTP |
| `rag` | `search_documents` | RAG also pre-injected automatically into system prompt |
| `whatsapp_media` | `send_whatsapp_image`, `send_whatsapp_document` | WA channel only |
| `wa_agent_manager` | `send_agent_wa_qr` | For agent-manager bots only |
| `mcp` | dynamic | Tools from external MCP servers |
| `subagents` | `task()` | Delegate to sub-agents |

**Tool name conflict note**: deepagents' `FilesystemMiddleware` always registers `read_file` and `write_file` using its own `StateBackend`. Our Docker sandbox tools are named `sandbox_read_file` / `sandbox_write_file` to avoid conflicts.

### Subagents (`tools_config.subagents`)

```json
{ "subagents": { "enabled": true } }
```
Auto-loads 4 hardcoded system sub-agents (no DB dependency):
- `sys_researcher` — HTTP/web research, returns structured summaries
- `sys_coder` — Python sandbox, writes and executes code
- `sys_writer` — Writing, editing, formatting, translation
- `sys_analyst` — Data analysis with pandas/numpy in sandbox

To use specific custom agents instead:
```json
{ "subagents": { "enabled": true, "agent_ids": ["uuid-1", "uuid-2"] } }
```
Custom agents are loaded from DB by UUID. Both system and custom can be mixed by listing UUIDs (system agents are always built-in when `agent_ids` is empty).

Sub-agent rules:
- Workspace isolated: `{SANDBOX_BASE_DIR}/{session_id}_sys_{name}/` or `{session_id}_sub_{agent_id}/`
- Excluded tools in sub-agents: escalation, scheduler, wa_agent_manager (no channel access)
- Model: each sub-agent uses its own model config (default `openai/gpt-4o-mini` for system agents)
- Session: ephemeral — not persisted to DB as a full session

### WhatsApp Integration (wa-service)

Go microservice at `wa-service/`. Uses `whatsmeow` library (WhatsApp Web).

```
POST /devices          → create device, returns base64 QR PNG
GET  /devices/{id}/qr  → current QR
GET  /devices/{id}/status
POST /devices/{id}/send
POST /devices/{id}/send-image
DELETE /devices/{id}
```

Device state is in memory + SQLite (`{WA_STORE_DIR}/{device_id}.db`). On incoming messages, calls Python webhook at `PYTHON_WEBHOOK_URL` → `POST /v1/channels/wa/incoming`.

### Data Models (PostgreSQL)

- `agents` — config: model, instructions, tools_config (JSON), safety_policy, wa_device_id
- `sessions` — per-user/task context: agent_id, external_user_id, metadata
- `messages` — every turn and tool call: role (user|agent|tool), content, step_index, run_id
- `agent_memories` — long-term KV facts, scoped per external_user_id
- `agent_skills` — reusable prompt snippets
- `agent_custom_tools` — user-created Python tool code
- `documents` — uploaded files with pgvector embeddings
- `scheduled_jobs` — APScheduler-backed reminders
- `channels` — per-agent channel config (type, webhook_url, etc.)

### LLM Access

All LLMs accessed via OpenRouter (`langchain-openai` with base_url override). Model is set per-agent (e.g. `anthropic/claude-sonnet-4-6`, `openai/gpt-4.1-mini`). List available models: `GET /v1/models`.

## API Surface

All requests require `X-API-Key: <API_KEY>` header. Swagger UI at `/docs`.

```
POST/GET/PATCH/DELETE /v1/agents/{id}
POST   /v1/agents/{id}/sessions
POST   /v1/agents/{id}/sessions/{session_id}/messages   ← primary execution endpoint
GET    /v1/sessions/{session_id}/history
GET    /v1/runs/{run_id}
GET/POST/DELETE /v1/agents/{id}/memory
GET/POST/DELETE /v1/agents/{id}/skills
GET/POST        /v1/agents/{id}/custom-tools
POST            /v1/agents/{id}/documents/upload
GET/DELETE      /v1/agents/{id}/documents
GET    /v1/agents/{id}/wa/qr
GET    /v1/agents/{id}/wa/status
POST   /v1/channels/wa/incoming                         ← wa-service webhook
GET    /v1/sessions/{session_id}/stream                 ← SSE stream (scheduled/proactive events)
GET    /health
```

## Key Constraints

- **Step limit**: `AGENT_MAX_STEPS` (default 12); controlled via `recursion_limit` in LangGraph
- **Timeout**: `AGENT_TIMEOUT_SECONDS` (default 300); tools have retry + fallback
- **Memory scoping**: Long-term memories are always scoped by `external_user_id` to prevent cross-user leakage
- **Sandbox**: Each bash call spins a fresh ephemeral container; the workspace dir persists across message turns within a session
- **Escalation safety**: `reply_to_user` and `send_to_number` require prior `escalate_to_human` call in same session to prevent accidental sends

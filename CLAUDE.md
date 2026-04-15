# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Managed Agent Platform** — a self-hosted backend for managing and executing AI agents across internal applications ("King Bagas" internal tooling). Built on Python/FastAPI + LangChain DeepAgents. Primary goal: a central "agent platform" callable from OpenClaw, MCP, webchat, and internal scripts.

This is a greenfield project. The only artifact currently is `PRD.md` (the product spec, written in Indonesian).

## Planned Stack

- **Backend**: Python + FastAPI
- **AI Orchestration**: LangChain + LangChain DeepAgents
- **Database**: PostgreSQL (recommended) or SQLite for POC
- **Auth**: Phase 1 — `X-API-Key` header; Phase 2 — JWT/OAuth2
- **Observability**: Structured local logging; optional LangSmith integration

## Architecture

### Core Data Models

**Agent** — defines an AI agent configuration:
- `id`, `name`, `description`, `instructions` (system prompt), `model` (e.g. `claude-sonnet-4-6`, `gpt-4.1-mini`)
- `tools_config` (JSON — enabled tools + their configs)
- `safety_policy` (JSON/text), `version` (int), timestamps

**Session** — a per-user/task conversation context:
- `id`, `agent_id`, `external_user_id` (optional), `metadata` (JSON), timestamps

**Message / Run Log** — each turn and tool call:
- `id`, `session_id`, `role` (user | agent | tool), `content`, `tool_name` (optional), `timestamp`

### API Surface

```
# Agent management
POST   /v1/agents
GET    /v1/agents
GET    /v1/agents/{agent_id}
PATCH  /v1/agents/{agent_id}
DELETE /v1/agents/{agent_id}

# Session lifecycle
POST   /v1/agents/{agent_id}/sessions

# Primary execution endpoint (called by OpenClaw, MCP, webchat, etc.)
POST   /v1/agents/{agent_id}/sessions/{session_id}/messages

# History & observability
GET    /v1/sessions/{session_id}/history
GET    /v1/runs/{run_id}
```

### Agent Execution Flow (per request)

1. Load `AgentConfig` from DB
2. Build a `DeepAgent` instance:
   - `instructions` → system prompt
   - `model` from config
   - Attach tools from `tools_config`
   - Inject safety guidelines
3. Load session history as context
4. Run DeepAgent with user message; enforce max steps (8–12) and timeout
5. Log all tool calls (name, args, result summary) and persist the run

### Tool System

Tools are modular and attached per-agent via `tools_config`. Phase 1 tools:
- **HTTP tool** — generic REST GET/POST to internal endpoints
- **RAG retrieval tool** — document/FAQ index lookup
- **GitHub API wrapper** (POC) — read issues and PRs

New tools should be self-contained modules that accept a config object and return a LangChain-compatible tool.

## Development Milestones

**Milestone 1 (POC):** FastAPI skeleton + DB migrations; `POST/GET /v1/agents`; session creation; message endpoint with one dummy tool; basic logging.

**Milestone 2 (Internal Alpha):** Real tools (HTTP internal, RAG); improved step/tool call logging; history endpoint; first channel integration (OpenClaw or webchat).

**Milestone 3 (Hardening):** Basic admin UI; API key auth; LangSmith integration; developer docs.

## Key Constraints

- **Step limit**: Cap agent runs at 8–12 steps to prevent runaway loops and control LLM cost.
- **Timeout**: Every run must have a hard timeout; tools must have retry + fallback so the agent can still reply even if a tool fails.
- **Config-driven agents**: New agent types must be creatable via API/config without changing Python code.
- **Single-tenant**: Phase 1 is single-tenant with simple user scoping via `external_user_id`.

# System Overview - Managed Agent Platform

Tanggal snapshot: 2026-07-02

## System Context
Managed Agent Platform menerima request dari API client, WhatsApp production service, WA dev trial service, dashboard/dev UI, scheduler, dan MCP integrations. Core backend mengorkestrasi LLM, tools, memory, RAG, sandbox, sub-agent, WhatsApp send, dan persistence.

## High-Level Architecture
```text
Client / WhatsApp / Scheduler / Dashboard
        |
        v
FastAPI app.main
        |
        +-- API routers app/api/*
        +-- Agent runtime app/core/engine/*
        +-- Domain services app/core/domain/*
        +-- Infra adapters app/core/infra/*
        |
        +-- PostgreSQL + pgvector
        +-- Redis optional event/rate-limit backend
        +-- Docker sandbox/deployment containers
        +-- OpenRouter LLMs
        +-- Go wa-service / wa-dev-service
        +-- MCP servers, especially Google Workspace
```

## Component Diagram
```text
FastAPI
  |-- agents/messages/sessions/history/runs API
  |-- users/subscriptions API
  |-- documents/memory/skills/custom-tools API
  |-- channels API
  |
  |-- agent_runner
      |-- prompt_builder
      |-- agent_tool_setup
      |-- tool_builder
      |-- google_mcp_support
      |-- reply/WA/SOP guards
      |-- DeepAgents / LangGraph
  |
  |-- domain services
      |-- memory_service
      |-- document_service
      |-- embedding_service
      |-- subscription_service
      |-- agent_sop_service
  |
  |-- infra
      |-- DockerSandbox
      |-- wa_client
      |-- channel_service
      |-- deployment_service
      |-- Redis event bus
```

## Service Overview
- `api`: FastAPI backend, REST API, runtime agent, metrics, health, static `/ui`.
- `scheduler`: worker untuk proactive scheduled jobs di production compose.
- `postgres`: database utama di local compose; production memakai host DB + pgbouncer.
- `redis`: optional multi-process event bus dan rate limit storage.
- `wa-service`: Go WhatsApp service production, satu device/session per agent.
- `wa-dev-service`: Go shared trial number service untuk demo/multi-agent routing.
- `pgbouncer`: connection pooling production.
- External MCP services: Google Workspace MCP dan Google integration/OAuth service.

## Data Flow
1. Client membuat agent via `/v1/agents` dengan `X-API-Key`.
2. Client membuat session via `/v1/agents/{agent_id}/sessions`.
3. Client/customer mengirim message via API atau WhatsApp webhook.
4. `messages.py` validasi `X-Agent-Key`, quota, session, dan lock session.
5. `agent_runner.run_agent()` membuat run record, history context, prompt, LLM, tools, MCP runtime, dan graph.
6. Agent calls tools, writes steps, handles interrupts/recovery, lalu final reply dipersist.
7. Reply dikirim via channel service kalau session channel aktif.
8. Usage/token dicatat di `runs`, `agents`, dan subscription owner.

## Integration Flow
- WhatsApp inbound: `wa-service` -> `POST /v1/channels/wa/incoming` -> session lookup/create -> agent run -> WA send.
- WA dev trial: `wa-dev-service` manages shared number routing, claim code, disconnect, operator route.
- Google Workspace: builder enables MCP config -> auth link generated -> token injected by runtime -> MCP tools loaded via `langchain-mcp-adapters`.
- RAG: upload document -> extract/chunk/embed -> store `documents.embedding` -> runtime search context.
- Sandbox: runtime creates per-session workspace -> ephemeral Docker container per command -> cleanup reaper.

## External Dependencies
- OpenRouter API.
- WhatsApp Web protocol via whatsmeow.
- Docker daemon/socket.
- PostgreSQL 16 + pgvector.
- Redis 7 optional.
- Google integration service and Workspace MCP.
- Tavily API for browsing tools.
- Mistral OCR for PDF extraction.
- Sentry optional.

## System Boundaries
- Backend owns agent configuration, runtime, memory, RAG, subscriptions, and API contracts.
- Go WhatsApp services own WA device sessions and QR/send/inbound bridging.
- MCP servers own external app-specific action execution.
- Docker sandbox/deployment containers are untrusted execution zones and must not be treated as secure by default.
- Arthur dedicated WhatsApp identity must stay separate from `wa-dev-service` shared trial number.


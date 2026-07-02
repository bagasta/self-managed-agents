# Agent Architecture

Tanggal snapshot: 2026-07-02

## Agent List
- Arthur / builder agent: creates, updates, verifies, and manages user agents.
- Business/customer agents: WhatsApp/API agents built from config.
- System subagents: `sys_researcher`, `sys_coder`, `sys_writer`, `sys_analyst`.
- Custom subagents: DB-backed agents delegated through `task()`.
- Operator mode: same agent runtime but message starts with operator envelope and gets operator tools.

## Agent Responsibilities
- Arthur: interview user, plan agent, compose blueprint/SOP/instructions/soul, create/update records, set owner/operator, enable channel/tools, generate Google auth link, verify readiness.
- Business agents: serve customer workflows, answer questions, intake order, use memory/RAG, escalate, and respect SOP.
- Subagents: execute bounded specialist work without direct channel authority.
- Operator: respond to escalations, approve/deny actions, send direct replies when allowed.

## Agent Workflow
1. API/channel creates or loads session.
2. Runtime builds current time, platform context, memory, RAG, SOP, history summary, and tool capability contract.
3. Runtime builds active tools based on `tools_config`, channel, operator turn, SOP maturity, and Google/MCP policy.
4. DeepAgents/LangGraph executes with step logger.
5. Tool calls are persisted as message steps.
6. Reply guards validate non-empty reply and prevent false success claims.
7. Long-term memory extraction runs every configured interval.
8. Token usage and run status are stored.

## Input and Output
- Input: text, optional external user ID, session/channel metadata, possible WA media.
- Output: final reply, step summaries, run ID, channel send side effects, updated DB records.
- Tool output: normalized strings/JSON persisted to messages/runs.

## Tool Usage
- Always/common: memory, heartbeat, skills, escalation depending on config defaults.
- Optional: sandbox, tool_creator, scheduler, HTTP, Tavily, RAG, WhatsApp media, WA agent manager, deployment, MCP, subagents.
- WhatsApp channel self-heals media tools because file send is a latent WA need.
- Tool creator requires sandbox.
- Builder tools are exposed only for builder-capable agents.
- Google Workspace requests prefer MCP tools over sandbox fallback.

## Memory Usage
- `agent_memories` stores key/value facts.
- Scope is normally `session.external_user_id`.
- Global memories use `scope=None` for agent identity/soul.
- Runtime builds layered memory and can extract long-term memory after N user messages.
- Daily and long-term update tools exist for proactive memory maintenance.

## Context Management
- Short-term conversation window controlled by `SHORT_TERM_MEMORY_TURNS`.
- Context summary trigger controlled by `CONTEXT_SUMMARY_TRIGGER`.
- Prompt includes current time in WIB.
- Prompt includes runtime tool contract so agent knows actual available actions.
- RAG context is appended when enabled and relevant.
- Google MCP notices modify prompt when integration auth/state is relevant.

## Handoff Rules
- `escalate_to_human` starts human handoff.
- `reply_to_user` and `send_to_number` require prior escalation evidence in session.
- Operator messages are detected by `[OPERATOR]` or `<OPERATOR>` envelope.
- Subagents must not directly send WhatsApp/customer-visible messages unless explicitly exposed and safe.
- Parent agent owns final WA delivery for generated files.

## Failure Recovery
- Active run cancellation on new user message for same session.
- Startup marks running runs as `abandoned`.
- Recovery messages can be sent for interrupted or failed runs.
- Google MCP auth/scope failures are converted into re-auth/blocker replies.
- Reply guards override empty, unsafe, or false success replies.
- Sandbox and deployment cleanup loops remove orphan resources.

## Escalation Flow
```text
Customer message
  -> Agent detects need for human
  -> escalate_to_human creates operator context
  -> Operator receives notification/summary
  -> Operator responds with envelope
  -> Runtime exposes operator tools
  -> reply_to_user or send_to_number is allowed only after escalation evidence
```


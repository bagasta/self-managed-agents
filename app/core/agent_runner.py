"""
agent_runner.py — Orchestrator utama yang menggabungkan semua komponen dan menjalankan agent.

Modul ini HANYA berisi logika orchestration (run_agent).
Semua implementation detail dipecah ke:
  - app/core/tool_builder.py     → build_*_tools()
  - app/core/prompt_builder.py   → build_system_prompt(), build_rag_context(), maybe_summarize_context()
  - app/core/subagent_builder.py → build_subagents(), _SYSTEM_SUBAGENTS
  - app/core/context_service.py  → load_history(), count_user_messages(), db_messages_to_lc()

Memory model
------------
Short-term  Last `short_term_memory_turns` user/agent pairs loaded from DB
            into the LLM context window. Older turns are silently dropped.

Long-term   Persistent key-value store (agent_memories table).
            Injected into every system prompt as a markdown block.
            Auto-extracted: every `ltm_extraction_every` user messages,
            the LLM reads recent turns and distils important facts.

RAG context Top-3 documents (cosine-similar to the user query) fetched
            from the vector store and injected into the system prompt.
            Agent does NOT need to call a tool — context is pre-injected.

Tool defaults (conservative)
-----------------------------
ON  by default : memory, skills, escalation, whatsapp_media (WA channel only)
OFF by default : sandbox, tool_creator, scheduler, http, mcp,
                 wa_agent_manager
"""
from __future__ import annotations

import uuid
from typing import Any, TypedDict

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.context_service import count_user_messages, db_messages_to_lc, load_history
from app.core.log_sanitizer import redact_pii
from app.core.memory_service import build_memory_context, extract_long_term_memory
from app.core.prompt_builder import build_rag_context, build_system_prompt, maybe_summarize_context
from app.core.sandbox import DockerSandbox
from app.core.subagent_builder import build_subagents
from app.core.tool_builder import (
    _is_enabled,
    build_http_tools,
    build_loaded_custom_tools,
    build_memory_tools,
    build_sandbox_binary_tool,
    build_skill_tools,
    build_tool_creator_tools,
    build_wa_agent_manager_tools,
    build_whatsapp_media_tools,
)
from app.models.agent import Agent as AgentModel
from app.models.message import Message
from app.models.session import Session
from app.core.custom_tool_service import list_custom_tools

logger = structlog.get_logger(__name__)
settings = get_settings()


class AgentRunResult(TypedDict):
    reply: str
    steps: list[dict]
    run_id: uuid.UUID
    tokens_used: int


async def run_agent(
    *,
    agent_model: AgentModel,
    session: Session,
    user_message: str,
    db: AsyncSession,
    escalation_user_jid: str | None = None,
    escalation_context: str | None = None,
    media_image_b64: str | None = None,
    media_image_mime: str | None = None,
    sender_name: str | None = None,
) -> AgentRunResult:
    """
    Jalankan agent end-to-end:
    1. Setup LLM + sandbox
    2. Build tools berdasarkan tools_config
    3. Build sub-agents (jika enabled)
    4. Inject RAG + context summary + memory ke system prompt
    5. Load history, run graph
    6. Persist messages ke DB
    7. Auto-extract long-term memory (jika triggered)
    """
    run_id = uuid.uuid4()
    agent_id: uuid.UUID = session.agent_id
    _raw_tools_cfg = agent_model.tools_config
    tools_config: dict[str, Any] = _raw_tools_cfg if isinstance(_raw_tools_cfg, dict) else {}
    temperature: float = getattr(agent_model, "temperature", 0.7)

    log = logger.bind(
        run_id=str(run_id),
        session_id=str(session.id),
        agent_id=str(agent_id),
        model=agent_model.model,
    )
    log.info("agent_run.start")

    # ------------------------------------------------------------------ #
    # 1. LLM                                                              #
    # ------------------------------------------------------------------ #
    llm = ChatOpenAI(
        model=agent_model.model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=settings.llm_max_tokens,
        temperature=temperature,
    )

    # ------------------------------------------------------------------ #
    # 2. Sandbox (lazy init)                                              #
    # ------------------------------------------------------------------ #
    sandbox: DockerSandbox | None = None
    if _is_enabled(tools_config, "sandbox", default=False):
        sandbox = DockerSandbox(session.id)

    # ------------------------------------------------------------------ #
    # 3. Tools                                                            #
    # ------------------------------------------------------------------ #
    tools: list = []
    active_groups: list[str] = []
    saved_custom_tools: list = []

    if sandbox is not None:
        tools.extend(build_sandbox_binary_tool(sandbox))
        active_groups.append("sandbox")

    _memory_scope = getattr(session, "external_user_id", None)
    if _is_enabled(tools_config, "memory", default=True):
        tools.extend(build_memory_tools(agent_id, db, scope=_memory_scope))
        active_groups.append("memory")

    if _is_enabled(tools_config, "skills", default=True):
        tools.extend(build_skill_tools(agent_id, db))
        active_groups.append("skills")

    if _is_enabled(tools_config, "tool_creator", default=False):
        if sandbox is None:
            log.warning("agent_run.tool_creator_requires_sandbox")
        else:
            tools.extend(build_tool_creator_tools(agent_id, db, sandbox))
            saved_custom_tools = await list_custom_tools(agent_id, db)
            tools.extend(build_loaded_custom_tools(saved_custom_tools, sandbox))
            active_groups.append("tool_creator")

    if _is_enabled(tools_config, "scheduler", default=False):
        from app.core.tools.scheduler_tool import build_scheduler_tools
        tools.extend(build_scheduler_tools(session.id, agent_id, db))
        active_groups.append("scheduler")

    if _is_enabled(tools_config, "escalation", default=True):
        from app.core.tools.escalation_tool import build_escalation_tools
        _raw_cfg = session.channel_config
        _channel_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        _user_jid = (
            escalation_user_jid
            or _channel_cfg.get("user_phone")
            or getattr(session, "external_user_id", None)
        )
        tools.extend(build_escalation_tools(session.id, agent_id, db, user_jid=_user_jid))
        active_groups.append("escalation")

    # Operator tools: hanya aktif di session operator (is_op_msg = True)
    is_op_msg_early = user_message.startswith("[OPERATOR] ")
    if is_op_msg_early:
        from app.core.tools.operator_tools import build_operator_tools
        tools.extend(build_operator_tools(agent_id=agent_id, db=db))
        active_groups.append("operator")

    if _is_enabled(tools_config, "http", default=False):
        tools.extend(build_http_tools(tools_config))
        active_groups.append("http")

    if getattr(session, "channel_type", None) == "whatsapp":
        if _is_enabled(tools_config, "whatsapp_media", default=True):
            tools.extend(build_whatsapp_media_tools(session, sandbox))
            active_groups.append("whatsapp_media")
        if _is_enabled(tools_config, "wa_agent_manager", default=False):
            tools.extend(build_wa_agent_manager_tools(session))
            active_groups.append("wa_agent_manager")

    # ------------------------------------------------------------------ #
    # 4. Sub-agents                                                       #
    # ------------------------------------------------------------------ #
    subagent_list: list = []
    sub_sandboxes: list[DockerSandbox] = []
    if _is_enabled(tools_config, "subagents", default=False):
        _sub_ids: list[str] = tools_config.get("subagents", {}).get("agent_ids", [])
        subagent_list, sub_sandboxes = await build_subagents(_sub_ids, session.id, db, log)
        if subagent_list:
            active_groups.append(f"subagents({len(subagent_list)})")
            log.info("agent_run.subagents_ready", names=[s["name"] for s in subagent_list])

    log.debug("agent_run.tools_ready (pre-mcp)", groups=active_groups, count=len(tools))

    # ------------------------------------------------------------------ #
    # 5. Context enrichment                                               #
    # ------------------------------------------------------------------ #
    rag_context = ""
    if _is_enabled(tools_config, "rag", default=False):
        rag_context = await build_rag_context(agent_id, user_message, db, tools_config, log)

    context_summary = await maybe_summarize_context(session, db, llm, log)

    memory_block = await build_memory_context(agent_id, db, scope=_memory_scope)

    history_rows = await load_history(session.id, db, max_turns=settings.short_term_memory_turns)
    prior_messages = db_messages_to_lc(history_rows)
    log.debug("agent_run.history_loaded", turns=len(prior_messages) // 2)

    is_op_msg = user_message.startswith("[OPERATOR] ")

    # ------------------------------------------------------------------ #
    # 6. System prompt                                                    #
    # ------------------------------------------------------------------ #
    system_prompt = build_system_prompt(
        agent_model=agent_model,
        session=session,
        active_groups=active_groups,
        saved_custom_tools=saved_custom_tools,
        subagent_list=subagent_list,
        sender_name=sender_name,
        context_summary=context_summary,
        memory_block=memory_block,
        rag_context=rag_context,
        escalation_user_jid=escalation_user_jid,
        escalation_context=escalation_context,
        is_operator_message=is_op_msg,
    )

    # ------------------------------------------------------------------ #
    # 7. Persist user message                                             #
    # ------------------------------------------------------------------ #
    step_base = max((m.step_index for m in history_rows), default=-1) + 1
    db.add(Message(
        session_id=session.id,
        role="user",
        content=user_message,
        step_index=step_base,
        run_id=run_id,
    ))
    await db.flush()

    # ------------------------------------------------------------------ #
    # 8. Run agent graph (with MCP tools)                                 #
    # ------------------------------------------------------------------ #
    from app.core.tools.mcp_tool import mcp_client_context
    from langchain_core.callbacks import AsyncCallbackHandler

    class _AgentLogger(AsyncCallbackHandler):
        async def on_llm_start(self, serialized, prompts, **kwargs):
            log.debug("agent_step.llm_thinking")

        async def on_llm_end(self, response, **kwargs):
            try:
                text = response.generations[0][0].text[:200]
                if text:
                    log.info("agent_step.llm_response", preview=text)
            except Exception:
                pass

        async def on_tool_start(self, serialized, input_str, **kwargs):
            tool_name = serialized.get("name", "?")
            safe_input = redact_pii(str(input_str)[:300])
            log.info("agent_step.tool_call", tool=tool_name, input=safe_input)

        async def on_tool_end(self, output, **kwargs):
            log.info("agent_step.tool_result", output=str(output)[:300])

        async def on_tool_error(self, error, **kwargs):
            log.warning("agent_step.tool_error", error=str(error))

        async def on_chain_start(self, serialized, inputs, **kwargs):
            if not serialized:
                return
            name = serialized.get("name", serialized.get("id", ["?"])[-1])
            log.debug("agent_step.chain_start", chain=name)

        async def on_chain_end(self, outputs, **kwargs):
            log.debug("agent_step.chain_end")

    async with mcp_client_context(tools_config) as mcp_tools:
        if mcp_tools:
            tools = tools + mcp_tools
            active_groups.append(f"mcp({len(mcp_tools)} tools)")
            log.debug("agent_run.mcp_tools_added", count=len(mcp_tools))

        try:
            from deepagents import create_deep_agent
            from app.core.deep_agent_backend import DockerBackend

            backend = DockerBackend(sandbox) if sandbox is not None else None
            graph = create_deep_agent(
                model=llm,
                tools=tools,
                system_prompt=system_prompt,
                backend=backend,
                subagents=subagent_list or None,
            )
        except (ImportError, TypeError):
            from langgraph.prebuilt import create_react_agent
            graph = create_react_agent(llm, tools=tools, prompt=system_prompt)

        if media_image_b64 and media_image_mime:
            human_content: Any = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": f"data:{media_image_mime};base64,{media_image_b64}"}},
            ]
        else:
            human_content = user_message

        input_messages: list[BaseMessage] = prior_messages + [HumanMessage(content=human_content)]
        steps: list[dict[str, Any]] = []
        final_reply = ""
        step_counter = step_base + 1

        try:
            result = await graph.ainvoke(
                {"messages": input_messages},
                config={
                    "recursion_limit": settings.agent_max_steps * 2,
                    "callbacks": [_AgentLogger()],
                },
            )
        except Exception as exc:
            log.error("agent_run.error", error=str(exc))
            if sandbox:
                sandbox.close()
            for _ssb in sub_sandboxes:
                _ssb.close()
            raise

        # ------------------------------------------------------------------ #
        # 9. Parse & persist result messages                                  #
        # ------------------------------------------------------------------ #
        all_messages: list[BaseMessage] = result.get("messages", [])
        new_messages = all_messages[len(input_messages):]
        tool_step = 0
        pending_tool_records: list[Message] = []
        total_tokens_used = 0

        for msg in new_messages:
            if isinstance(msg, AIMessage):
                usage = getattr(msg, "usage_metadata", None)
                if usage:
                    total_tokens_used += usage.get("total_tokens", 0)

                if msg.content:
                    text = msg.content if isinstance(msg.content, str) else str(msg.content)
                    final_reply = text
                    db.add(Message(
                        session_id=session.id,
                        role="agent",
                        content=text,
                        step_index=step_counter,
                        run_id=run_id,
                    ))
                    step_counter += 1
                for tc in (msg.tool_calls or []):
                    tool_step += 1
                    steps.append({"step": tool_step, "tool": tc["name"], "args": tc.get("args", {}), "result": ""})
                    record = Message(
                        session_id=session.id,
                        role="tool",
                        tool_name=tc["name"],
                        tool_args=tc.get("args", {}),
                        step_index=step_counter,
                        run_id=run_id,
                    )
                    db.add(record)
                    pending_tool_records.append(record)
                    step_counter += 1
            elif isinstance(msg, ToolMessage):
                output = msg.content if isinstance(msg.content, str) else str(msg.content)
                for entry in reversed(steps):
                    if entry["result"] == "":
                        entry["result"] = output[:500]
                        break
                if pending_tool_records:
                    pending_tool_records.pop(0).tool_result = output[:2000]

    await db.flush()

    # ------------------------------------------------------------------ #
    # 10. Long-term memory auto-extraction                                #
    # ------------------------------------------------------------------ #
    if _is_enabled(tools_config, "memory", default=True):
        user_msg_count = await count_user_messages(session.id, db)
        if user_msg_count > 0 and user_msg_count % settings.ltm_extraction_every == 0:
            log.info("agent_run.ltm_trigger", user_messages=user_msg_count)
            recent_for_ltm = await load_history(session.id, db, max_turns=settings.ltm_extraction_every)
            await extract_long_term_memory(
                agent_id=agent_id,
                recent_messages=recent_for_ltm,
                llm=llm,
                db=db,
                log=log,
                scope=_memory_scope,
            )

    # cleanup
    if sandbox:
        sandbox.close()
    for _ssb in sub_sandboxes:
        _ssb.close()

    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        tokens_used=total_tokens_used,
    )
    return {"reply": final_reply, "steps": steps, "run_id": run_id, "tokens_used": total_tokens_used}

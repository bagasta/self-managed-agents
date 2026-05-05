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

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, TypedDict

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.database import AsyncSessionLocal

from app.config import get_settings
from app.core.engine.context_service import count_user_messages, db_messages_to_lc, load_history
from app.core.utils.log_sanitizer import redact_pii
from app.core.domain.memory_service import build_memory_context, extract_long_term_memory
from app.core.engine.prompt_builder import build_rag_context, build_system_prompt, maybe_summarize_context
from app.core.infra.sandbox import DockerSandbox
from app.core.engine.subagent_builder import build_subagents
from app.core.engine.tool_builder import (
    _is_enabled,
    build_builder_tools,
    build_deployment_tools,
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
from app.models.run import Run
from app.models.session import Session
from app.core.domain.custom_tool_service import list_custom_tools
from app.core.engine.result_parser import (
    ParsedResult,
    ensure_tool_messages_complete as _ensure_tool_messages_complete,
    sanitize_input_messages as _sanitize_input_messages,
    parse_agent_result,
)

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

    # --- Create Run record (status: running) ---
    _now = datetime.now(timezone.utc)
    run_record = Run(
        id=run_id,
        session_id=session.id,
        status="running",
        started_at=_now,
    )
    db.add(run_record)
    await db.flush()

    # ------------------------------------------------------------------ #
    # 1. LLM                                                              #
    # ------------------------------------------------------------------ #
    _model_name = agent_model.model or ""
    if _model_name.startswith("mistral/") or _model_name.startswith("mistral-"):
        _llm_api_key = settings.mistral_api_key
        _llm_base_url = "https://api.mistral.ai/v1"
        _bare_model = _model_name.removeprefix("mistral/")
    else:
        _llm_api_key = settings.openrouter_api_key
        _llm_base_url = "https://openrouter.ai/api/v1"
        _bare_model = _model_name

    _max_tokens: int = getattr(agent_model, "max_tokens", None) or settings.llm_max_tokens
    _sandbox_enabled = _is_enabled(
        _raw_tools_cfg if isinstance(_raw_tools_cfg, dict) else {}, "sandbox", default=False
    )
    # llm_raw: untuk create_deep_agent (DeepAgents SDK tidak support RunnableBinding)
    # llm    : dengan .bind(parallel_tool_calls=False) untuk create_react_agent fallback
    llm_raw = ChatOpenAI(
        model=_bare_model,
        api_key=_llm_api_key,
        base_url=_llm_base_url,
        temperature=temperature,
        max_tokens=_max_tokens,
    )
    llm = llm_raw.bind(parallel_tool_calls=False)

    # ------------------------------------------------------------------ #
    # 2. Sandbox (lazy init)                                              #
    # ------------------------------------------------------------------ #
    sandbox: DockerSandbox | None = None
    _deploy_enabled = _is_enabled(tools_config, "deploy", default=False)
    if _is_enabled(tools_config, "sandbox", default=False) or _deploy_enabled:
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

        tools.extend(build_deployment_tools(sandbox))
        active_groups.append("deploy")

    _memory_scope = getattr(session, "external_user_id", None)
    if _is_enabled(tools_config, "memory", default=True):
        tools.extend(build_memory_tools(agent_id, AsyncSessionLocal, scope=_memory_scope))
        active_groups.append("memory")

    if _is_enabled(tools_config, "skills", default=True):
        tools.extend(build_skill_tools(agent_id, AsyncSessionLocal))
        active_groups.append("skills")

    if _is_enabled(tools_config, "tool_creator", default=False):
        if sandbox is None:
            log.warning("agent_run.tool_creator_requires_sandbox")
        else:
            tools.extend(build_tool_creator_tools(agent_id, AsyncSessionLocal, sandbox))
            saved_custom_tools = await list_custom_tools(agent_id, db)
            tools.extend(build_loaded_custom_tools(saved_custom_tools, sandbox))
            active_groups.append("tool_creator")

    if _is_enabled(tools_config, "scheduler", default=False):
        from app.core.tools.scheduler_tool import build_scheduler_tools
        tools.extend(build_scheduler_tools(session.id, agent_id, AsyncSessionLocal))
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
        tools.extend(build_escalation_tools(session.id, agent_id, AsyncSessionLocal, user_jid=_user_jid))
        active_groups.append("escalation")

    # Operator tools: hanya aktif di session operator (is_op_msg = True)
    is_op_msg_early = user_message.startswith("[OPERATOR] ")
    if is_op_msg_early:
        from app.core.tools.operator_tools import build_operator_tools
        tools.extend(build_operator_tools(agent_id=agent_id, db_factory=AsyncSessionLocal))
        active_groups.append("operator")

    if _is_enabled(tools_config, "http", default=False):
        tools.extend(build_http_tools(tools_config))
        active_groups.append("http")

    if getattr(session, "channel_type", None) == "whatsapp":
        if _is_enabled(tools_config, "whatsapp_media", default=True):
            tools.extend(build_whatsapp_media_tools(session, sandbox))
            active_groups.append("whatsapp_media")
        if _is_enabled(tools_config, "wa_agent_manager", default=False):
            tools.extend(build_wa_agent_manager_tools(session, db_factory=AsyncSessionLocal))
            active_groups.append("wa_agent_manager")

    # Builder tools — hanya untuk agent dengan capability "builder"
    _caps = getattr(agent_model, "capabilities", []) or []
    if "builder" in _caps:
        tools.extend(build_builder_tools(
            db_factory=AsyncSessionLocal,
            owner_phone=_memory_scope,
            self_agent_id=str(agent_id),
            api_key=settings.api_key,
        ))
        active_groups.append("builder")

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
        """Enhanced callback logger with tool_call_id tracing."""

        async def on_llm_start(self, serialized, prompts, **kwargs):
            log.debug("agent_step.llm_thinking")

        async def on_llm_end(self, response, **kwargs):
            try:
                gen = response.generations[0][0]
                text = gen.text[:200] if gen.text else ""
                # Log tool_call_ids from the AIMessage for tracing
                ai_msg = gen.message if hasattr(gen, "message") else None
                tc_ids = []
                if ai_msg and hasattr(ai_msg, "tool_calls") and ai_msg.tool_calls:
                    tc_ids = [
                        f"{tc.get('name', '?')}:{tc.get('id', '?')}"
                        for tc in ai_msg.tool_calls
                    ]
                if text:
                    log.info("agent_step.llm_response", preview=text,
                             tool_calls=tc_ids if tc_ids else None)
                elif tc_ids:
                    log.info("agent_step.llm_tool_calls", tool_calls=tc_ids)
            except Exception:
                pass

        async def on_tool_start(self, serialized, input_str, **kwargs):
            tool_name = serialized.get("name", "?")
            # Extract tool_call_id from kwargs (LangGraph passes it)
            tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
            safe_input = redact_pii(str(input_str)[:300])
            log.info("agent_step.tool_start",
                     tool=tool_name, tool_call_id=str(tool_call_id)[:36],
                     input=safe_input)

        async def on_tool_end(self, output, **kwargs):
            tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
            log.info("agent_step.tool_end",
                     tool_call_id=str(tool_call_id)[:36],
                     output=str(output)[:300])

        async def on_tool_error(self, error, **kwargs):
            tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
            log.warning("agent_step.tool_error",
                        tool_call_id=str(tool_call_id)[:36],
                        error=str(error)[:500])

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
            from app.core.engine.deep_agent_backend import DockerBackend

            backend = DockerBackend(sandbox) if sandbox is not None else None
            # PENTING: gunakan llm_raw (bukan llm yang sudah .bind()) —
            # DeepAgents SDK memanggil .count() pada model untuk parse nama provider,
            # yang gagal pada RunnableBinding dan menyebabkan AttributeError ditangkap
            # sebagai TypeError → fallback ke create_react_agent tanpa backend.
            graph = create_deep_agent(
                model=llm_raw,
                tools=tools,
                system_prompt=system_prompt,
                backend=backend,
                subagents=subagent_list or None,
            )
        except (ImportError, TypeError, AttributeError) as _dag_err:
            log.warning(
                "agent_run.deepagent_fallback",
                error=str(_dag_err)[:300],
                has_sandbox=sandbox is not None,
            )
            from langgraph.prebuilt import create_react_agent
            graph = create_react_agent(llm, tools=tools, prompt=system_prompt)

        if media_image_b64 and media_image_mime:
            human_content: Any = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": f"data:{media_image_mime};base64,{media_image_b64}"}},
            ]
        else:
            human_content = user_message

        # Sanitize prior_messages BEFORE building input — strip any AIMessages
        # from loaded history that have dangling tool_calls (no matching ToolMessage).
        # db_messages_to_lc() does not reconstruct ToolMessages from DB, so any
        # AIMessage reconstructed with tool_calls would immediately cause a
        # "No tool output found" rejection on the first provider call.
        # Applying sanitization here makes the normal path correct rather than
        # relying solely on the retry fallback.
        sanitized_prior = _sanitize_input_messages(prior_messages)
        if len(sanitized_prior) != len(prior_messages):
            log.warning(
                "agent_run.sanitized_prior_messages",
                original=len(prior_messages),
                sanitized=len(sanitized_prior),
            )

        input_messages: list[BaseMessage] = sanitized_prior + [HumanMessage(content=human_content)]
        step_counter = step_base + 1

        _graph_config = {
            "recursion_limit": settings.agent_max_steps * 8,
            "callbacks": [_AgentLogger()],
        }

        async def _cleanup_sandboxes() -> None:
            if sandbox:
                await sandbox.aclose()
            for _ssb in sub_sandboxes:
                await _ssb.aclose()

        try:
            async with asyncio.timeout(settings.agent_timeout_seconds):
                result = await graph.ainvoke({"messages": input_messages}, config=_graph_config)
        except asyncio.TimeoutError:
            log.error(
                "agent_run.timeout",
                timeout_seconds=settings.agent_timeout_seconds,
                session_id=str(session.id),
            )
            # Update Run → timed_out
            run_record.status = "timed_out"
            run_record.completed_at = datetime.now(timezone.utc)
            run_record.error_message = f"Timeout after {settings.agent_timeout_seconds}s"
            await db.flush()
            await _cleanup_sandboxes()
            raise
        except Exception as exc:
            err_str = str(exc)
            # "No tool output found for function call" means the provider received
            # an AIMessage with tool_calls but no matching ToolMessage. This can
            # happen when the Deep Agents SDK drops a tool result mid-graph (e.g.
            # tool exception before ToolMessage is written to state).
            #
            # Retry strategy: rebuild graph using LangGraph's built-in
            # create_react_agent (more reliable tool execution than Deep Agents SDK)
            # with sanitized input so history is clean.
            if "No tool output found for function call" in err_str:
                log.warning(
                    "agent_run.dangling_tool_call_retry",
                    error=err_str[:300],
                    input_msg_count=len(input_messages),
                )
                # Rebuild with create_react_agent as the fallback executor —
                # the Deep Agents SDK may have been the source of the dropped
                # tool result; LangGraph's ToolNode is the safer path here.
                try:
                    from langgraph.prebuilt import create_react_agent as _cra
                    _fallback_graph = _cra(llm, tools=tools, prompt=system_prompt)
                except Exception as _ge:
                    log.warning("agent_run.fallback_graph_build_failed", error=str(_ge))
                    _fallback_graph = graph

                clean_input = _sanitize_input_messages(input_messages)
                log.info(
                    "agent_run.dangling_tool_call_retry_with_fallback",
                    clean_msg_count=len(clean_input),
                )
                try:
                    async with asyncio.timeout(settings.agent_timeout_seconds):
                        result = await _fallback_graph.ainvoke(
                            {"messages": clean_input}, config=_graph_config
                        )
                    log.info("agent_run.dangling_tool_call_retry_ok")
                except Exception as retry_exc:
                    retry_err = str(retry_exc)
                    if "No tool output found for function call" in retry_err:
                        log.error(
                            "agent_run.dangling_tool_call_retry_failed",
                            error=retry_err[:300],
                        )
                        # Update Run → failed (dangling tool call)
                        run_record.status = "failed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        run_record.error_message = "Dangling tool call after retry"
                        await db.commit()
                        return AgentRunResult(
                            reply="Maaf, terjadi kesalahan internal. Silakan coba lagi.",
                            steps=[],
                            run_id=run_id,
                            tokens_used=0,
                        )
                    log.error("agent_run.retry_error", error=retry_err)
                    # Update Run → failed
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = retry_err[:2000]
                    await db.flush()
                    await _cleanup_sandboxes()
                    raise retry_exc
            else:
                log.error("agent_run.error", error=err_str)
                # Update Run → failed
                run_record.status = "failed"
                run_record.completed_at = datetime.now(timezone.utc)
                run_record.error_message = err_str[:2000]
                await db.flush()
                await _cleanup_sandboxes()
                raise

        # ------------------------------------------------------------------ #
        # 9. Parse & persist result messages                                  #
        # ------------------------------------------------------------------ #
        parsed: ParsedResult = parse_agent_result(
            result=result,
            input_messages=input_messages,
            session_id=session.id,
            run_id=run_id,
            step_start=step_counter,
            log=log,
        )
        final_reply = parsed["final_reply"]
        steps = parsed["steps"]
        total_tokens_used = parsed["total_tokens_used"]
        for _msg_record in parsed["db_messages"]:
            db.add(_msg_record)

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
        await sandbox.aclose()
    for _ssb in sub_sandboxes:
        await _ssb.aclose()

    if not final_reply:
        import asyncio as _asyncio
        import re as _re
        from app.core.infra import deployment_service as _svc

        _empty_llm = not parsed["has_output"]  # LLM tidak menghasilkan output sama sekali
        if _empty_llm:
            log.error(
                "agent_run.no_llm_output",
                session_id=str(session.id),
                run_id=str(run_id),
                user_message=user_message[:100],
            )

        # --- Cek URL deployment aktif ---
        try:
            status_info = await _asyncio.to_thread(_svc.get_deployment_status, str(session.id))
        except Exception as _ds_err:
            log.warning("agent_run.deployment_status_check_failed", error=str(_ds_err))
            status_info = {}

        _deploy_url: str | None = None
        _deploy_status = status_info.get("status", "")
        if _deploy_status in ("running", "degraded") and status_info.get("url"):
            _deploy_url = status_info["url"]

        # Fallback ke URL dari tool result run ini
        if not _deploy_url:
            _url_pat = _re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
            for step in steps:
                m = _url_pat.search(step.get("result", ""))
                if m:
                    _deploy_url = m.group(0)
                    break

        if _deploy_url:
            # Deployment sudah aktif — langsung share URL tanpa "Selesai."
            final_reply = f"App sudah live! Buka di sini: {_deploy_url}"
        elif not _empty_llm:
            # Agent sudah menjalankan tool calls tapi tidak menghasilkan text reply.
            # Invoke LLM langsung (tanpa graph) untuk meminta summary response.
            log.warning(
                "agent_run.missing_final_reply",
                steps=len(steps),
                run_id=str(run_id),
            )
            try:
                from langchain_core.messages import SystemMessage as _SM, ToolMessage as _TM
                _tool_summary = "\n".join(
                    f"- {s['tool']}: {s['result'][:200]}" for s in steps
                ) if steps else "(tidak ada tool yang dijalankan)"

                # Cek apakah ada deploy URL di tool results
                import re as _re2
                _cf_url_in_steps = None
                for _st in steps:
                    _mu = _re2.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", _st.get("result", ""))
                    if _mu:
                        _cf_url_in_steps = _mu.group(0)
                        break

                _deploy_hint = (
                    f"\n\nPENTING: App sudah berhasil di-deploy. URL publiknya adalah: {_cf_url_in_steps}\n"
                    f"Wajib sertakan URL ini di responmu."
                ) if _cf_url_in_steps else ""

                _summary_prompt = (
                    "Kamu baru saja menjalankan beberapa langkah. "
                    "Tulis respons singkat kepada user dalam Bahasa Indonesia — "
                    "jelaskan apa yang sudah dilakukan dan apa langkah selanjutnya. "
                    f"Jangan sebut nama tool secara teknis.{_deploy_hint}"
                )

                # Gunakan prior_messages (history bersih) + pesan user + prompt
                # JANGAN pakai result["messages"] — masih berisi AIMessage dengan
                # dangling tool_calls yang tidak punya pasangan ToolMessage,
                # sehingga LLM reject dengan error "No tool output found".
                _clean_msgs = list(prior_messages) + [
                    HumanMessage(content=user_message),
                    _SM(content=f"{_summary_prompt}\n\nHasil:\n{_tool_summary}"),
                ]
                _force_resp = await llm.ainvoke(_clean_msgs)
                _force_text = (
                    _force_resp.content
                    if isinstance(_force_resp.content, str)
                    else " ".join(
                        b.get("text", "") for b in _force_resp.content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                ).strip()
                if _force_text:
                    final_reply = _force_text
                    db.add(Message(
                        session_id=session.id,
                        role="agent",
                        content=final_reply,
                        step_index=step_counter,
                        run_id=run_id,
                    ))
                    await db.flush()
            except Exception as _fe:
                log.warning("agent_run.force_reply_failed", error=str(_fe))

        if not final_reply:
            final_reply = "Maaf, sepertinya ada gangguan sementara. Coba kirim pesan lagi."

    # Always inject Cloudflare URL if deploy happened this run but LLM forgot to include it.
    # The `if not final_reply` block above only runs as a fallback — if LLM generated any text
    # without the URL, we still need to surface it.
    import re as _re_url
    _cf_pat = _re_url.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
    if final_reply and not _cf_pat.search(final_reply):
        for _step in steps:
            _m = _cf_pat.search(_step.get("result", ""))
            if _m:
                final_reply = f"{final_reply}\n\n{_m.group(0)}"
                break

    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        tokens_used=total_tokens_used,
    )

    # Update Run → completed
    run_record.status = "completed"
    run_record.completed_at = datetime.now(timezone.utc)
    run_record.tokens_used = total_tokens_used
    await db.flush()

    return {"reply": final_reply, "steps": steps, "run_id": run_id, "tokens_used": total_tokens_used}

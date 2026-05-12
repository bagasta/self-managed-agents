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
import re
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
from app.core.domain.memory_service import build_memory_context, extract_long_term_memory, load_layered_memory
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
    build_heartbeat_tools,
    build_sandbox_binary_tool,
    build_skill_tools,
    build_tool_creator_tools,
    build_wa_agent_manager_tools,
    build_wa_notify_tool,
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
from app.core.engine.wa_progress import (
    build_progress_message as _build_progress_message,
    build_task_done_message as _build_task_done_message,
    parse_tool_input_payload as _parse_tool_input_payload,
)
from app.core.engine.reply_guard import ensure_non_empty_reply

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
    prior_run_was_interrupted: bool = False,
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
    # 0b. HITL resume — check if session has a pending interrupt          #
    # ------------------------------------------------------------------ #
    from app.core.engine import interrupt_store as _istore
    _pending = await _istore.get_interrupt(session.id)
    if _pending:
        # Use LLM to classify user intent naturally — no keyword matching.
        # The LLM sees the pending action and the user's reply, then decides:
        #   approve  → run the tool
        #   reject   → cancel it
        #   respond  → user asked a question / needs clarification before deciding
        _pending_action_requests = _pending.get("action_requests", [])
        _pending_tool = _pending_action_requests[0].get("name", "unknown") if _pending_action_requests else "unknown"
        _pending_args = _pending_action_requests[0].get("args", {}) if _pending_action_requests else {}

        _classify_prompt = (
            "You are a decision classifier. An AI agent was paused and asked the user for approval "
            f"before calling tool `{_pending_tool}` with args {_pending_args}.\n\n"
            f"The user replied: \"{user_message}\"\n\n"
            "Classify the user's intent as exactly one of:\n"
            "- APPROVE  (user wants to proceed)\n"
            "- REJECT   (user wants to cancel)\n"
            "- RESPOND  (user asked a question or wants clarification before deciding)\n\n"
            "Reply with only the single word: APPROVE, REJECT, or RESPOND."
        )
        try:
            _cls_llm = ChatOpenAI(
                model="openai/gpt-4o-mini",
                api_key=get_settings().openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
                temperature=0,
                max_tokens=10,
            )
            _cls_resp = await _cls_llm.ainvoke([HumanMessage(content=_classify_prompt)])
            _cls_text = (_cls_resp.content or "").strip().upper()
            if "APPROVE" in _cls_text:
                _decision_type: str | None = "approve"
            elif "REJECT" in _cls_text:
                _decision_type = "reject"
            elif "RESPOND" in _cls_text:
                _decision_type = "respond"
            else:
                _decision_type = None
        except Exception as _cls_err:
            log.warning("agent_run.hitl_classify_failed", error=str(_cls_err))
            _decision_type = None

        if _decision_type is not None:
            from langgraph.types import Command
            _resume_graph = _pending["graph"]
            _resume_ckpt = _pending["checkpointer"]
            _resume_thread = _pending["thread_id"]
            _resume_cfg = {
                "recursion_limit": (get_settings().agent_max_steps * 8),
                "configurable": {"thread_id": _resume_thread},
            }
            if _decision_type == "respond":
                _resume_cmd = Command(
                    resume={"decisions": [{"type": "respond", "message": user_message}]}
                )
            else:
                _resume_cmd = Command(
                    resume={"decisions": [{"type": _decision_type}]}
                )
            try:
                async with asyncio.timeout(get_settings().agent_timeout_seconds):
                    _resume_output = await _resume_graph.ainvoke(
                        _resume_cmd,
                        config=_resume_cfg,
                        version="v2",
                    )
                    _resume_state = await _resume_graph.aget_state(_resume_cfg)
                    _resume_result: dict = dict(_resume_state.values) if _resume_state else {}
                await _istore.clear_interrupt(session.id)
                # Parse result normally
                _r_interrupts = getattr(_resume_output, "interrupts", None)
                if _r_interrupts:
                    # Another interrupt — save again
                    _r_action_requests: list[dict] = []
                    for _ri in _r_interrupts:
                        _rv = getattr(_ri, "value", {}) or {}
                        _r_action_requests.extend(_rv.get("action_requests", []))
                    await _istore.save_interrupt(
                        session.id,
                        graph=_resume_graph,
                        checkpointer=_resume_ckpt,
                        thread_id=_resume_thread,
                        action_requests=_r_action_requests,
                    )
                    _tool_name2 = _r_action_requests[0].get("name", "") if _r_action_requests else ""
                    _tool_args2 = _r_action_requests[0].get("args", {}) if _r_action_requests else {}
                    _humanize2 = (
                        "You are a helpful assistant. Explain to the user (in the same language "
                        "they used) what you are about to do, in plain conversational language — "
                        "no tool names, no code, no technical jargon. Then ask if it's okay to proceed.\n\n"
                        f"Tool: {_tool_name2}\nArguments: {_tool_args2}\n\nKeep it to 1–2 sentences max."
                    )
                    try:
                        _h2_llm = ChatOpenAI(
                            model="openai/gpt-4o-mini",
                            api_key=get_settings().openrouter_api_key,
                            base_url="https://openrouter.ai/api/v1",
                            temperature=0.3,
                            max_tokens=100,
                        )
                        _h2_resp = await _h2_llm.ainvoke([HumanMessage(content=_humanize2)])
                        _ir_reply = (_h2_resp.content or "").strip() or "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"
                    except Exception:
                        _ir_reply = "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"
                    run_record.status = "interrupted"
                    run_record.completed_at = datetime.now(timezone.utc)
                    await db.flush()
                    return AgentRunResult(reply=_ir_reply, steps=[], run_id=run_id, tokens_used=0)

                from app.core.engine.result_parser import parse_agent_result as _par
                _rp = _par(
                    result=_resume_result,
                    input_messages=[HumanMessage(content=user_message)],
                    session_id=session.id,
                    run_id=run_id,
                    step_start=1,
                    log=log,
                )
                for _m in _rp["db_messages"]:
                    db.add(_m)
                run_record.status = "completed"
                run_record.completed_at = datetime.now(timezone.utc)
                await db.flush()
                return AgentRunResult(
                    reply=_rp["final_reply"] or ("Selesai." if _decision_type == "approve" else "Dibatalkan."),
                    steps=_rp["steps"],
                    run_id=run_id,
                    tokens_used=_rp["total_tokens_used"],
                )
            except Exception as _re_err:
                log.error("agent_run.resume_failed", error=str(_re_err))
                await _istore.clear_interrupt(session.id)
                run_record.status = "failed"
                run_record.completed_at = datetime.now(timezone.utc)
                run_record.error_message = str(_re_err)[:2000]
                await db.flush()
                return AgentRunResult(
                    reply="Maaf, gagal melanjutkan. Silakan coba lagi dari awal.",
                    steps=[],
                    run_id=run_id,
                    tokens_used=0,
                )
        else:
            # LLM couldn't classify — treat as new message, clear interrupt
            await _istore.clear_interrupt(session.id)
            log.info("agent_run.interrupt_cleared_unclassified", session_id=str(session.id))

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
        tools.extend(build_heartbeat_tools(agent_id, session.id, AsyncSessionLocal, scope=_memory_scope))
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
        tools.extend(build_escalation_tools(session.id, agent_id, AsyncSessionLocal, user_jid=_user_jid, sender_name=sender_name))
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
        tools.extend(build_wa_notify_tool(session))
        active_groups.append("wa_notify")
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
        _sub_ch: dict = session.channel_config if isinstance(session.channel_config, dict) else {}
        subagent_list, sub_sandboxes = await build_subagents(
            _sub_ids, session.id, db, log,
            wa_device_id=_sub_ch.get("device_id", ""),
            wa_target=_sub_ch.get("user_phone", ""),
        )
        if subagent_list:
            active_groups.append(f"subagents({len(subagent_list)})")
            log.info("agent_run.subagents_ready", names=[s.get("name", "?") for s in subagent_list])

            # Hard-block: parent can no longer call deployment tools when subagents
            # are active. These tools are session-scoped and always return empty for
            # parent (subagent runs in isolated session), causing parent to falsely
            # conclude "subagent failed" → re-delegate / fallback loop.
            # Sub-agents (sys_coder etc.) keep their own deployment tools.
            _deploy_tool_names = {
                "deploy_app", "stop_deployment",
                "get_deployment_status", "get_deployment_logs",
            }
            _before = len(tools)
            tools = [t for t in tools if getattr(t, "name", None) not in _deploy_tool_names]
            if len(tools) != _before:
                log.info(
                    "agent_run.parent_deploy_tools_stripped",
                    removed=_before - len(tools),
                    reason="subagents_active",
                )
                if "deploy" in active_groups:
                    active_groups.remove("deploy")

    log.debug("agent_run.tools_ready (pre-mcp)", groups=active_groups, count=len(tools))

    # ------------------------------------------------------------------ #
    # 5. Context enrichment                                               #
    # ------------------------------------------------------------------ #
    rag_context = ""
    if _is_enabled(tools_config, "rag", default=False):
        rag_context = await build_rag_context(agent_id, user_message, db, tools_config, log)

    context_summary = await maybe_summarize_context(session, db, llm, log)

    memory_block = await build_memory_context(agent_id, db, scope=_memory_scope)
    layered_memory = await load_layered_memory(agent_id, db, scope=_memory_scope)

    # When a context summary is already injected into the system prompt (triggered
    # after context_summary_trigger messages), loading the full short_term_memory_turns
    # is redundant — the summary covers older turns.  Reduce to half the configured
    # limit so the LLM context stays manageable on long sessions.
    _history_turns = (
        max(settings.short_term_memory_turns // 2, 5)
        if context_summary
        else settings.short_term_memory_turns
    )
    history_rows = await load_history(session.id, db, max_turns=_history_turns)
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
        layered_memory=layered_memory,
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

    # WA progress notify — only for WhatsApp sessions
    _ch_cfg: dict = session.channel_config if isinstance(session.channel_config, dict) else {}
    _wa_device_id: str = _ch_cfg.get("device_id", "")
    _wa_target: str = _ch_cfg.get("user_phone", "")
    _is_wa_session: bool = getattr(session, "channel_type", None) == "whatsapp" and bool(_wa_device_id and _wa_target)

    async def _send_agent_recovery_message(reason: str) -> None:
        """Jalankan quick 1-turn LLM call agar agent bisa kirim pesan recovery natural ke WA."""
        if not _is_wa_session:
            return
        try:
            from langchain_core.messages import HumanMessage as _HM, SystemMessage as _SM
            _recovery_llm = llm_raw.bind(max_tokens=200)
            _resp = await _recovery_llm.ainvoke([
                _SM(content=system_prompt if isinstance(system_prompt, str) else ""),
                _HM(content=(
                    f"[INTERNAL] Task kamu barusan {reason}. "
                    "Kirim pesan singkat dan natural ke user untuk memberi tahu bahwa prosesnya terganggu. "
                    "Tanyakan apakah mereka ingin melanjutkan atau mencoba lagi. "
                    "Jangan gunakan format robotik. Tulis langsung pesan untuk user, tidak lebih dari 2 kalimat."
                )),
            ])
            _msg = getattr(_resp, "content", "") or ""
            if _msg.strip():
                from app.core.infra.wa_client import send_wa_message as _send_wa
                await _send_wa(_wa_device_id, _wa_target, _msg.strip())
        except Exception as _re:
            log.warning("agent_run.recovery_message_failed", error=str(_re))

    class _AgentLogger(AsyncCallbackHandler):
        """Callback logger — log tiap step + kirim progress update ke WA.

        Also accumulates token usage from ALL LLM calls in the run, including
        sub-agent calls, so the quota deduction covers the full cost.
        """

        def __init__(self) -> None:
            self._notified: set[str] = set()   # tool types yang sudah dinotif run ini
            self._tool_inputs: dict[str, Any] = {}
            self._tool_names: dict[str, str] = {}
            self._task_done_notified: set[str] = set()
            self._last_ts: float = 0.0
            # Cumulative token counter — covers parent + all sub-agents
            self.total_tokens_from_callbacks: int = 0

        async def _wa_progress(self, msg: str, *, force: bool = False) -> None:
            """Kirim progress message ke WA dengan throttle 6 detik."""
            if not _is_wa_session:
                return
            import time as _time
            now = _time.monotonic()
            if not force and now - self._last_ts < 6:
                return
            self._last_ts = now
            try:
                from app.core.infra.wa_client import send_wa_message as _send
                await _send(_wa_device_id, _wa_target, msg)
            except Exception:
                pass

        async def on_llm_start(self, serialized, prompts, **kwargs):
            log.debug("agent_step.llm_thinking")

        async def on_llm_end(self, response, **kwargs):
            try:
                # Accumulate token usage — covers parent + sub-agent LLM calls
                usage = getattr(response, "llm_output", None) or {}
                token_usage = usage.get("token_usage") or usage.get("usage") or {}
                total = (
                    token_usage.get("total_tokens")
                    or token_usage.get("total_token_count")
                    or 0
                )
                if not total:
                    # Fallback: sum from individual generations
                    for gen_list in response.generations:
                        for g in gen_list:
                            ai_msg = getattr(g, "message", None)
                            if ai_msg:
                                u = getattr(ai_msg, "usage_metadata", None) or {}
                                total += u.get("total_tokens", 0)
                self.total_tokens_from_callbacks += total

                gen = response.generations[0][0]
                text = gen.text[:200] if gen.text else ""
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
            tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
            self._tool_inputs[str(tool_call_id)] = input_str
            self._tool_names[str(tool_call_id)] = str(tool_name)
            safe_input = redact_pii(str(input_str)[:300])
            log.info("agent_step.tool_start",
                     tool=tool_name, tool_call_id=str(tool_call_id)[:36],
                     input=safe_input)
            # Kirim progress ke WA
            progress_msg = _build_progress_message(tool_name, input_str)
            if progress_msg:
                if tool_name == "task":
                    # One-shot notification only — no heartbeat loop to avoid spam
                    await self._wa_progress(progress_msg, force=True)
                elif tool_name not in self._notified:
                    self._notified.add(tool_name)
                    await self._wa_progress(progress_msg)

        async def on_tool_end(self, output, **kwargs):
            tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
            log.info("agent_step.tool_end",
                     tool_call_id=str(tool_call_id)[:36],
                     output=str(output)[:300])
            input_payload = self._tool_inputs.get(str(tool_call_id))
            tool_name = self._tool_names.get(str(tool_call_id), "")
            task_done_msg = _build_task_done_message(input_payload, output)
            if tool_name == "task" and input_payload and task_done_msg and str(tool_call_id) not in self._task_done_notified:
                parsed = _parse_tool_input_payload(input_payload)
                if parsed.get("name") or parsed.get("task"):
                    self._task_done_notified.add(str(tool_call_id))
                    await self._wa_progress(task_done_msg, force=True)

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
            from langgraph.checkpoint.memory import MemorySaver
            from app.core.engine.deep_agent_backend import DockerBackend
            from app.core.engine import interrupt_store as _istore

            backend = DockerBackend(sandbox) if sandbox is not None else None
            _checkpointer = MemorySaver()
            # interrupt_on: tools_config["interrupt_on"] may be:
            #   - dict: {"tool_name": true}  → pass directly
            #   - list: ["tool_name"]         → convert to {name: True}
            _raw_interrupt_on = tools_config.get("interrupt_on") if isinstance(tools_config, dict) else None
            if isinstance(_raw_interrupt_on, dict) and _raw_interrupt_on:
                _interrupt_on: dict[str, bool] = _raw_interrupt_on
            elif isinstance(_raw_interrupt_on, list) and _raw_interrupt_on:
                _interrupt_on = {name: True for name in _raw_interrupt_on}
            else:
                _interrupt_on = {}
            # PENTING: gunakan llm_raw (bukan llm yang sudah .bind()) —
            # DeepAgents SDK memanggil .count() pada model untuk parse nama provider,
            # yang gagal pada RunnableBinding dan menyebabkan AttributeError ditangkap
            # sebagai TypeError → fallback ke create_react_agent tanpa backend.
            _dag_kwargs: dict[str, Any] = dict(
                model=llm_raw,
                tools=tools,
                system_prompt=system_prompt,
                backend=backend,
                subagents=subagent_list or None,
                checkpointer=_checkpointer,
            )
            if _interrupt_on:
                _dag_kwargs["interrupt_on"] = _interrupt_on
            graph = create_deep_agent(**_dag_kwargs)
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

        # Hard cap on context window size to prevent token explosion on long sessions.
        # Each "turn" in prior_messages can include AIMessage+ToolMessages pairs that
        # each add significant tokens.  We cap at 30 messages (≈15 conversation turns)
        # regardless of short_term_memory_turns setting.  When a context summary is
        # active (injected into system prompt), history older than this is redundant.
        _MAX_PRIOR_MESSAGES = 30
        if len(sanitized_prior) > _MAX_PRIOR_MESSAGES:
            log.debug(
                "agent_run.history_trimmed",
                original=len(sanitized_prior),
                trimmed=_MAX_PRIOR_MESSAGES,
            )
            sanitized_prior = sanitized_prior[-_MAX_PRIOR_MESSAGES:]

        # Detect & strip dangling-tail from a prior run that was interrupted, crashed,
        # or didn't produce a final assistant reply. We don't trust prior_run_was_interrupted
        # alone — it's only set when cancel_active_run() found a live task. If the prior
        # run already finished (timeout / error / partial commit) before this message
        # arrived, the flag is False even though the tail is dirty.
        #
        # Strategy: scan history_rows from the end and find the last "clean boundary" —
        # a row whose role is "user". Any AI/tool rows after the most-recent user row
        # without a final AI text reply (no tool_calls) are partial. Drop them. The
        # interrupt note tells the LLM to ignore them entirely.
        _interrupt_note: list[BaseMessage] = []
        _tail_dirty = False
        if history_rows:
            # Find last "clean" position. Walk backwards: if we see a final AI text
            # reply (role=agent, no tool_calls in content metadata) → clean. If we
            # see partial tool_calls or trailing tool messages → tail is dirty.
            _idx_last_user = -1
            for _i in range(len(history_rows) - 1, -1, -1):
                if history_rows[_i].role == "user":
                    _idx_last_user = _i
                    break

            _has_final_ai_reply_after_user = False
            if _idx_last_user >= 0:
                for _row in history_rows[_idx_last_user + 1:]:
                    # A "final AI reply" is an agent message that's pure text (not a
                    # tool call). We approximate: role=agent and content is non-empty
                    # and the row is the LAST agent row in the run. If any agent row
                    # after the last user has no following tool-call partner, treat as
                    # final. Heuristic: presence of any agent text reply after last
                    # user means the run completed at least one turn.
                    if _row.role == "agent" and _row.content and not str(_row.content).startswith("[tool_call]"):
                        _has_final_ai_reply_after_user = True
                        break

            _tail_dirty = _idx_last_user >= 0 and not _has_final_ai_reply_after_user
            if _tail_dirty:
                # Trim history to everything up to (and including) the last user row,
                # plus the agent's final reply (if any). Drop everything after that —
                # those are partial tool chains the LLM shouldn't try to continue.
                _kept_rows = history_rows[: _idx_last_user + 1] if _idx_last_user >= 0 else []
                # Include the first complete agent text reply right after, if present.
                for _row in history_rows[_idx_last_user + 1:]:
                    _kept_rows.append(_row)
                    if _row.role == "agent" and _row.content and not str(_row.content).startswith("[tool_call]"):
                        break
                if len(_kept_rows) < len(history_rows):
                    sanitized_prior = _sanitize_input_messages(db_messages_to_lc(_kept_rows))
                    log.info(
                        "agent_run.stripped_dirty_tail",
                        stripped=len(history_rows) - len(_kept_rows),
                        tail_dirty=True,
                    )

        # Inject interrupt note ONLY when tail is actually dirty (incomplete prior run).
        # If prior run completed cleanly (has final AI reply), skip the note — injecting
        # it when the tail is clean causes the agent to re-run the previous task.
        if _tail_dirty:
            from langchain_core.messages import SystemMessage as _SM
            _interrupt_note = [_SM(content=(
                "[SYSTEM — HARD OVERRIDE] Pesan baru dari user di bawah ini adalah PRIORITAS TUNGGAL. "
                "Task sebelumnya (jika ada) sudah dibatalkan. JANGAN melanjutkan, mengulang, atau "
                "menyebut pekerjaan lama kecuali user eksplisit bertanya tentangnya. "
                "Respon HANYA terhadap pesan user terbaru. "
                "Kalau user cuma sapa ('Halo', 'hai', 'bro'), balas sapaan singkat — JANGAN otomatis "
                "lanjut delegasi atau tool call apapun yang berkaitan dengan task lama."
            ))]

        input_messages: list[BaseMessage] = sanitized_prior + _interrupt_note + [HumanMessage(content=human_content)]
        step_counter = step_base + 1

        _agent_logger = _AgentLogger()
        _thread_id = str(session.id)
        _graph_config = {
            "recursion_limit": settings.agent_max_steps * 8,
            "callbacks": [_agent_logger],
            "configurable": {"thread_id": _thread_id},
        }

        async def _cleanup_sandboxes() -> None:
            if sandbox:
                await sandbox.aclose()
            for _ssb in sub_sandboxes:
                await _ssb.aclose()

        _agent_caps = getattr(agent_model, "capabilities", []) or []
        _has_subagents = bool(
            isinstance(tools_config, dict)
            and tools_config.get("subagents", {})
            and tools_config["subagents"].get("enabled")
        )
        # Agents that delegate to sys_coder may build framework projects (npm install,
        # next build, pip install) — these can take 5-10 min on cold sandboxes.
        # Multiplier 8x → ~40 min ceiling for builder/system/subagent-enabled flows.
        _timeout = (
            settings.agent_timeout_seconds * 8
            if "builder" in _agent_caps or "system" in _agent_caps or _has_subagents
            else settings.agent_timeout_seconds
        )
        try:
            async with asyncio.timeout(_timeout):
                _graph_output = await graph.ainvoke(
                    {"messages": input_messages},
                    config=_graph_config,
                    version="v2",
                )
                # GraphOutput (version="v2") only carries .interrupts; get the
                # actual state dict (with "messages") from the checkpointer.
                _state = await graph.aget_state(_graph_config)
                result: dict = dict(_state.values) if _state else {}
        except asyncio.CancelledError:
            # Human interrupt — user sent a new message while this run was active.
            log.info("agent_run.cancelled_by_interrupt", session_id=str(session.id))
            await _cleanup_sandboxes()
            raise  # propagate so the task is properly marked cancelled
        except asyncio.TimeoutError:
            log.error(
                "agent_run.timeout",
                timeout_seconds=_timeout,
                session_id=str(session.id),
            )
            run_record.status = "timed_out"
            run_record.completed_at = datetime.now(timezone.utc)
            run_record.error_message = f"Timeout after {_timeout}s"
            await db.flush()
            await _cleanup_sandboxes()
            await _send_agent_recovery_message("memakan waktu terlalu lama dan terpaksa dihentikan")
            raise
        except Exception as exc:
            err_str = str(exc)

            # JSONDecodeError inside a subagent = OpenRouter returned a truncated
            # HTTP response (network hiccup). Retry the same graph once with a brief
            # delay — no need to rebuild or sanitize messages.
            import json as _json_mod
            if isinstance(exc.__cause__, _json_mod.JSONDecodeError) or isinstance(exc, _json_mod.JSONDecodeError) or "JSONDecodeError" in type(exc).__name__ or (exc.__context__ and isinstance(exc.__context__, _json_mod.JSONDecodeError)):
                log.warning("agent_run.subagent_json_error_retry", error=err_str[:200])
                try:
                    await asyncio.sleep(1)
                    async with asyncio.timeout(_timeout):
                        _graph_output = await graph.ainvoke(
                            {"messages": input_messages},
                            config=_graph_config,
                            version="v2",
                        )
                        _state = await graph.aget_state(_graph_config)
                        result = dict(_state.values) if _state else {}
                    log.info("agent_run.subagent_json_error_retry_ok")
                except Exception as _retry_json_exc:
                    log.error("agent_run.subagent_json_error_retry_failed", error=str(_retry_json_exc)[:300])
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = str(_retry_json_exc)[:2000]
                    await db.flush()
                    await _cleanup_sandboxes()
                    await _send_agent_recovery_message("mengalami gangguan koneksi ke model")
                    return AgentRunResult(
                        reply="Maaf, terjadi gangguan koneksi ke model. Silakan coba lagi.",
                        steps=[],
                        run_id=run_id,
                        tokens_used=0,
                    )

            # "No tool output found for function call" means the provider received
            # an AIMessage with tool_calls but no matching ToolMessage. This can
            # happen when the Deep Agents SDK drops a tool result mid-graph (e.g.
            # tool exception before ToolMessage is written to state).
            #
            # Retry strategy: rebuild graph using LangGraph's built-in
            # create_react_agent (more reliable tool execution than Deep Agents SDK)
            # with sanitized input so history is clean.
            elif "No tool output found for function call" in err_str:
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
        # 9a. Handle HITL interrupt (version="v2" result object)              #
        # ------------------------------------------------------------------ #
        _interrupts = getattr(_graph_output, "interrupts", None)
        if _interrupts:
            # Graph paused — awaiting human approval before sensitive tool call.
            # Save state so the next user message can resume.
            try:
                _action_requests: list[dict] = []
                for _intr in _interrupts:
                    _val = getattr(_intr, "value", {}) or {}
                    _ar = _val.get("action_requests", [])
                    _action_requests.extend(_ar)
                await _istore.save_interrupt(
                    session.id,
                    graph=graph,
                    checkpointer=_checkpointer,
                    thread_id=_thread_id,
                    action_requests=_action_requests,
                )
                # Build approval-request message — natural language, no tech jargon
                if _action_requests:
                    _tool_name = _action_requests[0].get("name", "")
                    _tool_args = _action_requests[0].get("args", {})
                    _humanize_prompt = (
                        "You are a helpful assistant. Explain to the user (in the same language "
                        "they used) what you are about to do, in plain conversational language — "
                        "no tool names, no code, no technical jargon. Then ask if it's okay to proceed.\n\n"
                        f"Tool: {_tool_name}\n"
                        f"Arguments: {_tool_args}\n\n"
                        "Keep it to 1–2 sentences max."
                    )
                    try:
                        _h_llm = ChatOpenAI(
                            model="openai/gpt-4o-mini",
                            api_key=get_settings().openrouter_api_key,
                            base_url="https://openrouter.ai/api/v1",
                            temperature=0.3,
                            max_tokens=100,
                        )
                        _h_msgs = list(prior_messages[-4:]) + [HumanMessage(content=user_message), HumanMessage(content=_humanize_prompt)]
                        _h_resp = await _h_llm.ainvoke(_h_msgs)
                        _interrupt_reply = (_h_resp.content or "").strip() or "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"
                    except Exception:
                        _interrupt_reply = "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"
                else:
                    _interrupt_reply = "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"
            except Exception as _ie:
                log.warning("agent_run.interrupt_save_failed", error=str(_ie))
                _interrupt_reply = "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"

            run_record.status = "interrupted"
            run_record.completed_at = datetime.now(timezone.utc)
            await db.flush()
            await _cleanup_sandboxes()
            return AgentRunResult(
                reply=_interrupt_reply,
                steps=[],
                run_id=run_id,
                tokens_used=0,
            )

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
        # Prefer callback-based counter — it captures sub-agent LLM calls too.
        # Fall back to result_parser count if callback produced nothing (e.g. mocked LLM).
        _cb_tokens = _agent_logger.total_tokens_from_callbacks
        total_tokens_used = _cb_tokens if _cb_tokens > 0 else parsed["total_tokens_used"]
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
        _empty_llm = not parsed["has_output"]
        if _empty_llm:
            log.error(
                "agent_run.no_llm_output",
                session_id=str(session.id),
                run_id=str(run_id),
                user_message=user_message[:100],
            )
        else:
            log.warning(
                "agent_run.missing_final_reply",
                steps=len(steps),
                run_id=str(run_id),
            )

    final_reply = ensure_non_empty_reply(final_reply, steps)

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

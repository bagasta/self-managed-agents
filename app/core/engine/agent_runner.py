"""Main agent orchestration entry point.

`run_agent()` coordinates run records, LLM setup, tools, prompt/context,
graph execution, result persistence, and post-run memory extraction. Detailed
tool setup, HITL handling, MCP support, callbacks, and input preparation live
in sibling modules.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypedDict

import structlog
from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.engine.context_service import count_user_messages, db_messages_to_lc, load_history
from app.core.domain.memory_service import build_memory_context, extract_long_term_memory, load_layered_memory
from app.core.engine.prompt_builder import build_rag_context, build_system_prompt, maybe_summarize_context
from app.core.engine.tool_builder import _is_enabled
from app.core.engine.agent_callbacks import AgentStepLogger
from app.core.engine.agent_hitl import handle_graph_interrupt, handle_pending_interrupt
from app.core.engine.agent_input import build_input_messages
from app.core.engine.agent_llm import build_agent_llms
from app.core.engine.agent_recovery import send_agent_recovery_message
from app.core.engine.agent_tool_setup import build_agent_tool_setup
from app.models.agent import Agent as AgentModel
from app.models.message import Message
from app.models.run import Run
from app.models.session import Session
from app.core.engine.result_parser import (
    ParsedResult,
    sanitize_input_messages as _sanitize_input_messages,
    parse_agent_result,
)
from app.core.engine.reply_guard import ensure_non_empty_reply
from app.core.engine.google_mcp_support import (
    _build_google_mcp_auth_failure_reply,
    _build_google_mcp_unavailable_reply,
    _build_google_mcp_validation_reply,
    _candidate_external_user_ids,
    _extract_google_mcp_step_error,
    _extract_requested_slide_count,
    _fetch_google_auth_link,
    _is_google_auth_or_scope_error,
    _is_google_forms_authoring_intent,
    _is_google_mcp_intent,
    _is_google_sheets_authoring_intent,
    _is_google_slides_relayout_intent,
    _looks_like_progress_claim,
    _needs_google_forms_followup,
    _needs_google_sheets_followup,
    _needs_google_slides_followup,
    apply_google_mcp_reply_overrides,
    apply_mcp_error_notice,
    google_forms_create_retry_directive,
    google_forms_followup_directive,
    google_forms_followup_retry_directive,
    google_forms_request_kind_retry_directive,
    google_sheets_followup_directive,
    prepare_google_mcp_runtime,
    sanitize_google_forms_tools,
    google_slides_dimension_retry_directive,
    google_slides_followup_directive,
    google_slides_shape_retry_directive,
)

logger = structlog.get_logger(__name__)
settings = get_settings()




class AgentRunResult(TypedDict):
    reply: str
    steps: list[dict]
    run_id: uuid.UUID
    tokens_used: int
    usage: dict[str, Any]


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

    # HITL action_requests use .get("name", ...) and .get("args", ...);
    # compatibility tests also assert .get("name", ...) remains documented here.
    resumed_result = await handle_pending_interrupt(
        session=session,
        user_message=user_message,
        db=db,
        run_record=run_record,
        run_id=run_id,
        log=log,
    )
    if resumed_result is not None:
        return AgentRunResult(**resumed_result)

    llm_raw, llm = build_agent_llms(agent_model, settings, temperature)

    # Tool setup lives in agent_tool_setup.py; it still gates builder tools via
    # capabilities and build_builder_tools.
    tool_setup = await build_agent_tool_setup(
        agent_model=agent_model,
        session=session,
        tools_config=tools_config,
        raw_tools_config=_raw_tools_cfg,
        db=db,
        log=log,
        escalation_user_jid=escalation_user_jid,
        sender_name=sender_name,
        user_message=user_message,
    )
    tools = tool_setup.tools
    active_groups = tool_setup.active_groups
    saved_custom_tools = tool_setup.saved_custom_tools
    sandbox = tool_setup.sandbox
    subagent_list = tool_setup.subagent_list
    sub_sandboxes = tool_setup.sub_sandboxes
    _memory_scope = tool_setup.memory_scope

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

    # WA progress notify — only for WhatsApp sessions
    _ch_cfg: dict = session.channel_config if isinstance(session.channel_config, dict) else {}
    _wa_device_id: str = _ch_cfg.get("device_id", "")
    _wa_target: str = _ch_cfg.get("user_phone", "")
    _is_wa_session: bool = getattr(session, "channel_type", None) == "whatsapp" and bool(_wa_device_id and _wa_target)

    google_mcp = await prepare_google_mcp_runtime(
        tools_config=tools_config,
        tools=tools,
        active_groups=active_groups,
        session=session,
        agent_id=agent_id,
        memory_scope=_memory_scope,
        api_key=settings.api_key,
        user_message=user_message,
        system_prompt=system_prompt,
        log=log,
    )
    system_prompt = google_mcp.system_prompt
    _google_mcp_auth_url = google_mcp.auth_url

    async with mcp_client_context(tools_config) as (mcp_tools, mcp_errors):
        if google_mcp.preflight_error and "google_workspace" not in mcp_errors:
            mcp_errors["google_workspace"] = google_mcp.preflight_error
        if mcp_tools:
            mcp_tools = sanitize_google_forms_tools(mcp_tools, log)
            tools = tools + mcp_tools
            active_groups.append(f"mcp({len(mcp_tools)} tools)")
            log.debug("agent_run.mcp_tools_added", count=len(mcp_tools))
        if mcp_errors:
            log.warning("agent_run.mcp_errors", errors=mcp_errors)
            _google_mcp_auth_url, system_prompt = await apply_mcp_error_notice(
                mcp_errors=mcp_errors,
                runtime=google_mcp,
                agent_id=agent_id,
                memory_scope=_memory_scope,
                api_key=settings.api_key,
                system_prompt=system_prompt,
                log=log,
            )

        backend = None
        try:
            from deepagents import create_deep_agent
            from langgraph.checkpoint.memory import MemorySaver
            from app.core.engine.deep_agent_backend import DockerBackend

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
            if sandbox is not None or backend is not None or subagent_list:
                log.error(
                    "agent_run.deepagent_required_failed",
                    error=str(_dag_err)[:300],
                    has_backend=backend is not None,
                    has_sandbox=sandbox is not None,
                    subagents=len(subagent_list or []),
                )
                raise
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

        input_messages: list[BaseMessage] = build_input_messages(
            prior_messages=prior_messages,
            history_rows=history_rows,
            human_content=human_content,
            log=log,
        )
        step_counter = step_base + 1

        _agent_logger = AgentStepLogger(log)

        def _usage_summary() -> dict[str, Any]:
            return {
                "prompt_tokens": _agent_logger.prompt_tokens_from_callbacks,
                "completion_tokens": _agent_logger.completion_tokens_from_callbacks,
                "reasoning_tokens": _agent_logger.reasoning_tokens_from_callbacks,
                "cached_tokens": _agent_logger.cached_tokens_from_callbacks,
                "total_tokens": _agent_logger.total_tokens_from_callbacks,
                "openrouter_cost_usd": round(_agent_logger.openrouter_cost_usd_from_callbacks, 8),
                "details": _agent_logger.usage_details,
            }

        def _apply_run_usage(total_tokens: int) -> None:
            summary = _usage_summary()
            run_record.tokens_used = int(total_tokens or summary["total_tokens"] or 0)
            run_record.prompt_tokens = int(summary["prompt_tokens"] or 0)
            run_record.completion_tokens = int(summary["completion_tokens"] or 0)
            run_record.reasoning_tokens = int(summary["reasoning_tokens"] or 0)
            run_record.cached_tokens = int(summary["cached_tokens"] or 0)
            run_record.openrouter_cost_usd = Decimal(str(summary["openrouter_cost_usd"] or 0))
            run_record.usage_details = summary["details"] or None

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

        # Initialize so UnboundLocalError can't happen if an exception bypasses parse_agent_result
        final_reply: str = ""
        steps: list = []
        total_tokens_used: int = 0
        parsed: dict = {"final_reply": "", "steps": [], "total_tokens_used": 0, "has_output": False, "db_messages": []}

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
            await send_agent_recovery_message(
                is_wa_session=_is_wa_session,
                wa_device_id=_wa_device_id,
                wa_target=_wa_target,
                llm_raw=llm_raw,
                system_prompt=system_prompt,
                reason="memakan waktu terlalu lama dan terpaksa dihentikan",
                log=log,
            )
            raise
        except Exception as exc:
            err_str = str(exc)
            # Log sub-exceptions from ExceptionGroup for debugging
            if hasattr(exc, "exceptions"):
                for _sub in exc.exceptions:
                    log.error("agent_run.exception_group_sub", sub_type=type(_sub).__name__, sub_error=str(_sub))

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
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    await send_agent_recovery_message(
                        is_wa_session=_is_wa_session,
                        wa_device_id=_wa_device_id,
                        wa_target=_wa_target,
                        llm_raw=llm_raw,
                        system_prompt=system_prompt,
                        reason="mengalami gangguan koneksi ke model",
                        log=log,
                    )
                    return AgentRunResult(
                        reply="Maaf, terjadi gangguan koneksi ke model. Silakan coba lagi.",
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
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
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.commit()
                        return AgentRunResult(
                            reply="Maaf, terjadi kesalahan internal. Silakan coba lagi.",
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )
                    log.error("agent_run.retry_error", error=retry_err)
                    # Update Run → failed
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = retry_err[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    raise retry_exc
            else:
                _recovered_via_retry = False
                _slides_invalid_page_target = (
                    "error calling tool 'batch_update_presentation'" in err_str.lower()
                    and "invalid slides batch update request" in err_str.lower()
                    and "targets a slide/page object" in err_str.lower()
                )

                _slides_invalid_dimension = (
                    "error calling tool 'batch_update_presentation'" in err_str.lower()
                    and (
                        "invalid value" in err_str.lower()
                        or "unknown dimension unit" in err_str.lower()
                        or "unit_unspecified" in err_str.lower()
                    )
                    and "dimension" in err_str.lower()
                    and ("create_shape" in err_str.lower() or "createshape" in err_str.lower())
                )

                if _slides_invalid_dimension:
                    log.warning(
                        "agent_run.slides_dimension_retry",
                        error=err_str[:300],
                    )
                    _slides_dim_retry_directive = google_slides_dimension_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _slides_prompt = (
                            (system_prompt + "\n\n" + _slides_dim_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                        _slides_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _slides_graph.ainvoke(
                                {"messages": _slides_input}, config=_graph_config
                            )
                        log.info("agent_run.slides_dimension_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _slides_retry_exc:
                        log.warning(
                            "agent_run.slides_dimension_retry_failed",
                            error=str(_slides_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _slides_invalid_page_target:
                    log.warning(
                        "agent_run.slides_shape_retry",
                        error=err_str[:300],
                    )
                    _slides_retry_directive = google_slides_shape_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _slides_prompt = (
                            (system_prompt + "\n\n" + _slides_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                        _slides_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _slides_graph.ainvoke(
                                {"messages": _slides_input}, config=_graph_config
                            )
                        log.info("agent_run.slides_shape_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _slides_retry_exc:
                        log.warning(
                            "agent_run.slides_shape_retry_failed",
                            error=str(_slides_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                _forms_create_title_only_error = (
                    "error calling tool 'create_form'" in err_str.lower()
                    and "only info.title can be set when creating a form" in err_str.lower()
                )
                _forms_request_kind_error = (
                    "error calling tool 'batch_update_form'" in err_str.lower()
                    and "request kind was not provided" in err_str.lower()
                )

                if _forms_create_title_only_error:
                    log.warning(
                        "agent_run.forms_create_retry",
                        error=err_str[:300],
                    )
                    _forms_retry_directive = google_forms_create_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_prompt = (
                            (system_prompt + "\n\n" + _forms_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_graph = _cra(llm, tools=tools, prompt=_forms_prompt)
                        _forms_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_graph.ainvoke(
                                {"messages": _forms_input}, config=_graph_config
                            )
                        log.info("agent_run.forms_create_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _forms_retry_exc:
                        log.warning(
                            "agent_run.forms_create_retry_failed",
                            error=str(_forms_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _forms_request_kind_error:
                    log.warning(
                        "agent_run.forms_request_kind_retry",
                        error=err_str[:300],
                    )
                    _forms_kind_retry_directive = google_forms_request_kind_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_kind_prompt = (
                            (system_prompt + "\n\n" + _forms_kind_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_kind_graph = _cra(llm, tools=tools, prompt=_forms_kind_prompt)
                        _forms_kind_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_kind_graph.ainvoke(
                                {"messages": _forms_kind_input}, config=_graph_config
                            )
                        log.info("agent_run.forms_request_kind_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _forms_kind_retry_exc:
                        log.warning(
                            "agent_run.forms_request_kind_retry_failed",
                            error=str(_forms_kind_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _recovered_via_retry:
                    log.info("agent_run.retry_recovered_continue")
                elif (
                    "validation error for call[batch_update_presentation]" in err_str.lower()
                    or ("missing required argument" in err_str.lower() and "requests" in err_str.lower())
                    or (
                        "error calling tool 'batch_update_presentation'" in err_str.lower()
                        and "invalid slides batch update request" in err_str.lower()
                    )
                    or (
                        "error calling tool 'create_form'" in err_str.lower()
                        and "only info.title can be set when creating a form" in err_str.lower()
                    )
                    or (
                        "error calling tool 'batch_update_form'" in err_str.lower()
                        and "request kind was not provided" in err_str.lower()
                    )
                ):
                    _reply = _build_google_mcp_validation_reply(err_str)
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    return AgentRunResult(
                        reply=_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

                if (not _recovered_via_retry) and _is_google_auth_or_scope_error(err_str):
                    if not _google_mcp_auth_url:
                        _google_mcp_auth_url = await _fetch_google_auth_link(
                            integration_url=google_mcp.integration_url,
                            api_key=settings.api_key,
                            agent_id=agent_id,
                            candidate_user_ids=google_mcp.candidate_user_ids,
                        )
                    _reply = await _build_google_mcp_auth_failure_reply(
                        llm=llm_raw,
                        user_message=user_message,
                        error_text=err_str,
                        auth_url=_google_mcp_auth_url,
                    )
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    return AgentRunResult(
                        reply=_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

                if not _recovered_via_retry:
                    log.error("agent_run.error", error=err_str)
                    # Update Run → failed
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = err_str[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    raise


        interrupt_result = await handle_graph_interrupt(
            graph_output=_graph_output,
            graph=graph,
            checkpointer=_checkpointer,
            thread_id=_thread_id,
            session=session,
            db=db,
            run_record=run_record,
            run_id=run_id,
            prior_messages=prior_messages,
            user_message=user_message,
            cleanup_sandboxes=_cleanup_sandboxes,
            log=log,
        )
        if interrupt_result is not None:
            return AgentRunResult(**interrupt_result)

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

        _needs_forms_followup, _followup_form_id = _needs_google_forms_followup(user_message, steps)
        if _needs_forms_followup and _followup_form_id:
            log.info("agent_run.forms_followup_continue", form_id=_followup_form_id)
            _forms_followup_directive = google_forms_followup_directive(_followup_form_id)
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _forms_prompt = (
                    (system_prompt + "\n\n" + _forms_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _forms_graph = _cra(llm, tools=tools, prompt=_forms_prompt)
                _forms_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _forms_graph.ainvoke(
                        {"messages": _forms_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _forms_followup_exc:
                _forms_followup_err = str(_forms_followup_exc)
                log.warning("agent_run.forms_followup_continue_failed", error=_forms_followup_err[:300], form_id=_followup_form_id)
                _is_missing_requests_err = (
                    "validation error for call[batch_update_form]" in _forms_followup_err.lower()
                    and "missing required argument" in _forms_followup_err.lower()
                    and "requests" in _forms_followup_err.lower()
                )
                if _is_missing_requests_err:
                    _forms_followup_retry_directive = google_forms_followup_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_prompt_retry = (
                            (system_prompt + "\n\n" + _forms_followup_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_graph_retry = _cra(llm, tools=tools, prompt=_forms_prompt_retry)
                        _forms_input_retry = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_graph_retry.ainvoke(
                                {"messages": _forms_input_retry}, config=_graph_config
                            )
                        parsed = parse_agent_result(
                            result=result,
                            input_messages=input_messages,
                            session_id=session.id,
                            run_id=run_id,
                            step_start=step_counter,
                            log=log,
                        )
                        final_reply = parsed["final_reply"]
                        steps = parsed["steps"]
                        total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                        for _msg_record in parsed["db_messages"]:
                            db.add(_msg_record)
                    except Exception as _forms_followup_retry_exc:
                        log.warning(
                            "agent_run.forms_followup_retry_failed",
                            error=str(_forms_followup_retry_exc)[:300],
                            form_id=_followup_form_id,
                        )

        _needs_slides_followup, _followup_presentation_id = _needs_google_slides_followup(user_message, steps)
        if _needs_slides_followup and _followup_presentation_id:
            log.info("agent_run.slides_followup_continue", presentation_id=_followup_presentation_id)
            _slides_followup_directive = google_slides_followup_directive(
                _followup_presentation_id, user_message
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _slides_prompt = (
                    (system_prompt + "\n\n" + _slides_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                _slides_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _slides_graph.ainvoke(
                        {"messages": _slides_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _slides_followup_exc:
                log.warning(
                    "agent_run.slides_followup_continue_failed",
                    error=str(_slides_followup_exc)[:300],
                    presentation_id=_followup_presentation_id,
                )

        _needs_sheets_followup, _followup_spreadsheet_id = _needs_google_sheets_followup(user_message, steps)
        if _needs_sheets_followup and _followup_spreadsheet_id:
            log.info("agent_run.sheets_followup_continue", spreadsheet_id=_followup_spreadsheet_id)
            _sheets_followup_directive = google_sheets_followup_directive(
                _followup_spreadsheet_id, user_message
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _sheets_prompt = (
                    (system_prompt + "\n\n" + _sheets_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _sheets_graph = _cra(llm, tools=tools, prompt=_sheets_prompt)
                _sheets_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _sheets_graph.ainvoke(
                        {"messages": _sheets_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _sheets_followup_exc:
                log.warning(
                    "agent_run.sheets_followup_continue_failed",
                    error=str(_sheets_followup_exc)[:300],
                    spreadsheet_id=_followup_spreadsheet_id,
                )

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

    final_reply, steps, _google_mcp_auth_url = await apply_google_mcp_reply_overrides(
        final_reply=final_reply,
        steps=steps,
        mcp_errors=mcp_errors,
        runtime=google_mcp,
        auth_url=_google_mcp_auth_url,
        llm_raw=llm_raw,
        user_message=user_message,
        agent_id=agent_id,
        api_key=settings.api_key,
        log=log,
    )

    final_reply = ensure_non_empty_reply(final_reply, steps)

    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        tokens_used=total_tokens_used,
        prompt_tokens=_agent_logger.prompt_tokens_from_callbacks,
        completion_tokens=_agent_logger.completion_tokens_from_callbacks,
        openrouter_cost_usd=round(_agent_logger.openrouter_cost_usd_from_callbacks, 8),
    )

    # Update Run → completed
    run_record.status = "completed"
    run_record.completed_at = datetime.now(timezone.utc)
    _apply_run_usage(total_tokens_used)
    await db.flush()

    return {
        "reply": final_reply,
        "steps": steps,
        "run_id": run_id,
        "tokens_used": total_tokens_used,
        "usage": _usage_summary(),
    }

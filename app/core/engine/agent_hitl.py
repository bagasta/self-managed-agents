"""Human-in-the-loop resume handling for paused agent runs."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.engine import interrupt_store
from app.core.engine.result_parser import parse_agent_result
from app.models.run import Run
from app.models.session import Session


async def handle_pending_interrupt(
    *,
    session: Session,
    user_message: str,
    db: AsyncSession,
    run_record: Run,
    run_id: uuid.UUID,
    log: Any,
) -> dict[str, Any] | None:
    pending = await interrupt_store.get_interrupt(session.id)
    if not pending:
        return None

    pending_action_requests = pending.get("action_requests", [])
    pending_tool = pending_action_requests[0].get("name", "unknown") if pending_action_requests else "unknown"
    pending_args = pending_action_requests[0].get("args", {}) if pending_action_requests else {}
    decision_type = await _classify_hitl_decision(
        user_message=user_message,
        pending_tool=pending_tool,
        pending_args=pending_args,
        log=log,
    )

    if decision_type is None:
        await interrupt_store.clear_interrupt(session.id)
        log.info("agent_run.interrupt_cleared_unclassified", session_id=str(session.id))
        return None

    try:
        from langgraph.types import Command

        resume_graph = pending["graph"]
        resume_ckpt = pending["checkpointer"]
        resume_thread = pending["thread_id"]
        resume_cfg = {
            "recursion_limit": (get_settings().agent_max_steps * 8),
            "configurable": {"thread_id": resume_thread},
        }
        if decision_type == "respond":
            resume_cmd = Command(
                resume={"decisions": [{"type": "respond", "message": user_message}]}
            )
        else:
            resume_cmd = Command(resume={"decisions": [{"type": decision_type}]})

        async with asyncio.timeout(get_settings().agent_timeout_seconds):
            resume_output = await resume_graph.ainvoke(
                resume_cmd,
                config=resume_cfg,
                version="v2",
            )
            resume_state = await resume_graph.aget_state(resume_cfg)
            resume_result: dict = dict(resume_state.values) if resume_state else {}
        await interrupt_store.clear_interrupt(session.id)

        interrupts = getattr(resume_output, "interrupts", None)
        if interrupts:
            action_requests: list[dict] = []
            for interrupt in interrupts:
                value = getattr(interrupt, "value", {}) or {}
                action_requests.extend(value.get("action_requests", []))
            await interrupt_store.save_interrupt(
                session.id,
                graph=resume_graph,
                checkpointer=resume_ckpt,
                thread_id=resume_thread,
                action_requests=action_requests,
            )
            reply = await _build_interrupt_confirmation_reply(action_requests)
            run_record.status = "interrupted"
            run_record.completed_at = datetime.now(timezone.utc)
            await db.flush()
            return {"reply": reply, "steps": [], "run_id": run_id, "tokens_used": 0}

        parsed = parse_agent_result(
            result=resume_result,
            input_messages=[HumanMessage(content=user_message)],
            session_id=session.id,
            run_id=run_id,
            step_start=1,
            log=log,
        )
        for message in parsed["db_messages"]:
            db.add(message)
        run_record.status = "completed"
        run_record.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return {
            "reply": parsed["final_reply"] or ("Selesai." if decision_type == "approve" else "Dibatalkan."),
            "steps": parsed["steps"],
            "run_id": run_id,
            "tokens_used": parsed["total_tokens_used"],
        }
    except Exception as err:
        log.error("agent_run.resume_failed", error=str(err))
        await interrupt_store.clear_interrupt(session.id)
        run_record.status = "failed"
        run_record.completed_at = datetime.now(timezone.utc)
        run_record.error_message = str(err)[:2000]
        await db.flush()
        return {
            "reply": "Maaf, gagal melanjutkan. Silakan coba lagi dari awal.",
            "steps": [],
            "run_id": run_id,
            "tokens_used": 0,
        }


async def handle_graph_interrupt(
    *,
    graph_output: Any,
    graph: Any,
    checkpointer: Any,
    thread_id: str,
    session: Session,
    db: AsyncSession,
    run_record: Run,
    run_id: uuid.UUID,
    prior_messages: list[Any],
    user_message: str,
    cleanup_sandboxes: Any,
    log: Any,
) -> dict[str, Any] | None:
    interrupts = getattr(graph_output, "interrupts", None)
    if not interrupts:
        return None

    try:
        action_requests: list[dict] = []
        for interrupt in interrupts:
            value = getattr(interrupt, "value", {}) or {}
            action_requests.extend(value.get("action_requests", []))
        await interrupt_store.save_interrupt(
            session.id,
            graph=graph,
            checkpointer=checkpointer,
            thread_id=thread_id,
            action_requests=action_requests,
        )
        reply = await _build_interrupt_confirmation_reply(
            action_requests,
            prior_messages=prior_messages,
            user_message=user_message,
        )
    except Exception as err:
        log.warning("agent_run.interrupt_save_failed", error=str(err))
        reply = "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"

    run_record.status = "interrupted"
    run_record.completed_at = datetime.now(timezone.utc)
    await db.flush()
    await cleanup_sandboxes()
    return {"reply": reply, "steps": [], "run_id": run_id, "tokens_used": 0}


async def _classify_hitl_decision(
    *,
    user_message: str,
    pending_tool: str,
    pending_args: dict[str, Any],
    log: Any,
) -> str | None:
    prompt = (
        "You are a decision classifier. An AI agent was paused and asked the user for approval "
        f"before calling tool `{pending_tool}` with args {pending_args}.\n\n"
        f"The user replied: \"{user_message}\"\n\n"
        "Classify the user's intent as exactly one of:\n"
        "- APPROVE  (user wants to proceed)\n"
        "- REJECT   (user wants to cancel)\n"
        "- RESPOND  (user asked a question or wants clarification before deciding)\n\n"
        "Reply with only the single word: APPROVE, REJECT, or RESPOND."
    )
    try:
        llm = ChatOpenAI(
            model="openai/gpt-4o-mini",
            api_key=get_settings().openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0,
            max_tokens=10,
        )
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        text = (response.content or "").strip().upper()
        if "APPROVE" in text:
            return "approve"
        if "REJECT" in text:
            return "reject"
        if "RESPOND" in text:
            return "respond"
        return None
    except Exception as err:
        log.warning("agent_run.hitl_classify_failed", error=str(err))
        return None


async def _build_interrupt_confirmation_reply(
    action_requests: list[dict],
    *,
    prior_messages: list[Any] | None = None,
    user_message: str | None = None,
) -> str:
    if not action_requests:
        return "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"

    tool_name = action_requests[0].get("name", "")
    tool_args = action_requests[0].get("args", {})
    humanize_prompt = (
        "You are a helpful assistant. Explain to the user (in the same language "
        "they used) what you are about to do, in plain conversational language — "
        "no tool names, no code, no technical jargon. Then ask if it's okay to proceed.\n\n"
        f"Tool: {tool_name}\nArguments: {tool_args}\n\nKeep it to 1–2 sentences max."
    )
    try:
        llm = ChatOpenAI(
            model="openai/gpt-4o-mini",
            api_key=get_settings().openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=100,
        )
        messages = [HumanMessage(content=humanize_prompt)]
        if prior_messages is not None and user_message is not None:
            messages = list(prior_messages[-4:]) + [
                HumanMessage(content=user_message),
                HumanMessage(content=humanize_prompt),
            ]
        response = await llm.ainvoke(messages)
        return (response.content or "").strip() or "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"
    except Exception:
        return "Saya memerlukan konfirmasi sebelum melanjutkan. Boleh?"

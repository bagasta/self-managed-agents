"""
result_parser.py — Pure function for parsing LangGraph agent output into DB records.

Extracted from agent_runner.py so the message parsing loop is unit-testable
without a running DB or agent graph.
"""
from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.models.message import Message


class ParsedResult(TypedDict):
    final_reply: str
    steps: list[dict[str, Any]]
    total_tokens_used: int
    db_messages: list[Message]
    has_output: bool  # True if graph produced at least one new message


def ensure_tool_messages_complete(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Inject synthetic ToolMessages for any dangling AIMessage tool calls.

    Defensive layer for edge cases where graph interruption leaves orphaned
    tool_call_ids with no matching ToolMessage.
    """
    answered_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id"):
            answered_ids.add(msg.tool_call_id)

    patched: list[BaseMessage] = []
    for msg in messages:
        patched.append(msg)
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                if tc_id and tc_id not in answered_ids:
                    patched.append(ToolMessage(
                        content=(
                            f"Tool call '{tc.get('name', '?')}' did not produce output — "
                            "execution was interrupted or timed out."
                        ),
                        name=tc.get("name", "unknown"),
                        tool_call_id=tc_id,
                        status="error",
                    ))
                    answered_ids.add(tc_id)
    return patched


def sanitize_input_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Strip dangling tool_calls from AIMessages that have no matching ToolMessage.

    Produces clean history the model can reason about without fake ToolMessages
    that would confuse subsequent reasoning.
    """
    answered_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolMessage) and hasattr(msg, "tool_call_id"):
            answered_ids.add(msg.tool_call_id)

    sanitized: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            dangling = [tc for tc in msg.tool_calls if tc.get("id", "") not in answered_ids]
            if dangling:
                kept_calls = [tc for tc in msg.tool_calls if tc.get("id", "") in answered_ids]
                if msg.content or kept_calls:
                    sanitized.append(AIMessage(
                        content=msg.content or "",
                        tool_calls=kept_calls,
                        id=msg.id,
                    ))
                continue
        sanitized.append(msg)
    return sanitized


_SELF_INSTRUCTION_MARKERS = (
    # Deep agents TodoListMiddleware leaks these internal directives into the final AIMessage.
    # Use specific multi-word phrases to avoid stripping valid agent text.
    "Tuliskan respons singkat",
    "Tulis respons singkat",
)


def _clean_final_reply(text: str) -> str:
    """Strip deep-agent internal artifacts from final reply text.

    The deep agents SDK's TodoListMiddleware sometimes appends self-instructions
    (e.g. 'Tuliskan respons singkat...') or todo-list entries to the last
    AIMessage. Remove these so the user only sees the actual response.
    """
    if not text:
        return text
    for marker in _SELF_INSTRUCTION_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx].rstrip()
    return text


def parse_agent_result(
    result: dict[str, Any],
    input_messages: list[BaseMessage],
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    step_start: int,
    log: Any,
) -> ParsedResult:
    """Parse LangGraph graph output into reply text, step summaries, and Message DB records.

    Pure function — no DB calls, no side effects.
    Caller is responsible for db.add() on each item in db_messages then db.flush().
    """
    all_messages: list[BaseMessage] = result.get("messages", [])

    # Audit tool_call_id → tool_result integrity for observability
    _tc_requested: list[str] = []
    _tc_answered: set[str] = set()
    for _m in all_messages:
        if isinstance(_m, AIMessage) and _m.tool_calls:
            for _tc in _m.tool_calls:
                _tc_id = _tc.get("id", "")
                if _tc_id:
                    _tc_requested.append(f"{_tc.get('name','?')}:{_tc_id[:12]}")
        elif isinstance(_m, ToolMessage) and hasattr(_m, "tool_call_id"):
            _tc_answered.add(_m.tool_call_id)
    _tc_dangling = [t for t in _tc_requested if t.split(":")[-1] not in
                    {_id[:12] for _id in _tc_answered}]
    if _tc_dangling:
        log.warning(
            "agent_run.tool_call_integrity_check",
            requested=_tc_requested,
            answered=len(_tc_answered),
            dangling=_tc_dangling,
        )
    elif _tc_requested:
        log.debug("agent_run.tool_call_integrity_ok", tool_calls=_tc_requested)

    all_messages = ensure_tool_messages_complete(all_messages)
    new_messages = all_messages[len(input_messages):]

    if not new_messages:
        log.warning(
            "agent_run.empty_llm_output",
            input_messages=len(input_messages),
            all_messages=len(all_messages),
            run_id=str(run_id),
        )

    final_reply = ""
    steps: list[dict[str, Any]] = []
    total_tokens_used = 0
    db_messages: list[Message] = []
    step_counter = step_start
    tool_step = 0
    pending_tool_records: list[Message] = []

    for msg in new_messages:
        if isinstance(msg, AIMessage):
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                total_tokens_used += usage.get("total_tokens", 0)

            if msg.content:
                if isinstance(msg.content, str):
                    text = msg.content
                elif isinstance(msg.content, list):
                    text = " ".join(
                        b.get("text", "") for b in msg.content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                else:
                    text = str(msg.content)
                if text:
                    final_reply = _clean_final_reply(text)
                db_messages.append(Message(
                    session_id=session_id,
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
                    session_id=session_id,
                    role="tool",
                    tool_name=tc["name"],
                    tool_args=tc.get("args", {}),
                    step_index=step_counter,
                    run_id=run_id,
                )
                db_messages.append(record)
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

    return ParsedResult(
        final_reply=final_reply,
        steps=steps,
        total_tokens_used=total_tokens_used,
        db_messages=db_messages,
        has_output=bool(new_messages),
    )

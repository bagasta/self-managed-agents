"""Bounded computer progress review and evidence-based run finalization."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.core.engine.agent_middleware import VisualObservationCompactionMiddleware
from app.core.engine.decision_router import decision_prompt

logger = structlog.get_logger(__name__)


def computer_graph_recursion_limit(model_calls: int) -> int:
    # Middleware nodes also consume graph steps. Let the model-call limiter
    # stop execution before LangGraph exhausts its own limit.
    return max(64, model_calls * 10 + 32)


class ComputerProgressReviewMiddleware(VisualObservationCompactionMiddleware):
    name = "ComputerProgressReviewMiddleware"

    def __init__(self, router: Any = None):
        super().__init__()
        self.router = router
        self.reviewed: set[str] = set()

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        update = self.before_model(state, runtime)
        if self.router is None or len(self.reviewed) >= 3:
            return update
        latest = next((m for m in reversed(state.get("messages", [])) if isinstance(m, ToolMessage)), None)
        if latest is None or not str(latest.name or "").startswith("computer_"):
            return update
        key = latest.tool_call_id
        if key in self.reviewed:
            return update
        parts = latest.content if isinstance(latest.content, list) else [{"type": "text", "text": latest.content}]
        evidence: dict[str, Any] = {}
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                try:
                    parsed = json.loads(part.get("text", ""))
                    if isinstance(parsed, dict):
                        evidence.update(parsed)
                except (ValueError, TypeError):
                    pass
        verification = evidence.get("verification") or {}
        stalled = isinstance(verification, dict) and verification.get("unchanged_screen_streak", 0) >= 3
        failed = evidence.get("ok") is False
        if not stalled and not failed:
            return update
        self.reviewed.add(key)
        image_url = next((p.get("image_url", {}).get("url") for p in parts
                          if isinstance(p, dict) and p.get("type") == "image_url"), None)
        decision = await self.router.decide(
            progress=json.dumps({"tool": latest.name, "evidence": evidence}, ensure_ascii=False)[:6000],
            image_url=image_url, review_screen=bool(image_url),
        )
        instruction = (
            "Runtime computer progress check: the last tool failed or the screen has not changed. "
            "Do not repeat the same action or keep taking identical screenshots. "
            "Choose a different action supported by the visible screen, or report the actual blocker. "
            "A human-input lock means wait for the owner and finish independent work; never bypass it."
            + decision_prompt(decision)
        )
        messages = list(update["messages"]) if update else list(state.get("messages", []))
        messages.append(HumanMessage(content=instruction))
        if not update:
            from langchain_core.messages import RemoveMessage
            from langgraph.graph.message import REMOVE_ALL_MESSAGES
            messages.insert(0, RemoveMessage(id=REMOVE_ALL_MESSAGES))
        return {"messages": messages}


async def finalize_computer_run(*, llm: Any, messages: list[Any], request: str,
                                reason: str, callbacks: list[Any]) -> AIMessage:
    """One tool-free call reports verified partial work after a bounded stop."""
    compacted = VisualObservationCompactionMiddleware().before_model({"messages": messages}, None)
    retained = compacted["messages"][1:] if compacted else messages
    evidence = []
    for message in retained[-48:]:
        if isinstance(message, ToolMessage):
            content = message.content
            if isinstance(content, str):
                content = content[:4000]
            evidence.append(HumanMessage(content=(
                [{"type": "text", "text": f"Tool evidence: {message.name}"}, *content]
                if isinstance(content, list) else f"Tool evidence: {message.name}\n{content}"
            )))
        elif isinstance(message, AIMessage) and message.content:
            evidence.append(HumanMessage(content=f"Previous agent notes (unverified): {str(message.content)[:2000]}"))
    prompt = [
        SystemMessage(content=(
            "The computer execution stopped at its bounded limit. No tools are available in this final reporting step. "
            "Reply in the user's language with a useful, truthful partial report from tool evidence only. "
            "State which requested items were verified, which remain unchecked, and the concrete blocker. "
            "Never claim full coverage or completion, invent lesson names, say work continues in the background, "
            "or ask the user to repeat an available request. If no useful evidence exists, state that clearly. "
            "Do not expose internal model or graph details."
        )),
        HumanMessage(content=f"Current request: {request}\nStop reason: {reason}"),
        *evidence,
    ]
    try:
        async with asyncio.timeout(45):
            answer = await llm.ainvoke(prompt, config={"callbacks": callbacks})
        if isinstance(answer, AIMessage) and answer.content and not answer.tool_calls:
            return answer
    except Exception as exc:  # noqa: BLE001 - final fallback must not re-raise a provider failure
        logger.warning("computer_execution.final_report_failed", error=str(exc)[:300])
    return AIMessage(content=(
        "Pemeriksaan belum selesai karena proses mencapai batas eksekusi. "
        "Saya belum memiliki hasil lengkap yang terverifikasi untuk permintaan ini. "
        "Tidak ada pemeriksaan yang masih berjalan di latar."
    ))

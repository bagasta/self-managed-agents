"""LangGraph middleware classes for agent tool-call interception."""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

logger = structlog.get_logger(__name__)

# Control-flow signals that MUST propagate (human-in-the-loop interrupts, parent
# commands, cancellation) — never swallow these as a "tool error".
try:  # pragma: no cover - import path depends on installed langgraph version
    from langgraph.errors import GraphBubbleUp as _GraphBubbleUp

    _CONTROL_FLOW_EXC: tuple[type[BaseException], ...] = (_GraphBubbleUp, asyncio.CancelledError)
except Exception:  # pragma: no cover
    _CONTROL_FLOW_EXC = (asyncio.CancelledError,)


class ToolErrorRecoveryMiddleware(AgentMiddleware):
    """Catch tool exceptions and feed them back to the model instead of crashing the run.

    Without this, a single failing tool call (e.g. an MCP/Google Workspace 400 such
    as "Invalid To header") raises through LangGraph's ToolNode and aborts the whole
    agent run — the agent can never recover or pick a different tool. Here we convert
    the exception into a ToolMessage(status="error") so the LLM gets the failure as
    feedback and can self-correct (retry with valid args, switch tool, or tell the user).
    Control-flow signals (interrupts/cancellation) are re-raised untouched.
    """

    name = "ToolErrorRecoveryMiddleware"

    def _tool_meta(self, request: Any) -> tuple[str, str]:
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = (
            getattr(getattr(request, "tool", None), "name", None)
            or tool_call.get("name")
            or "tool"
        )
        return str(tool_name), str(tool_call.get("id", ""))

    def _error_message(self, request: Any, exc: Exception) -> ToolMessage:
        tool_name, tool_call_id = self._tool_meta(request)
        detail = str(exc).strip()
        if len(detail) > 600:
            detail = detail[:600] + " …"
        logger.warning(
            "agent.tool_error_recovered",
            tool=tool_name,
            error=detail[:300],
        )
        return ToolMessage(
            content=(
                f"[tool_error] Tool '{tool_name}' gagal: {detail}\n"
                "Tool/argumen ini TIDAK berhasil. Jangan ulangi pemanggilan yang sama persis. "
                "Pilih tool yang benar untuk maksud user (mis. untuk kirim file ke WhatsApp pakai "
                "send_whatsapp_document/send_whatsapp_image, BUKAN email), perbaiki argumennya, "
                "atau jelaskan kendalanya ke user dengan jujur. Jangan klaim berhasil kalau belum."
            ),
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        try:
            return handler(request)
        except _CONTROL_FLOW_EXC:
            raise
        except Exception as exc:  # noqa: BLE001 - intentional catch-all for recovery
            return self._error_message(request, exc)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        try:
            return await handler(request)
        except _CONTROL_FLOW_EXC:
            raise
        except Exception as exc:  # noqa: BLE001 - intentional catch-all for recovery
            return self._error_message(request, exc)


class VisualObservationCompactionMiddleware(AgentMiddleware):
    """Keep only the newest computer framebuffer in the model's working state.

    Each image is useful to decide the immediate next click, but resending all
    previous frames turns a short visual task into a huge and slow prompt.
    Tool-call text is retained for audit and reasoning; only old image blocks
    are replaced with a short marker.
    """

    name = "VisualObservationCompactionMiddleware"

    @staticmethod
    def _has_image(message: Any) -> bool:
        return isinstance(message, ToolMessage) and isinstance(message.content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message.content
        )

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = list(state.get("messages", []))
        image_indexes = [index for index, message in enumerate(messages) if self._has_image(message)]
        if len(image_indexes) <= 1:
            return None
        keep_index = image_indexes[-1]
        compacted = []
        for index, message in enumerate(messages):
            if index in image_indexes and index != keep_index:
                compacted.append(message.model_copy(update={
                    "content": "\n".join(
                        str(part.get("text", "")) for part in message.content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ) + "\n[Screenshot komputer sebelumnya diringkas. Gunakan screenshot terbaru untuk menentukan aksi berikutnya.]"
                }))
            else:
                compacted.append(message)
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *compacted]}

class BlockTaskToolMiddleware(AgentMiddleware):
    """Block Deep Agents task delegation for flows that must execute in parent."""

    name = "BlockTaskToolMiddleware"

    def _blocked_message(self, request: Any) -> ToolMessage | None:
        tool_name = getattr(getattr(request, "tool", None), "name", None)
        if tool_name != "task":
            return None
        tool_call = getattr(request, "tool_call", {}) or {}
        return ToolMessage(
            content=(
                "The task tool is disabled for this run. Execute the requested "
                "Google Workspace action directly with the connected Google Workspace tools."
            ),
            tool_call_id=tool_call.get("id", ""),
            name="task",
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        blocked = self._blocked_message(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        blocked = self._blocked_message(request)
        if blocked is not None:
            return blocked
        return await handler(request)

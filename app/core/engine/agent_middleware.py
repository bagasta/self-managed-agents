"""LangGraph middleware classes for agent tool-call interception."""
from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.core.engine.agent_policy import (
    AgentRuntimePolicy,
    should_block_external_service_fallback_tool,
)


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


class ExternalServiceFallbackGuardMiddleware(AgentMiddleware):
    """Reject local/delegated fallback tools for external-service side effects."""

    name = "ExternalServiceFallbackGuardMiddleware"

    def __init__(
        self,
        *,
        policy: AgentRuntimePolicy,
        google_workspace_mcp_available: bool,
        user_message: str,
    ) -> None:
        self._policy = policy
        self._google_workspace_mcp_available = google_workspace_mcp_available
        self._user_message = user_message

    def _blocked_message(self, request: Any) -> ToolMessage | None:
        tool_name = getattr(getattr(request, "tool", None), "name", None) or ""
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_payload = tool_call.get("args", tool_call)
        if not should_block_external_service_fallback_tool(
            policy=self._policy,
            tool_name=tool_name,
            tool_payload=tool_payload,
            user_message=self._user_message,
            google_workspace_mcp_available=self._google_workspace_mcp_available,
        ):
            return None
        return ToolMessage(
            content=(
                "This is a Google Workspace external-service action. Do not use "
                "sandbox, filesystem, or task delegation as a fallback. Call the "
                "relevant Google Workspace tool directly, or return the Google "
                "auth/unavailable blocker if the integration cannot run."
            ),
            tool_call_id=tool_call.get("id", ""),
            name=tool_name,
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

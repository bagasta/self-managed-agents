"""Runtime policy helpers for agent orchestration.

The platform is SaaS multi-tenant, so policy is not only about model behavior.
It also decides which runtime capabilities are safe to expose for each class of
agent before the model gets a chance to choose tools semantically.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from app.core.engine.google_mcp_support import (
    _is_google_mcp_intent,
    is_google_workspace_mcp_configured,
)

AgentPolicyClass = Literal["builder", "operational"]


@dataclass(frozen=True)
class AgentRuntimePolicy:
    policy_class: AgentPolicyClass
    is_builder: bool


def build_agent_runtime_policy(agent_model: Any, tools_config: dict[str, Any]) -> AgentRuntimePolicy:
    capabilities = getattr(agent_model, "capabilities", []) or []
    is_builder = "builder" in capabilities or bool(
        isinstance(tools_config, dict) and tools_config.get("builder")
    )
    return AgentRuntimePolicy(
        policy_class="builder" if is_builder else "operational",
        is_builder=is_builder,
    )


def should_use_google_workspace_parent_only(
    *,
    policy: AgentRuntimePolicy,
    user_message: str,
    tools_config: dict[str, Any],
) -> bool:
    """Return True only if the legacy hard branch is explicitly configured.

    The semantic MCP-first path should expose tools and let the model choose
    MCP. This legacy guard remains as an emergency compatibility switch, not as
    the default routing mechanism.
    """
    if policy.policy_class == "builder" or not isinstance(tools_config, dict):
        return False
    mcp_cfg = tools_config.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        return False
    hard_parent_only = bool(mcp_cfg.get("google_workspace_parent_only"))
    return (
        hard_parent_only
        and _is_google_mcp_intent(user_message)
        and is_google_workspace_mcp_configured(tools_config)
    )


_GOOGLE_WORKSPACE_EXTERNAL_MARKERS = (
    "google workspace",
    "google slide",
    "google slides",
    "google sheet",
    "google sheets",
    "google docs",
    "google doc",
    "google drive",
    "google form",
    "google forms",
    "gmail",
    "calendar",
    "google calendar",
    "kalender google",
    "dokumen google",
    "form google",
    "spreadsheet",
    "presentation",
    "presentasi google",
)


def text_mentions_google_workspace_external_service(value: Any) -> bool:
    """Detect tool-call payloads that are trying to perform Google side effects.

    This is intentionally stricter than `_is_google_mcp_intent`: a bare word
    like "slide" is too broad when inspecting sandbox/code task payloads.
    """
    if value is None:
        return False
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    lowered = text.lower()
    return any(marker in lowered for marker in _GOOGLE_WORKSPACE_EXTERNAL_MARKERS)


def should_block_external_service_fallback_tool(
    *,
    policy: AgentRuntimePolicy,
    tool_name: str,
    tool_payload: Any,
    user_message: str = "",
    google_workspace_mcp_available: bool,
) -> bool:
    """Block task/sandbox fallback attempts for Google Workspace work.

    The model should choose MCP tools semantically. This guard exists only when
    it chooses a local/delegation tool for a Google Workspace side effect.
    """
    if policy.policy_class != "operational" or not google_workspace_mcp_available:
        return False
    combined_payload = {
        "user_message": user_message,
        "tool_payload": tool_payload,
    }
    if not text_mentions_google_workspace_external_service(combined_payload):
        return False
    name = (tool_name or "").lower()
    fallback_tools = {
        "task",
        "execute",
        "write_file",
        "edit_file",
        "read_file",
        "sandbox_write_binary_file",
    }
    return name in fallback_tools

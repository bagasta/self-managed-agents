"""Runtime policy helpers for agent orchestration.

The platform is SaaS multi-tenant, so policy is not only about model behavior.
It also decides which runtime capabilities are safe to expose for each class of
agent before the model gets a chance to choose tools semantically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.core.engine.google_mcp_support import is_google_workspace_mcp_configured

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
    """Return True only if the hard branch is explicitly configured.

    Runtime policy must be based on configured capabilities, never words in a
    user message. ``user_message`` remains in the signature for compatibility.
    """
    del user_message
    if policy.policy_class == "builder" or not isinstance(tools_config, dict):
        return False
    mcp_cfg = tools_config.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        return False
    return bool(mcp_cfg.get("google_workspace_parent_only")) and (
        is_google_workspace_mcp_configured(tools_config)
    )

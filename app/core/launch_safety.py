"""Temporary launch safety switches for high-risk runtime features."""
from __future__ import annotations

from typing import Any

from app.config import get_settings


SANDBOX_DISABLED_NOTICE = (
    "Sandbox, deploy, tool creator, dan subagent sementara dinonaktifkan untuk launch. "
    "Agent tetap bisa chat, memory, WhatsApp, Google/MCP, scheduler, dan eskalasi."
)


def sandbox_subagents_enabled() -> bool:
    return bool(get_settings().sandbox_subagents_enabled)


def disable_sandbox_subagent_tools_config(tools_config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a copy with sandbox/subagent-dependent features disabled."""
    tc = dict(tools_config or {})
    disabled: list[str] = []

    for key in ("sandbox", "deploy", "tool_creator"):
        if tc.get(key):
            disabled.append(key)
        tc[key] = False

    subagents = tc.get("subagents")
    subagents_was_enabled = bool(
        subagents.get("enabled") if isinstance(subagents, dict) else subagents
    )
    if subagents_was_enabled:
        disabled.append("subagents")
    if isinstance(subagents, dict):
        sub = dict(subagents)
        sub["enabled"] = False
        sub["agent_ids"] = []
        tc["subagents"] = sub
    else:
        tc["subagents"] = {"enabled": False, "agent_ids": []}

    return tc, disabled

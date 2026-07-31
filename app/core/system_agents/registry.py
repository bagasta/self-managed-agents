"""Generic registry for optional system-agent plugins.

The generic API and agent runtime must not depend on any particular system
agent's prompts, tools, or asset paths.  Plugins identify themselves through a
stable ``system_plugin`` value in the agent's tools configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import get_settings


ARTHUR_LEGACY_PLUGIN = "arthur_legacy"
ARTHUR_V2_PLUGIN = "arthur_v2"


@dataclass(frozen=True)
class SystemAgentStatus:
    key: str
    enabled: bool
    disabled_reply: str


def system_agent_key(tools_config: dict[str, Any] | None) -> str | None:
    config = tools_config if isinstance(tools_config, dict) else {}
    explicit = str(config.get("system_plugin") or "").strip()
    if explicit:
        return explicit
    # Compatibility for the current seeded Arthur record.  The next seed writes
    # the explicit key, while existing production data remains routable.
    if bool(config.get("builder")) and isinstance(config.get("arthur_runtime"), dict):
        return ARTHUR_LEGACY_PLUGIN
    return None


def get_system_agent_status(tools_config: dict[str, Any] | None) -> SystemAgentStatus | None:
    key = system_agent_key(tools_config)
    if key == ARTHUR_V2_PLUGIN:
        return SystemAgentStatus(
            key=key,
            enabled=True,
            disabled_reply="Arthur baru sedang tidak tersedia.",
        )
    if key != ARTHUR_LEGACY_PLUGIN:
        return None
    return SystemAgentStatus(
        key=key,
        enabled=bool(get_settings().arthur_legacy_enabled),
        disabled_reply=(
            "Arthur versi sebelumnya sedang dinonaktifkan. "
            "Layanan pembuatan dan pengelolaan agent tetap tersedia melalui dashboard atau API."
        ),
    )


def build_system_agent_tools(*, tools_config: dict[str, Any] | None, **context: Any) -> list:
    """Resolve a system-agent tool plugin without coupling the engine to it."""
    key = system_agent_key(tools_config)
    if key == ARTHUR_V2_PLUGIN:
        from arthur_v2 import build_arthur_v2_tools

        # Arthur V2 owns a narrow control-plane contract.  The session ID is
        # included solely to resolve the current inbound document workspace for
        # its RAG ingestion tool; channel/device metadata remains excluded.
        v2_context = {
            field: context[field]
            for field in ("db_factory", "owner_phone", "self_agent_id", "default_target", "session_id")
            if field in context
        }
        return build_arthur_v2_tools(**v2_context)
    if key == ARTHUR_LEGACY_PLUGIN:
        from app.core.engine.tool_builder import build_builder_tools

        return build_builder_tools(**context)
    return []

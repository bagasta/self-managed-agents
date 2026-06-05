"""Tool and sub-agent setup for agent runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.core.domain.custom_tool_service import list_custom_tools
from app.core.engine.agent_policy import (
    build_agent_runtime_policy,
    should_use_google_workspace_parent_only,
)
from app.core.engine.subagent_builder import build_subagents
from app.core.engine.tool_builder import (
    _is_enabled,
    build_builder_tools,
    build_deployment_tools,
    build_http_tools,
    build_loaded_custom_tools,
    build_memory_tools,
    build_heartbeat_tools,
    build_sandbox_binary_tool,
    build_skill_tools,
    build_tavily_tools,
    build_tool_creator_tools,
    build_wa_agent_manager_tools,
    build_wa_notify_tool,
    build_whatsapp_media_tools,
)
from app.core.engine.sop_runtime_gate import filter_tools_by_sop
from app.core.infra.sandbox import DockerSandbox
from app.core.utils.phone_utils import normalize_phone
from app.models.agent import Agent as AgentModel
from app.models.session import Session


@dataclass
class AgentToolSetup:
    tools: list
    active_groups: list[str]
    saved_custom_tools: list
    sandbox: DockerSandbox | None
    subagent_list: list
    sub_sandboxes: list[DockerSandbox]
    memory_scope: str | None


def is_operator_turn(user_message: str) -> bool:
    return user_message.startswith("[OPERATOR] ") or user_message.startswith("<OPERATOR>")


def _is_probable_lid(value: str | None) -> bool:
    normalized = normalize_phone(value or "")
    return bool(normalized and normalized.isdigit() and len(normalized) > 15)


def _resolve_builder_owner_phone(session: Session) -> str | None:
    """Prefer a real phone number for Arthur ownership/subscription tools."""
    raw_cfg = getattr(session, "channel_config", None)
    channel_cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
    candidates = (
        channel_cfg.get("phone_number"),
        getattr(session, "external_user_id", None),
        channel_cfg.get("user_phone"),
    )
    for candidate in candidates:
        normalized = normalize_phone(str(candidate or ""))
        if normalized and not _is_probable_lid(normalized):
            return normalized
    fallback = getattr(session, "external_user_id", None)
    return normalize_phone(str(fallback or "")) or fallback


async def build_agent_tool_setup(
    *,
    agent_model: AgentModel,
    session: Session,
    tools_config: dict[str, Any],
    raw_tools_config: Any,
    db: AsyncSession,
    log: Any,
    escalation_user_jid: str | None,
    sender_name: str | None,
    user_message: str,
    operating_manual: dict[str, Any] | None = None,
) -> AgentToolSetup:
    settings = get_settings()
    agent_id = session.agent_id
    tools: list = []
    active_groups: list[str] = []
    saved_custom_tools: list = []

    policy = build_agent_runtime_policy(agent_model, tools_config)
    builder_agent = policy.is_builder
    operator_turn = is_operator_turn(user_message)
    google_mcp_parent_only = should_use_google_workspace_parent_only(
        policy=policy,
        user_message=user_message,
        tools_config=tools_config,
    )
    log.info("agent_run.policy_selected", policy_class=policy.policy_class)
    deploy_enabled = _is_enabled(tools_config, "deploy", default=False)
    sandbox: DockerSandbox | None = None
    sandbox_requested = (
        (_is_enabled(tools_config, "sandbox", default=False) or deploy_enabled)
        and not operator_turn
        and not builder_agent
    )
    if builder_agent and (_is_enabled(tools_config, "sandbox", default=False) or deploy_enabled):
        log.info(
            "agent_run.builder_sandbox_skipped",
            reason="builder_must_use_internal_tools_not_filesystem",
            deploy_enabled=deploy_enabled,
        )
    if sandbox_requested and google_mcp_parent_only:
        log.info(
            "agent_run.google_mcp_parent_sandbox_skipped",
            reason="google_workspace_mcp_must_not_fallback_to_sandbox",
            deploy_enabled=deploy_enabled,
        )
        active_groups.append("google_mcp_parent_only")
    elif sandbox_requested:
        sandbox = DockerSandbox(session.id)

    if sandbox is not None:
        tools.extend(build_sandbox_binary_tool(sandbox))
        active_groups.append("sandbox")
        if deploy_enabled:
            tools.extend(build_deployment_tools(sandbox))
            active_groups.append("deploy")

    memory_scope = getattr(session, "external_user_id", None)
    if _is_enabled(tools_config, "memory", default=True):
        tools.extend(build_memory_tools(agent_id, AsyncSessionLocal, scope=memory_scope))
        tools.extend(build_heartbeat_tools(agent_id, session.id, AsyncSessionLocal, scope=memory_scope))
        active_groups.append("memory")

    if (not operator_turn) and _is_enabled(tools_config, "skills", default=True):
        tools.extend(build_skill_tools(agent_id, AsyncSessionLocal))
        active_groups.append("skills")

    if (not operator_turn) and (not builder_agent) and _is_enabled(tools_config, "tool_creator", default=False):
        if google_mcp_parent_only:
            log.info(
                "agent_run.tool_creator_skipped",
                reason="google_workspace_mcp_parent_only",
            )
        elif sandbox is None:
            log.warning("agent_run.tool_creator_requires_sandbox")
        else:
            tools.extend(build_tool_creator_tools(agent_id, AsyncSessionLocal, sandbox))
            saved_custom_tools = await list_custom_tools(agent_id, db)
            tools.extend(build_loaded_custom_tools(saved_custom_tools, sandbox))
            active_groups.append("tool_creator")

    if (not operator_turn) and _is_enabled(tools_config, "scheduler", default=False):
        from app.core.tools.scheduler_tool import build_scheduler_tools

        tools.extend(build_scheduler_tools(session.id, agent_id, AsyncSessionLocal))
        active_groups.append("scheduler")

    if _is_enabled(tools_config, "escalation", default=True):
        from app.core.tools.escalation_tool import build_escalation_tools

        raw_cfg = session.channel_config
        channel_cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
        user_jid = (
            escalation_user_jid
            or channel_cfg.get("user_phone")
            or getattr(session, "external_user_id", None)
        )
        tools.extend(
            build_escalation_tools(
                session.id,
                agent_id,
                AsyncSessionLocal,
                user_jid=user_jid,
                sender_name=sender_name,
            )
        )
        active_groups.append("escalation")

    if operator_turn:
        from app.core.tools.operator_tools import build_operator_tools

        tools.extend(build_operator_tools(agent_id=agent_id, db_factory=AsyncSessionLocal))
        active_groups.append("operator")

    if (not operator_turn) and _is_enabled(tools_config, "http", default=False):
        tools.extend(build_http_tools(tools_config))
        active_groups.append("http")

    if (not operator_turn) and _is_enabled(tools_config, "tavily", default=True) and settings.tavily_api_key:
        tools.extend(build_tavily_tools(tools_config))
        active_groups.append("tavily")

    if (not operator_turn) and getattr(session, "channel_type", None) == "whatsapp":
        tools.extend(build_wa_notify_tool(session))
        active_groups.append("wa_notify")
        if _is_enabled(tools_config, "whatsapp_media", default=True):
            tools.extend(build_whatsapp_media_tools(session, sandbox))
            active_groups.append("whatsapp_media")
        if _is_enabled(tools_config, "wa_agent_manager", default=False):
            tools.extend(build_wa_agent_manager_tools(session, db_factory=AsyncSessionLocal))
            active_groups.append("wa_agent_manager")

    if (not operator_turn) and builder_agent:
        channel_cfg = getattr(session, "channel_config", None)
        channel_cfg = channel_cfg if isinstance(channel_cfg, dict) else {}
        tools.extend(build_builder_tools(
            db_factory=AsyncSessionLocal,
            owner_phone=_resolve_builder_owner_phone(session),
            self_agent_id=str(agent_id),
            device_id=channel_cfg.get("device_id", "") or "",
            default_target=channel_cfg.get("user_phone", "") or "",
            session_id=str(session.id),
        ))
        active_groups.append("builder")

    subagent_list: list = []
    sub_sandboxes: list[DockerSandbox] = []
    if operator_turn and _is_enabled(tools_config, "subagents", default=False):
        log.info("agent_run.operator_subagents_skipped", reason="operator_turn_must_not_run_business_workflow")
    elif builder_agent and _is_enabled(tools_config, "subagents", default=False):
        log.info(
            "agent_run.builder_subagents_skipped",
            reason="builder_must_update_platform_records_directly",
        )
    elif _is_enabled(tools_config, "subagents", default=False) and google_mcp_parent_only:
        log.info(
            "agent_run.google_mcp_subagents_skipped",
            reason="google_workspace_mcp_parent_only",
        )
    elif _is_enabled(tools_config, "subagents", default=False):
        sub_ids: list[str] = tools_config.get("subagents", {}).get("agent_ids", [])
        sub_channel: dict = session.channel_config if isinstance(session.channel_config, dict) else {}
        subagent_list, sub_sandboxes = await build_subagents(
            sub_ids,
            session.id,
            db,
            log,
            wa_device_id=sub_channel.get("device_id", ""),
            wa_target=sub_channel.get("user_phone", ""),
            user_message=user_message,
            expose_wa_media_tools_override=False,
        )
        if subagent_list:
            active_groups.append(f"subagents({len(subagent_list)})")
            log.info("agent_run.subagents_ready", names=[s.get("name", "?") for s in subagent_list])
            deploy_tool_names = {
                "deploy_app", "stop_deployment",
                "get_deployment_status", "get_deployment_logs",
            }
            before = len(tools)
            tools = [tool for tool in tools if getattr(tool, "name", None) not in deploy_tool_names]
            if len(tools) != before:
                log.info(
                    "agent_run.parent_deploy_tools_stripped",
                    removed=before - len(tools),
                    reason="subagents_active",
                )
                if "deploy" in active_groups:
                    active_groups.remove("deploy")

    # SOP runtime gate: remove final-action tools when SOP is not mature.
    # Builder/system agents are exempt.
    _caps = list(getattr(agent_model, "capabilities", None) or [])
    _before_sop = len(tools)
    tools = filter_tools_by_sop(tools, sop=operating_manual, caps=_caps)
    _removed_sop = _before_sop - len(tools)
    if _removed_sop:
        _maturity = (operating_manual or {}).get("maturity", "missing") if isinstance(operating_manual, dict) else "missing"
        log.info(
            "agent_tool_setup.sop_locked_tools_removed",
            removed=_removed_sop,
            maturity=_maturity,
            agent_id=str(agent_id),
        )

    return AgentToolSetup(
        tools=tools,
        active_groups=active_groups,
        saved_custom_tools=saved_custom_tools,
        sandbox=sandbox,
        subagent_list=subagent_list,
        sub_sandboxes=sub_sandboxes,
        memory_scope=memory_scope,
    )

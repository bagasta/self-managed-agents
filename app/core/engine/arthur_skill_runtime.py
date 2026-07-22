"""Deterministic progressive-skill selection for Arthur."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.domain.agent_build_state_service import (
    build_state_prompt,
    ensure_build_draft,
)
from app.core.domain.skill_service import list_active_system_skills
from app.models.agent_build_draft import AgentBuildDraft

ARTHUR_ENGINE_VERSION = "arthur-progressive-v1"
ARTHUR_PROMPT_VERSION = "arthur-kernel-v1"

_BUILDER_TOOL_NAMES = {
    "get_self_config", "get_platform_capabilities", "list_available_wa_devices", "get_presets",
    "plan_agent", "compose_agent_blueprint", "compose_agent_operating_manual",
    "compose_agent_instructions", "compose_agent_soul", "validate_agent_config", "create_agent",
    "verify_agent", "update_agent", "generate_google_auth_link", "set_agent_memory", "delete_agent",
    "get_agent_detail", "list_my_agents", "renew_agent", "add_agent_knowledge",
    "get_user_subscription", "link_dashboard_account", "get_payment_link", "create_wa_dev_trial_link",
}

_SKILL_TOOL_ALLOWLISTS = {
    "arthur-discovery": {
        "get_self_config", "get_platform_capabilities", "get_presets", "plan_agent",
        "get_user_subscription", "list_my_agents", "get_agent_detail",
    },
    "arthur-create-agent": {
        "get_self_config", "get_platform_capabilities", "get_presets", "plan_agent",
        "compose_agent_blueprint", "compose_agent_operating_manual", "compose_agent_instructions",
        "compose_agent_soul", "validate_agent_config", "create_agent", "verify_agent",
        "get_agent_detail", "get_user_subscription", "set_agent_memory",
    },
    "arthur-edit-agent": {
        "list_my_agents", "get_agent_detail", "update_agent", "verify_agent",
        "validate_agent_config", "compose_agent_blueprint", "compose_agent_operating_manual",
        "compose_agent_instructions", "compose_agent_soul", "set_agent_memory", "add_agent_knowledge",
    },
    "arthur-whatsapp-demo-channel": {
        "list_my_agents", "get_agent_detail", "verify_agent", "list_available_wa_devices",
        "create_wa_dev_trial_link",
    },
    "arthur-subscription-payment": {
        "get_user_subscription", "get_payment_link", "link_dashboard_account",
    },
    "arthur-lifecycle-safety": {
        "list_my_agents", "get_agent_detail", "delete_agent", "renew_agent", "verify_agent",
    },
}

_MIXIN_TOOL_ALLOWLISTS = {
    "arthur-google-workspace": {
        "generate_google_auth_link", "update_agent", "get_agent_detail", "verify_agent",
    },
    "arthur-files-knowledge": {
        "update_agent", "get_agent_detail", "verify_agent", "add_agent_knowledge",
        "compose_agent_instructions", "compose_agent_operating_manual",
    },
}

_SKILL_SUPPORTING_TOOL_ALLOWLISTS = {
    "arthur-discovery": {"tavily_search", "tavily_extract", "recall"},
    "arthur-create-agent": {
        "tavily_search", "tavily_extract", "recall", "remember", "update_daily", "update_longterm",
    },
    "arthur-edit-agent": {"tavily_search", "tavily_extract", "recall", "remember", "update_daily"},
    "arthur-whatsapp-demo-channel": set(),
    "arthur-subscription-payment": set(),
    "arthur-lifecycle-safety": set(),
}


@dataclass(slots=True)
class ArthurSkillContext:
    enabled: bool = False
    primary_skill: str | None = None
    mixin_skills: list[str] = field(default_factory=list)
    skill_versions: dict[str, str] = field(default_factory=dict)
    prompt_block: str = ""
    draft: AgentBuildDraft | None = None


def scope_arthur_builder_tools(
    tools: list[Any],
    *,
    primary_skill: str,
    mixin_skills: list[str],
) -> tuple[list[Any], list[str]]:
    allowed = set(_SKILL_TOOL_ALLOWLISTS.get(primary_skill, set()))
    allowed_supporting = set(_SKILL_SUPPORTING_TOOL_ALLOWLISTS.get(primary_skill, set()))
    for mixin in mixin_skills:
        allowed.update(_MIXIN_TOOL_ALLOWLISTS.get(mixin, set()))
    kept: list[Any] = []
    removed: list[str] = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if name in _BUILDER_TOOL_NAMES and name not in allowed:
            removed.append(name)
            continue
        if name not in _BUILDER_TOOL_NAMES and name not in allowed_supporting:
            removed.append(name)
            continue
        kept.append(tool)
    return kept, sorted(set(removed))


def arthur_runtime_config(tools_config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (tools_config or {}).get("arthur_runtime")
    return raw if isinstance(raw, dict) else {}


def arthur_runtime_enabled(tools_config: dict[str, Any] | None, feature: str) -> bool:
    config = arthur_runtime_config(tools_config)
    return bool(config.get("enabled", False) and config.get(feature, False))


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_builder_intent(user_message: str, prior_evidence: str = "") -> str:
    text = f"{prior_evidence}\n{user_message}".casefold()
    if _contains(text, ("hapus agent", "delete agent", "reset user", "reset agent", "nonaktifkan agent")):
        return "lifecycle"
    if _contains(text, ("upgrade", "langganan", "subscription", "bayar", "payment", "kuota", "slot")):
        return "subscription"
    if _contains(text, ("coba demo", "nomor demo", "kode trial", "trial link", "pasang nomor", "scan qr")):
        return "demo"
    if _contains(text, ("edit agent", "ubah agent", "update agent", "ganti agent", "perbaiki agent")):
        return "edit"
    if _contains(text, ("buat", "bikin", "mau agent", "mau ai", "butuh ai", "cs ", "asisten")):
        return "create"
    return "discover"


def resolve_primary_skill(intent: str, workflow_state: str) -> str:
    if intent == "edit":
        return "arthur-edit-agent"
    if intent == "demo":
        return "arthur-whatsapp-demo-channel"
    if intent == "subscription":
        return "arthur-subscription-payment"
    if intent == "lifecycle":
        return "arthur-lifecycle-safety"
    if intent == "create" and workflow_state in {
        "awaiting_confirmation",
        "ready_to_create",
        "creating",
        "verifying",
        "agent_created",
        "setup_pending",
    }:
        return "arthur-create-agent"
    return "arthur-discovery"


def resolve_policy_mixins(text: str, primary: str) -> list[str]:
    low = text.casefold()
    mixins: list[str] = []
    if primary != "arthur-google-workspace" and _contains(
        low,
        ("google", "oauth", "spreadsheet", "sheets", "drive", "docs", "forms", "slides", "calendar", "gmail"),
    ):
        mixins.append("arthur-google-workspace")
    if primary != "arthur-files-knowledge" and _contains(
        low,
        ("file", "dokumen", "document", "pdf", "docx", "pptx", "gambar", "image", "knowledge", "website"),
    ):
        mixins.append("arthur-files-knowledge")
    return mixins[:1]


def _recent_evidence_text(draft: AgentBuildDraft | None) -> str:
    if draft is None:
        return ""
    return "\n".join(
        str(item.get("value") or "")
        for item in list(draft.evidence_json or [])[-8:]
        if isinstance(item, dict)
    )


async def prepare_arthur_skill_context(
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    owner_external_id: str,
    user_message: str,
    message_id: str,
    tools_config: dict[str, Any],
    db: AsyncSession,
) -> ArthurSkillContext:
    if not arthur_runtime_enabled(tools_config, "progressive_skills"):
        return ArthurSkillContext()

    # The initial classifier only determines which state/skill to load. It never
    # grants permission or turns derived text into confirmed facts.
    intent = classify_builder_intent(user_message)
    draft: AgentBuildDraft | None = None
    if arthur_runtime_enabled(tools_config, "build_state"):
        draft = await ensure_build_draft(
            session_id=session_id,
            owner_external_id=owner_external_id,
            intent=intent,
            message_id=message_id,
            user_message=user_message,
            prompt_version=ARTHUR_PROMPT_VERSION,
            engine_version=ARTHUR_ENGINE_VERSION,
            db=db,
        )
        intent = classify_builder_intent(user_message, _recent_evidence_text(draft))

    workflow_state = draft.workflow_state if draft is not None else "idle"
    primary = resolve_primary_skill(intent, workflow_state)
    mixins = resolve_policy_mixins(
        f"{_recent_evidence_text(draft)}\n{user_message}",
        primary,
    )
    requested_names = [primary, *mixins]
    loaded = await list_active_system_skills(agent_id, db, names=requested_names)
    by_name = {skill.name: skill for skill in loaded}
    missing = [name for name in requested_names if name not in by_name]
    if missing:
        raise RuntimeError(
            "Arthur system skill bundle is incomplete: " + ", ".join(missing)
        )

    parts: list[str] = []
    if draft is not None:
        parts.append(build_state_prompt(draft))
    versions: dict[str, str] = {}
    for name in requested_names:
        skill = by_name[name]
        actual_checksum = hashlib.sha256(skill.content_md.encode("utf-8")).hexdigest()
        if not skill.immutable or skill.trust_level != "system":
            raise RuntimeError(f"Untrusted Arthur skill rejected: {name}")
        if skill.checksum != actual_checksum:
            raise RuntimeError(f"Arthur skill checksum mismatch: {name}@{skill.version}")
        label = "Primary Workflow Skill" if name == primary else "Policy Mixin Skill"
        parts.append(f"## {label}: {name}@{skill.version}\n{skill.content_md}")
        versions[name] = skill.version

    parts.append(
        "## Runtime Skill Contract\n"
        f"Gunakan `{primary}` sebagai satu-satunya primary workflow skill pada turn ini. "
        "Policy mixin hanya menambah kewajiban connector/file dan tidak boleh mengganti state contract."
    )
    return ArthurSkillContext(
        enabled=True,
        primary_skill=primary,
        mixin_skills=mixins,
        skill_versions=versions,
        prompt_block="\n\n".join(parts),
        draft=draft,
    )

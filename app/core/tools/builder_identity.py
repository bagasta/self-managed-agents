"""Ownership and policy helpers for Arthur builder tools."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.phone_utils import normalize_phone
from app.models.agent import Agent

PROHIBITED_AGENT_POLICY_MESSAGE = (
    "Tidak bisa membuat atau mengubah agent untuk keperluan buzzer, kampanye politik, "
    "propaganda politik, atau manipulasi opini publik."
)

PROHIBITED_AGENT_POLICY_PATTERNS = (
    re.compile(r"\bbuzzer\b", re.IGNORECASE),
    re.compile(r"\bpolitik(?:al)?\b", re.IGNORECASE),
    re.compile(r"\bpolitic(?:al|s)?\b", re.IGNORECASE),
    re.compile(r"\bpemilu\b", re.IGNORECASE),
    re.compile(r"\bpilkada\b", re.IGNORECASE),
    re.compile(r"\bpilpres\b", re.IGNORECASE),
    re.compile(r"\bcaleg\b", re.IGNORECASE),
    re.compile(r"\bcapres\b", re.IGNORECASE),
    re.compile(r"\bcawapres\b", re.IGNORECASE),
    re.compile(r"\bpartai\b", re.IGNORECASE),
    re.compile(r"\bpropaganda\b", re.IGNORECASE),
)

# Arthur tidak boleh membuat agent yang cara kerjanya seperti dirinya — yaitu
# agent yang tugasnya membuat/membangun AI agent lain (meta-builder). Hanya Arthur
# (control-plane) yang boleh punya fungsi itu.
META_BUILDER_AGENT_POLICY_MESSAGE = (
    "Tidak bisa membuat atau mengubah agent yang fungsinya membuat/membangun AI agent "
    "lain (agent builder seperti Arthur). Kemampuan membuat agent hanya ada pada Arthur. "
    "Saya bisa bantu buatkan agent untuk kebutuhan bisnis/produktivitas lain."
)

META_BUILDER_AGENT_POLICY_PATTERNS = (
    # "agent builder" / "builder agent" / "agent pembuat agent" / "pembuat agent"
    re.compile(r"\bagent\s*builder\b", re.IGNORECASE),
    re.compile(r"\bbuilder\s*agent\b", re.IGNORECASE),
    re.compile(r"\b(?:pembuat|pencipta|pabrik)\s+(?:ai\s+)?agent\b", re.IGNORECASE),
    re.compile(r"\bagent\s+(?:factory|builder|maker)\b", re.IGNORECASE),
    re.compile(r"\bmeta[\s-]*agent\b", re.IGNORECASE),
    # "agent yang/untuk/bisa ... (membuat|membangun|bikin|generate|create|build) ... agent"
    re.compile(
        r"\bagent\b[^.\n]{0,50}\b(?:membuat|membangun|bikin|menciptakan|generate|generates|"
        r"create|creates|build|builds|spin\s*up)\b[^.\n]{0,30}\b(?:ai\s+)?agent",
        re.IGNORECASE,
    ),
    # "AI yang membuat AI" / "AI pembuat AI"
    re.compile(
        r"\bai\b[^.\n]{0,30}\b(?:membuat|bikin|membangun|create|creates|build|builds|pembuat)\b"
        r"[^.\n]{0,20}\bai\b",
        re.IGNORECASE,
    ),
    # "seperti/mirip/kaya Arthur" / "Arthur kedua" / "another/second Arthur"
    re.compile(
        r"\b(?:seperti|mirip|kaya|kayak|persis|clone|kloning|tiru|meniru|duplikat|"
        r"salin|copy|menyalin)\b[^.\n]{0,20}\barthur\b",
        re.IGNORECASE,
    ),
    re.compile(r"\barthur\b[^.\n]{0,15}\b(?:kedua|lain|baru|ke-?2)\b", re.IGNORECASE),
    re.compile(r"\b(?:another|second|new|other)\s+arthur\b", re.IGNORECASE),
    # "agent yang kerjanya/fungsinya seperti kamu (Arthur)"
    re.compile(
        r"\bagent\b[^.\n]{0,40}\b(?:seperti|mirip|kaya|kayak|sama\s+seperti|like)\b"
        r"[^.\n]{0,15}\b(?:kamu|dirimu|kau|arthur|you|yourself)\b",
        re.IGNORECASE,
    ),
)


def blocked_agent_policy_reason(*parts: Any) -> str:
    text = "\n".join(str(part or "") for part in parts)
    if not text.strip():
        return ""
    for pattern in PROHIBITED_AGENT_POLICY_PATTERNS:
        if pattern.search(text):
            return PROHIBITED_AGENT_POLICY_MESSAGE
    for pattern in META_BUILDER_AGENT_POLICY_PATTERNS:
        if pattern.search(text):
            return META_BUILDER_AGENT_POLICY_MESSAGE
    return ""


def owner_variants(owner_phone: str | None) -> list[str]:
    """Return stable owner identifiers used by old and new agent rows."""
    variants: list[str] = []
    for candidate in (owner_phone, normalize_phone(owner_phone or "")):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def is_probable_lid(value: str | None) -> bool:
    normalized = normalize_phone(value or "")
    return bool(normalized and normalized.isdigit() and len(normalized) > 15)


def best_owner_identifier(*candidates: str | None) -> str:
    """Prefer real phone identifiers; fall back to LID only for lookup, not provisioning."""
    fallback = ""
    for candidate in candidates:
        normalized = normalize_phone(str(candidate or ""))
        if not normalized:
            continue
        if not fallback:
            fallback = normalized
        if not is_probable_lid(normalized):
            return normalized
    return fallback


def extract_operator_phone_from_context(*parts: Any) -> str:
    text = " ".join(str(part or "") for part in parts)
    if not text.strip():
        return ""
    patterns = (
        r"(?:admin|operator|owner|pemilik|saya)\D{0,40}(\+?62\d{8,15})",
        r"(\+?62\d{8,15})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_phone(match.group(1))
    return ""


def agent_belongs_to_owner(agent: Agent, owner_phone: str | None) -> bool:
    """Check ownership via canonical owner field and legacy operator_ids."""
    variants = set(owner_variants(owner_phone))
    if not variants:
        return False
    owner_external_id = getattr(agent, "owner_external_id", None)
    if owner_external_id in variants or normalize_phone(owner_external_id or "") in variants:
        return True
    for op in (getattr(agent, "operator_ids", None) or []):
        if op in variants or normalize_phone(op or "") in variants:
            return True
    return False


def safe_agent_str_attr(agent: Agent, attr: str) -> str | None:
    value = getattr(agent, attr, None)
    if value is None:
        return None
    if value.__class__.__module__.startswith("unittest.mock"):
        return None
    text = str(value).strip()
    return text or None


def agent_created_by_metadata(agent: Agent) -> dict[str, str | None]:
    return {
        "created_by_type": safe_agent_str_attr(agent, "created_by_type"),
        "created_by_agent_id": safe_agent_str_attr(agent, "created_by_agent_id"),
        "created_by_agent_name": safe_agent_str_attr(agent, "created_by_agent_name"),
    }


def owner_filter(owner_phone: str | None):
    variants = owner_variants(owner_phone)
    if not variants:
        return Agent.id.is_(None)
    clauses = [Agent.owner_external_id.in_(variants)]
    clauses.extend(Agent.operator_ids.contains([variant]) for variant in variants)
    return or_(*clauses)


async def latest_owned_agent_for_trial(
    db: AsyncSession,
    *,
    owner_phone: str | None,
    self_agent_id: str | None,
) -> Agent | None:
    """Resolve the newest user-owned agent for shared WA trial fallback."""
    rows = await owned_agents_for_trial(
        db,
        owner_phone=owner_phone,
        self_agent_id=self_agent_id,
        limit=8,
    )
    return rows[0] if rows else None


async def owned_agents_for_trial(
    db: AsyncSession,
    *,
    owner_phone: str | None,
    self_agent_id: str | None,
    limit: int = 20,
) -> list[Agent]:
    """Resolve user-owned non-builder agents eligible for shared WA trial links."""
    if not owner_phone:
        return []
    stmt = (
        select(Agent)
        .where(Agent.is_deleted.is_(False), owner_filter(owner_phone))
        .order_by(Agent.created_at.desc(), Agent.updated_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    agents: list[Agent] = []
    for agent in rows:
        if self_agent_id and str(getattr(agent, "id", "")) == str(self_agent_id):
            continue
        capabilities = getattr(agent, "capabilities", None) or []
        tools_config = getattr(agent, "tools_config", None) or {}
        if "builder" in capabilities or (isinstance(tools_config, dict) and tools_config.get("builder")):
            continue
        agents.append(agent)
    return agents

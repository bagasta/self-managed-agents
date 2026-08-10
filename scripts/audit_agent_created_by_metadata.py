"""
Audit/backfill created-by metadata for existing agents.

Default mode is read-only. Use --apply to persist high-confidence fixes.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/audit_agent_created_by_metadata.py
    PYTHONPATH=. .venv/bin/python scripts/audit_agent_created_by_metadata.py --apply
    PYTHONPATH=. .venv/bin/python scripts/audit_agent_created_by_metadata.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.models.document import Document
from app.models.memory import Memory


ARTHUR_MARKERS = (
    "dibuat dan dikonfigurasi oleh arthur",
    "dibuat/dikonfigurasi lewat arthur",
    "agent builder",
    "identitas platform dan owner",
)
ARTHUR_MEMORY_KEYS = {"platform_identity", "agent_blueprint", "soul"}


@dataclass(frozen=True)
class CreatedByInference:
    created_by_type: str | None
    created_by_agent_id: str | None
    created_by_agent_name: str | None
    confidence: str
    reason: str


@dataclass(frozen=True)
class AgentCreatedByAuditRow:
    agent_id: str
    name: str
    current_created_by_type: str | None
    inferred_created_by_type: str | None
    inferred_created_by_agent_name: str | None
    confidence: str
    reason: str
    action: str
    readiness_status: str
    readiness_category: str
    document_count: int | None
    blockers: list[str]
    warnings: list[str]


def _text_contains_any(text: str | None, markers: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in markers)


def _tool_enabled(tools_config: dict[str, Any] | None, key: str, *, default: bool = False) -> bool:
    if not isinstance(tools_config, dict):
        return default
    value = tools_config.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("enabled", default))
    return bool(value)


def _has_google_workspace_tools(tools_config: dict[str, Any] | None) -> bool:
    if not isinstance(tools_config, dict):
        return False
    mcp_cfg = tools_config.get("mcp")
    if not isinstance(mcp_cfg, dict):
        return False
    if "servers" in mcp_cfg or "enabled" in mcp_cfg:
        return bool(mcp_cfg.get("enabled")) and "google_workspace" in (mcp_cfg.get("servers") or {})
    return isinstance(mcp_cfg.get("google_workspace"), dict)


def _is_platform_agent(agent: Any) -> bool:
    capabilities = set(getattr(agent, "capabilities", None) or [])
    tools_config = getattr(agent, "tools_config", None) or {}
    return "system" in capabilities or "builder" in capabilities or _tool_enabled(tools_config, "builder", default=False)


def _classify_readiness(
    agent: Any,
    *,
    inference: CreatedByInference,
    action: str,
    document_count: int | None,
) -> tuple[str, str, list[str], list[str]]:
    tools_config = getattr(agent, "tools_config", None) or {}
    blockers: list[str] = []
    warnings: list[str] = []

    platform_agent = _is_platform_agent(agent)
    if not platform_agent and not (getattr(agent, "owner_external_id", None) or (getattr(agent, "operator_ids", None) or [])):
        blockers.append("owner_missing")
    if action == "needs_manual_review" or inference.confidence == "unknown":
        warnings.append("created_by_metadata_needs_manual_review")

    if _has_google_workspace_tools(tools_config):
        blockers.append("google_auth_required")
    if _tool_enabled(tools_config, "rag", default=False):
        if document_count is None:
            warnings.append("rag_document_count_unknown")
        elif document_count == 0:
            blockers.append("rag_documents_required")
    if getattr(agent, "channel_type", None) == "whatsapp" and not getattr(agent, "wa_device_id", None):
        blockers.append("whatsapp_setup_required")

    escalation_cfg = getattr(agent, "escalation_config", None) or {}
    instructions = (getattr(agent, "instructions", None) or "").lower()
    context = " ".join([
        str(getattr(agent, "name", "") or "").lower(),
        str(getattr(agent, "description", "") or "").lower(),
        instructions,
    ])
    needs_handoff = any(word in context for word in ("bayar", "payment", "transfer", "approve", "approval", "admin"))
    if needs_handoff and not (_tool_enabled(tools_config, "escalation", default=False) or escalation_cfg.get("operator_phone")):
        blockers.append("escalation_required")

    if blockers:
        return "launch_blocked", "needs_fix", blockers, warnings
    if warnings:
        return "launch_ready_with_warnings", "needs_manual_review", blockers, warnings
    return "launch_ready", "ready", blockers, warnings


def infer_created_by_metadata(
    agent: Any,
    *,
    global_memory_keys: set[str] | None = None,
    platform_identity_text: str | None = None,
    arthur_agent_id: str | None = None,
) -> CreatedByInference:
    """Infer only high-confidence metadata for legacy agents."""
    capabilities = set(getattr(agent, "capabilities", None) or [])
    current_type = getattr(agent, "created_by_type", None)
    if current_type:
        return CreatedByInference(
            created_by_type=str(current_type),
            created_by_agent_id=getattr(agent, "created_by_agent_id", None),
            created_by_agent_name=getattr(agent, "created_by_agent_name", None),
            confidence="existing",
            reason="metadata already present",
        )

    if "system" in capabilities:
        return CreatedByInference(
            created_by_type="system",
            created_by_agent_id=None,
            created_by_agent_name="System",
            confidence="high",
            reason="agent has system capability",
        )

    instructions = getattr(agent, "instructions", "") or ""
    memory_keys = global_memory_keys or set()
    if (
        _text_contains_any(instructions, ARTHUR_MARKERS)
        or _text_contains_any(platform_identity_text, ARTHUR_MARKERS)
        or "platform_identity" in memory_keys
        or "agent_blueprint" in memory_keys
    ):
        return CreatedByInference(
            created_by_type="arthur_builder",
            created_by_agent_id=arthur_agent_id,
            created_by_agent_name="Arthur",
            confidence="high",
            reason="Arthur platform identity or builder memory found",
        )

    return CreatedByInference(
        created_by_type=None,
        created_by_agent_id=None,
        created_by_agent_name=None,
        confidence="unknown",
        reason="no reliable source metadata found",
    )


async def _find_arthur_agent_id(db: AsyncSession) -> str | None:
    result = await db.execute(
        select(Agent).where(
            Agent.name == "Arthur",
            Agent.capabilities.contains(["system"]),
            Agent.is_deleted.is_(False),
        )
    )
    arthur = result.scalar_one_or_none()
    return str(arthur.id) if arthur else None


async def _global_memories_for_agent(db: AsyncSession, agent_id: Any) -> tuple[set[str], str | None]:
    result = await db.execute(
        select(Memory).where(Memory.agent_id == agent_id, Memory.scope.is_(None))
    )
    rows = result.scalars().all()
    keys = {row.key for row in rows if row.key in ARTHUR_MEMORY_KEYS}
    platform_identity = next((row.value_data for row in rows if row.key == "platform_identity"), None)
    return keys, platform_identity


async def _document_count_for_agent(db: AsyncSession, agent_id: Any) -> int | None:
    try:
        result = await db.execute(select(func.count()).where(Document.agent_id == agent_id))
        return int(result.scalar_one() or 0)
    except Exception:
        return None


async def audit_created_by_metadata(*, apply: bool = False) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        arthur_agent_id = await _find_arthur_agent_id(db)
        result = await db.execute(select(Agent).where(Agent.is_deleted.is_(False)).order_by(Agent.created_at.asc()))
        agents = result.scalars().all()

        rows: list[AgentCreatedByAuditRow] = []
        updated = 0
        missing = 0
        already_ok = 0
        for agent in agents:
            memory_keys, platform_identity = await _global_memories_for_agent(db, agent.id)
            document_count = await _document_count_for_agent(db, agent.id)
            inference = infer_created_by_metadata(
                agent,
                global_memory_keys=memory_keys,
                platform_identity_text=platform_identity,
                arthur_agent_id=arthur_agent_id,
            )

            current_type = getattr(agent, "created_by_type", None)
            if inference.confidence == "existing":
                action = "already_ok"
                already_ok += 1
            elif inference.created_by_type and inference.confidence == "high":
                action = "would_update"
                if apply:
                    agent.created_by_type = inference.created_by_type
                    agent.created_by_agent_id = inference.created_by_agent_id
                    agent.created_by_agent_name = inference.created_by_agent_name
                    action = "updated"
                    updated += 1
            else:
                action = "needs_manual_review"
                missing += 1

            readiness_status, readiness_category, blockers, warnings = _classify_readiness(
                agent,
                inference=inference,
                action=action,
                document_count=document_count,
            )

            rows.append(
                AgentCreatedByAuditRow(
                    agent_id=str(agent.id),
                    name=agent.name,
                    current_created_by_type=current_type,
                    inferred_created_by_type=inference.created_by_type,
                    inferred_created_by_agent_name=inference.created_by_agent_name,
                    confidence=inference.confidence,
                    reason=inference.reason,
                    action=action,
                    readiness_status=readiness_status,
                    readiness_category=readiness_category,
                    document_count=document_count,
                    blockers=blockers,
                    warnings=warnings,
                )
            )

        if apply:
            await db.commit()

    category_counts = {
        "ready": sum(1 for row in rows if row.readiness_category == "ready"),
        "needs_fix": sum(1 for row in rows if row.readiness_category == "needs_fix"),
        "needs_manual_review": sum(1 for row in rows if row.readiness_category == "needs_manual_review"),
    }

    return {
        "apply": apply,
        "total": len(rows),
        "already_ok": already_ok,
        "updated": updated,
        "would_update": sum(1 for row in rows if row.action == "would_update"),
        "needs_manual_review": missing,
        "readiness": category_counts,
        "rows": [asdict(row) for row in rows],
    }


def _print_human_report(report: dict[str, Any]) -> None:
    mode = "APPLY" if report["apply"] else "DRY RUN"
    print(f"Agent created-by metadata audit ({mode})")
    print(
        "total={total} already_ok={already_ok} updated={updated} "
        "would_update={would_update} needs_manual_review={needs_manual_review}".format(**report)
    )
    readiness = report.get("readiness") or {}
    print(
        "readiness ready={ready} needs_fix={needs_fix} needs_manual_review={needs_manual_review}".format(
            ready=readiness.get("ready", 0),
            needs_fix=readiness.get("needs_fix", 0),
            needs_manual_review=readiness.get("needs_manual_review", 0),
        )
    )
    for row in report["rows"]:
        if row["action"] == "already_ok" and row["readiness_category"] == "ready":
            continue
        print(
            "- {readiness_category}/{action}: {name} ({agent_id}) -> {inferred_created_by_type} "
            "[{confidence}] blockers={blockers} warnings={warnings} {reason}".format(**row)
        )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Audit/backfill Agent.created_by_* metadata")
    parser.add_argument("--apply", action="store_true", help="Persist high-confidence inferred metadata")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    report = await audit_created_by_metadata(apply=args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human_report(report)


if __name__ == "__main__":
    asyncio.run(_main())

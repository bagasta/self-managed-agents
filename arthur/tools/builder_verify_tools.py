"""Post-create verification tool for Arthur builder."""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from langchain_core.tools import tool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.domain.agent_sop_service import (
    get_latest_agent_operating_manual,
    operating_manual_readiness_issues,
    summarize_operating_manual,
)
from app.core.tools.builder_catalog import AGENT_PRESETS, RUNTIME_LIMITATIONS
from app.core.tools.builder_google import has_google_workspace_tools as _has_google_workspace_tools
from app.core.tools.builder_identity import agent_created_by_metadata as _agent_created_by_metadata
from app.core.tools.builder_intent import _critical_workflow_config_errors, _detect_preset_from_config
from app.core.tools.builder_planning_tools import _get_post_create_steps
from app.models.agent import Agent
from app.models.document import Document

logger = structlog.get_logger(__name__)

def _tool_config_enabled(tools_config: dict[str, Any] | None, key: str, *, default: bool = False) -> bool:
    if not isinstance(tools_config, dict):
        return default
    cfg = tools_config.get(key)
    if cfg is None:
        return default
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", default))
    return bool(cfg)


def _rag_enabled(tools_config: dict[str, Any] | None) -> bool:
    return _tool_config_enabled(tools_config, "rag", default=False)


async def _count_agent_documents(db: AsyncSession, agent_id: uuid.UUID) -> int | None:
    try:
        result = await db.execute(select(func.count()).where(Document.agent_id == agent_id))
        return int(result.scalar_one() or 0)
    except Exception as exc:
        logger.warning("builder_tools.verify_agent.document_count_failed", error=str(exc))
        return None


def _build_owner_setup_status(
    *,
    launch_status: str,
    owner_present: bool,
    created_by_present: bool,
    operating_manual: dict[str, Any] | None = None,
    google_workspace_enabled: bool,
    rag_enabled: bool,
    document_count: int | None,
    whatsapp_channel: bool,
    whatsapp_ready: bool,
    escalation_enabled: bool,
    readiness_blockers: list[str],
    readiness_warnings: list[str],
    media_enabled: bool = False,
) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    next_steps: list[str] = []

    def add_item(key: str, status: str, message: str) -> None:
        items.append({"key": key, "status": status, "message": message})

    add_item(
        "owner",
        "ready" if owner_present else "needs_setup",
        "Owner agent sudah tercatat." if owner_present else "Owner agent belum tercatat. Simpan nomor Owner dulu agar agent tahu siapa pemiliknya.",
    )
    if not owner_present:
        next_steps.append("Tambahkan nomor Owner agent.")

    add_item(
        "platform_identity",
        "ready" if created_by_present else "needs_review",
        "Sumber pembuatan agent sudah tercatat." if created_by_present else "Agent lama belum punya metadata pembuat. Runtime tetap aman, tapi data ini perlu dirapikan.",
    )
    if not created_by_present:
        next_steps.append("Review metadata pembuat agent lama.")

    manual = operating_manual or {}
    manual_present = bool(manual.get("present"))
    manual_maturity = str(manual.get("maturity") or "missing")
    if not manual_present:
        add_item(
            "operating_manual",
            "needs_setup",
            "SOP kerja agent belum dibuat terpisah. Buat Agent Operating Manual dulu agar cara kerja agent bisa dicek.",
        )
        next_steps.append("Buat atau review SOP kerja agent.")
    elif manual_maturity in {"draft", "needs_review"}:
        _sop_draft_msg = "SOP kerja agent masih draft. Agent aman untuk tanya kebutuhan dan membuat ringkasan, tapi belum boleh mengambil keputusan final."
        if media_enabled:
            _sop_draft_msg += (
                " Catatan: pengiriman file/gambar via WhatsApp dinonaktifkan sementara sampai SOP di-review"
                f" (maturity={manual_maturity}). Setelah SOP usable, fitur kirim file/gambar aktif lagi."
            )
        add_item(
            "operating_manual",
            "needs_review",
            _sop_draft_msg,
        )
        next_steps.append("Lengkapi dan review SOP kerja agent sebelum full launch.")
    elif manual_maturity == "verified":
        add_item("operating_manual", "ready", "SOP kerja agent sudah verified.")
    else:
        add_item(
            "operating_manual",
            "ready",
            "SOP kerja agent sudah tersedia dan bisa dipakai untuk workflow utama.",
        )

    if google_workspace_enabled:
        add_item(
            "google_workspace",
            "needs_setup",
            "Google sudah dipilih, tapi Owner perlu login atau sambungkan ulang Google sebelum agent boleh menjalankan tugas Google.",
        )
        next_steps.append("Minta Owner login atau sambungkan ulang Google Workspace.")
    else:
        add_item("google_workspace", "not_used", "Google Workspace tidak dipakai untuk agent ini.")

    if rag_enabled:
        if document_count is None:
            add_item("knowledge_base", "needs_review", "Knowledge base aktif, tapi jumlah dokumen belum bisa dicek.")
            next_steps.append("Cek dokumen knowledge base agent.")
        elif document_count > 0:
            add_item("knowledge_base", "ready", f"Knowledge base sudah berisi {document_count} dokumen.")
        else:
            add_item(
                "knowledge_base",
                "needs_setup",
                "Knowledge base aktif, tapi belum ada dokumen. Upload SOP/FAQ/dokumen dulu agar agent bisa menjawab dari dokumen.",
            )
            next_steps.append("Upload dokumen knowledge base untuk agent.")
    else:
        add_item("knowledge_base", "not_used", "Knowledge base/RAG tidak dipakai untuk agent ini.")

    if whatsapp_channel:
        add_item(
            "whatsapp",
            "ready" if whatsapp_ready else "needs_setup",
            "WhatsApp agent sudah punya device/nomor." if whatsapp_ready else "WhatsApp belum punya device/nomor aktif. Owner dapat memilih nomor demo Arthur atau memasang nomor khusus.",
        )
        if not whatsapp_ready:
            next_steps.append("Tawarkan nomor demo Arthur atau pemasangan nomor khusus, lalu jalankan pilihan Owner.")
    else:
        add_item("whatsapp", "not_used", "Agent ini bukan channel WhatsApp.")

    add_item(
        "human_handoff",
        "ready" if escalation_enabled else "not_used",
        "Handoff ke manusia aktif." if escalation_enabled else "Handoff ke manusia tidak aktif. Aktifkan jika workflow butuh approval admin/operator.",
    )

    if readiness_blockers:
        summary = "Agent belum siap launch. Ada setup yang perlu dibereskan dulu."
    elif readiness_warnings:
        summary = "Agent bisa dites, tapi ada data lama yang sebaiknya dirapikan."
    else:
        summary = "Agent siap dites atau digunakan."

    return {
        "status": launch_status,
        "summary_for_owner": summary,
        "items": items,
        "next_steps": next_steps,
    }


def build_builder_verify_tools(db_factory: async_sessionmaker) -> dict[str, Any]:
    @tool
    async def verify_agent(agent_id: str) -> str:
        """
        Verifikasi agent yang baru dibuat: baca kembali konfigurasinya dari DB,
        cek apakah tools_config sudah benar, dan berikan panduan smoke test.

        Gunakan ini SETELAH create_agent untuk konfirmasi bahwa agent terbuat dengan benar.

        Args:
            agent_id: UUID agent yang baru dibuat
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            document_count = (
                await _count_agent_documents(db, agent.id)
                if agent and _rag_enabled(agent.tools_config or {})
                else None
            )
            operating_manual = (
                await get_latest_agent_operating_manual(
                    agent.id,
                    db,
                    fallback_tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                )
                if agent
                else None
            )
        if not agent:
            return f"[error] Agent dengan ID {agent_id} tidak ditemukan setelah create — kemungkinan create gagal"

        tc: dict = agent.tools_config or {}
        active_tools = [k for k, v in tc.items() if v and v is not False]
        google_workspace_enabled = _has_google_workspace_tools(tc)
        rag_is_enabled = _rag_enabled(tc)
        operating_manual_summary = summarize_operating_manual(operating_manual)
        instructions_text = agent.instructions or ""
        created_by_metadata = _agent_created_by_metadata(agent)

        # Detect what preset this looks like
        detected_preset = _detect_preset_from_config(tc, agent.channel_type or "")

        # Check for config issues
        config_warnings: list[str] = []
        if tc.get("deploy") and not tc.get("sandbox"):
            config_warnings.append("CRITICAL: deploy aktif tapi sandbox tidak aktif — deploy akan gagal")
        if tc.get("tool_creator") and not tc.get("sandbox"):
            config_warnings.append("CRITICAL: tool_creator aktif tapi sandbox tidak aktif")
        if agent.channel_type == "whatsapp" and not tc.get("escalation"):
            config_warnings.append("WARNING: Agent WhatsApp tanpa escalation — tidak ada operator handoff")

        owner_present = bool(getattr(agent, "owner_external_id", None) or (getattr(agent, "operator_ids", None) or []))
        created_by_present = bool(created_by_metadata["created_by_type"])
        platform_identity_present = (
            created_by_metadata["created_by_type"] == "arthur_builder"
            or "IDENTITAS PLATFORM DAN OWNER" in instructions_text
            or "dibuat dan dikonfigurasi oleh Arthur" in instructions_text
        )
        readiness_blockers: list[str] = []
        readiness_warnings: list[str] = []
        if not owner_present:
            readiness_blockers.append("owner_missing: agent belum punya owner_external_id/operator_ids yang jelas.")
        if not created_by_present:
            readiness_warnings.append(
                "created_by_metadata_missing: agent lama belum punya metadata created_by_* dari DB."
            )
        if not platform_identity_present:
            readiness_warnings.append(
                "platform_identity_missing: runtime tetap inject Owner/tools, tapi instructions lama belum punya identitas dibuat Arthur."
            )
        if google_workspace_enabled:
            readiness_blockers.append(
                "google_auth_required: Google Workspace aktif; Owner harus login/reauth sebelum agent boleh mengklaim aksi Google berhasil."
            )
        if rag_is_enabled:
            if document_count == 0:
                readiness_blockers.append(
                    "rag_documents_required: Knowledge base/RAG aktif, tapi belum ada dokumen. Owner harus upload dokumen sebelum agent boleh mengklaim jawaban berdasarkan dokumen."
                )
            elif document_count is None:
                readiness_warnings.append(
                    "rag_documents_unknown: Knowledge base/RAG aktif, tapi jumlah dokumen belum bisa dicek."
                )
        if agent.channel_type == "whatsapp" and not getattr(agent, "wa_device_id", None):
            readiness_blockers.append(
                "whatsapp_setup_required: agent WhatsApp belum punya wa_device_id/nomor yang siap dipakai."
            )
        readiness_blockers.extend(
            f"workflow_invalid: {error}"
            for error in _critical_workflow_config_errors(
                name=agent.name or "",
                description=agent.description or "",
                instructions=instructions_text,
                tools_config=tc,
                preset_id=detected_preset,
            )
        )
        manual_blockers, manual_warnings = operating_manual_readiness_issues(operating_manual)
        readiness_blockers.extend(manual_blockers)
        readiness_warnings.extend(manual_warnings)
        readiness_warnings.extend(config_warnings)
        launch_status = "launch_blocked" if readiness_blockers else (
            "launch_ready_with_warnings" if readiness_warnings else "launch_ready"
        )

        # Surface applicable limitations
        preset = AGENT_PRESETS.get(detected_preset, {})
        applicable_limitations = [
            RUNTIME_LIMITATIONS[l]["user_message"]
            for l in preset.get("runtime_limitations", [])
            if l in RUNTIME_LIMITATIONS
        ]

        smoke_test = preset.get("smoke_test", {})
        post_create = _get_post_create_steps(detected_preset, agent.channel_type or "whatsapp", tc)
        setup_status_for_owner = _build_owner_setup_status(
            launch_status=launch_status,
            owner_present=owner_present,
            created_by_present=created_by_present,
            operating_manual=operating_manual_summary,
            google_workspace_enabled=google_workspace_enabled,
            rag_enabled=rag_is_enabled,
            document_count=document_count,
            whatsapp_channel=agent.channel_type == "whatsapp",
            whatsapp_ready=bool(getattr(agent, "wa_device_id", None)),
            escalation_enabled=_tool_config_enabled(tc, "escalation", default=False),
            readiness_blockers=readiness_blockers,
            readiness_warnings=readiness_warnings,
            media_enabled=_tool_config_enabled(tc, "whatsapp_media", default=False),
        )

        summary = {
            "status": launch_status,
            "agent_id": str(agent.id),
            "name": agent.name,
            "model": agent.model,
            "channel_type": agent.channel_type,
            "owner_external_id": getattr(agent, "owner_external_id", None),
            "operator_ids": getattr(agent, "operator_ids", None) or [],
            **created_by_metadata,
            "active_tools": active_tools,
            "google_workspace_enabled": google_workspace_enabled,
            "needs_google_auth": google_workspace_enabled,
            "rag_enabled": rag_is_enabled,
            "document_count": document_count,
            "operating_manual": operating_manual_summary,
            "max_tokens": agent.max_tokens,
            "detected_preset": detected_preset,
            "config_warnings": config_warnings,
            "launch_readiness": {
                "status": launch_status,
                "owner_present": owner_present,
                "created_by_present": created_by_present,
                "platform_identity_present": platform_identity_present,
                "created_by_metadata": created_by_metadata,
                "needs_google_auth": google_workspace_enabled,
                "rag_enabled": rag_is_enabled,
                "document_count": document_count,
                "operating_manual": operating_manual_summary,
                "blockers": readiness_blockers,
                "warnings": readiness_warnings,
            },
            "setup_status_for_owner": setup_status_for_owner,
            "applicable_limitations": applicable_limitations,
            "required_next_steps": post_create,
            "smoke_test_steps": smoke_test.get("steps", []),
            "smoke_test_expected": smoke_test.get("expected_status", ""),
            "known_failure_modes": smoke_test.get("known_failure_modes", []),
            "instructions_preview": instructions_text[:200] + ("..." if len(instructions_text) > 200 else ""),
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    return {"verify_agent": verify_agent}

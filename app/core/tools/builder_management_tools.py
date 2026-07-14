"""Per-user agent management tools for Arthur builder."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.domain.agent_sop_service import get_latest_agent_operating_manual, summarize_operating_manual
from app.core.tools.builder_google import has_google_workspace_tools as _has_google_workspace_tools
from app.core.tools.builder_identity import (
    agent_belongs_to_owner as _agent_belongs_to_owner,
    agent_created_by_metadata as _agent_created_by_metadata,
)
from app.models.agent import Agent

logger = structlog.get_logger(__name__)

LoggerProvider = Callable[[], Any]


def build_builder_management_tools(
    db_factory: async_sessionmaker,
    *,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    session_id: str | None = None,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _get_logger = get_logger or (lambda: logger)

    @tool
    async def set_agent_memory(agent_id: str, key: str, value: str) -> str:
        """
        Simpan/update memory global untuk agent milik user ini secara langsung ke database.
        Gunakan ini sebagai fallback jika soul/blueprint belum dikirim saat create_agent.

        Args:
            agent_id: UUID agent yang memory-nya akan diubah
            key: Nama memory, misalnya "soul" atau "agent_blueprint"
            value: Isi memory
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"
        if not key.strip():
            return "[error] key memory wajib diisi"
        if not value.strip():
            return "[error] value memory wajib diisi"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses ke agent ini"

            from app.core.domain.memory_service import upsert_memory

            await upsert_memory(agent.id, key.strip(), value.strip(), db, scope=None)
            await db.commit()

        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "key": key.strip(),
            "message": f"Memory '{key.strip()}' berhasil disimpan.",
        }, ensure_ascii=False, indent=2)

    @tool
    async def delete_agent(
        agent_id: str,
        confirm_name: str = "",
    ) -> str:
        """
        Hapus agent milik user ini secara soft-delete.

        Gunakan hanya setelah user eksplisit meminta hapus/delete agent dan sudah
        mengonfirmasi nama agent. Jika user belum menyebut agent mana, panggil
        list_my_agents() dulu. Jika confirm_name kosong atau tidak sama dengan
        nama agent, tool akan meminta konfirmasi dan tidak menghapus.

        Args:
            agent_id: UUID agent yang akan dihapus
            confirm_name: Nama agent persis sebagai konfirmasi hapus
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        if self_agent_id and str(agent_uuid) == self_agent_id:
            return "[error] Arthur tidak boleh menghapus dirinya sendiri."

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses untuk menghapus agent ini"

            expected_name = (agent.name or "").strip()
            if not confirm_name or confirm_name.strip() != expected_name:
                return json.dumps({
                    "success": False,
                    "needs_confirmation": True,
                    "agent_id": str(agent.id),
                    "agent_name": expected_name,
                    "message": (
                        f"Konfirmasi dulu sebelum menghapus agent '{expected_name}'. "
                        "Panggil delete_agent lagi dengan confirm_name persis sama dengan nama agent."
                    ),
                }, ensure_ascii=False, indent=2)

            wa_device_id = agent.wa_device_id
            wa_disconnect_error = ""
            if wa_device_id and not str(wa_device_id).startswith("wadev_"):
                try:
                    from app.core.infra.wa_client import delete_wa_device

                    await delete_wa_device(wa_device_id)
                except Exception as exc:
                    wa_disconnect_error = str(exc)
                    _get_logger().warning(
                        "builder_tools.delete_agent.wa_disconnect_failed",
                        agent_id=str(agent.id),
                        error=wa_disconnect_error,
                    )

            agent.is_deleted = True
            agent.version = (agent.version or 1) + 1
            await db.commit()

        _get_logger().info("builder_tools.delete_agent.success", agent_id=agent_id, owner_phone=owner_phone)
        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "agent_name": expected_name,
            "wa_device_id": wa_device_id,
            "wa_disconnect_error": wa_disconnect_error,
            "message": f"Agent '{expected_name}' berhasil dihapus.",
        }, ensure_ascii=False, indent=2)

    @tool
    async def get_agent_detail(agent_id: str, include_instructions: bool = False) -> str:
        """
        Baca konfigurasi lengkap sebuah agent. Gunakan untuk review sebelum update,
        atau untuk debugging konfigurasi agent yang sudah ada.

        Args:
            agent_id: UUID agent yang ingin dilihat
            include_instructions: True jika perlu membaca full instructions sebelum update.
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
        if not agent:
            return f"[error] Agent dengan ID {agent_id} tidak ditemukan"

        # Cek kepemilikan — bypass jika membaca diri sendiri
        is_self = self_agent_id and str(agent_uuid) == self_agent_id
        if not is_self and owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
            return f"[error] Kamu tidak punya akses ke agent ini"

        memory_summary: dict[str, str] = {}
        try:
            from app.core.domain.memory_service import get_memory

            async with db_factory() as db:
                soul_mem = await get_memory(agent.id, "soul", db, scope=None)
                blueprint_mem = await get_memory(agent.id, "agent_blueprint", db, scope=None)
                platform_identity_mem = await get_memory(agent.id, "platform_identity", db, scope=None)
            if soul_mem:
                soul_value = getattr(soul_mem, "value_data", "")
                if isinstance(soul_value, str):
                    memory_summary["soul_preview"] = soul_value[:500] + (
                        "..." if len(soul_value) > 500 else ""
                    )
            if blueprint_mem:
                blueprint_value = getattr(blueprint_mem, "value_data", "")
                if isinstance(blueprint_value, str):
                    memory_summary["agent_blueprint_preview"] = blueprint_value[:800] + (
                        "..." if len(blueprint_value) > 800 else ""
                    )
            if platform_identity_mem:
                platform_identity_value = getattr(platform_identity_mem, "value_data", "")
                if isinstance(platform_identity_value, str):
                    memory_summary["platform_identity_preview"] = platform_identity_value[:500] + (
                        "..." if len(platform_identity_value) > 500 else ""
                    )
        except Exception as exc:
            memory_summary["memory_warning"] = f"Gagal membaca memory agent: {exc}"

        instructions_text = agent.instructions or ""
        created_by_metadata = _agent_created_by_metadata(agent)
        operating_manual = None
        try:
            async with db_factory() as db:
                operating_manual = await get_latest_agent_operating_manual(
                    agent.id,
                    db,
                    fallback_tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                )
        except Exception as exc:
            memory_summary["operating_manual_warning"] = f"Gagal membaca SOP agent: {exc}"
        payload = {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "model": agent.model,
            "temperature": agent.temperature,
            "tools_config": agent.tools_config,
            "google_workspace_enabled": _has_google_workspace_tools(
                agent.tools_config if isinstance(agent.tools_config, dict) else {}
            ),
            "instructions_include_google_workspace": "Google Workspace" in (agent.instructions or ""),
            "escalation_config": agent.escalation_config,
            "operator_ids": agent.operator_ids,
            "allowed_senders": agent.allowed_senders,
            **created_by_metadata,
            "launch_metadata": {
                "owner_present": bool(getattr(agent, "owner_external_id", None) or (getattr(agent, "operator_ids", None) or [])),
                "created_by_present": bool(created_by_metadata["created_by_type"]),
                "created_by_arthur": created_by_metadata["created_by_type"] == "arthur_builder",
                "operating_manual": summarize_operating_manual(operating_manual),
            },
            "channel_type": agent.channel_type,
            "wa_device_id": agent.wa_device_id,
            "token_quota": agent.token_quota,
            "tokens_used": agent.tokens_used,
            "active_until": agent.active_until.isoformat() if agent.active_until else None,
            "version": agent.version,
            "instructions_len": len(instructions_text),
            "instructions_preview": instructions_text[:300] + ("..." if len(instructions_text) > 300 else ""),
            "memory": memory_summary,
        }
        if include_instructions:
            payload["instructions"] = instructions_text
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @tool
    async def list_my_agents() -> str:
        """
        Tampilkan semua agent yang kamu buat/miliki di platform ini.
        Agent diidentifikasi berdasarkan nomor WA yang sedang digunakan untuk chat.
        """
        if not owner_phone:
            return "[error] Tidak bisa identifikasi pemilik — pastikan kamu chat dari nomor WA yang terdaftar"

        try:
            async with db_factory() as db:
                result = await db.execute(
                    select(Agent).where(
                        Agent.is_deleted.is_(False),
                        ~Agent.capabilities.contains(["system"]),
                    )
                )
                all_agents = result.scalars().all()

                my_agents = [a for a in all_agents if _agent_belongs_to_owner(a, owner_phone)]

            if not my_agents:
                return json.dumps({
                    "count": 0,
                    "agents": [],
                    "message": "Kamu belum punya agent. Mau saya bantu buatkan yang pertama?",
                }, ensure_ascii=False, indent=2)

            return json.dumps({
                "count": len(my_agents),
                "agents": [
                    {
                        "id": str(a.id),
                        "name": a.name,
                        "description": a.description,
                        "model": a.model,
                        "channel_type": a.channel_type,
                        "wa_device_id": a.wa_device_id,
                        **_agent_created_by_metadata(a),
                        "launch_metadata": {
                            "owner_present": bool(getattr(a, "owner_external_id", None) or (getattr(a, "operator_ids", None) or [])),
                            "created_by_present": bool(_agent_created_by_metadata(a)["created_by_type"]),
                            "created_by_arthur": _agent_created_by_metadata(a)["created_by_type"] == "arthur_builder",
                        },
                        "token_quota": a.token_quota,
                        "tokens_used": a.tokens_used,
                        "active_until": a.active_until.isoformat() if a.active_until else None,
                        "tools_active": [k for k, v in (a.tools_config or {}).items() if v],
                    }
                    for a in my_agents
                ],
            }, ensure_ascii=False, indent=2)

        except Exception as exc:
            _get_logger().error("builder_tools.list_my_agents.error", error=str(exc))
            return f"[error] Gagal mengambil daftar agent: {exc}"

    @tool
    async def renew_agent(agent_id: str) -> str:
        """Perpanjang masa aktif dan reset kuota token agent milik user ini.

        Gunakan setelah memastikan agent terkait memang expired atau user meminta
        renewal. Jangan membuat kode WA trial baru sebelum renewal ini sukses.
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
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"
            if "system" in set(agent.capabilities or []):
                return "[error] Agent sistem tidak dapat diperpanjang melalui Arthur"
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses untuk memperpanjang agent ini"

            period_days = max(1, int(agent.quota_period_days or 30))
            previous_active_until = agent.active_until
            agent.active_until = datetime.now(timezone.utc) + timedelta(days=period_days)
            agent.tokens_used = 0
            agent.version = (agent.version or 1) + 1
            agent_name = agent.name
            renewed_until = agent.active_until
            await db.commit()

        _get_logger().info(
            "builder_tools.renew_agent.success",
            agent_id=agent_id,
            owner_phone=owner_phone,
            active_until=renewed_until.isoformat(),
        )
        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "previous_active_until": previous_active_until.isoformat() if previous_active_until else None,
            "active_until": renewed_until.isoformat(),
            "quota_period_days": period_days,
            "message": (
                f"Agent '{agent_name}' sudah aktif kembali sampai "
                f"{renewed_until.isoformat()}. Kuota token di-reset."
            ),
        }, ensure_ascii=False, indent=2)

    @tool
    async def add_agent_knowledge(agent_id: str, filename: str = "", title: str = "") -> str:
        """
        Tambahkan FILE yang dikirim user (via WhatsApp) sebagai knowledge base (RAG)
        milik agent TARGET. File yang baru dikirim user otomatis tersimpan di workspace
        sesi ini; tool mengekstrak teksnya (PDF→OCR, DOCX/PPTX/TXT/MD/CSV), memecah jadi
        chunk, meng-embed, lalu menyimpannya ke tabel documents agent target, dan
        mengaktifkan RAG (search_documents) pada agent itu bila belum aktif.

        PENTING: JANGAN pakai remember/set_agent_memory untuk menambah knowledge dokumen —
        itu hanya memori KV milik Arthur, BUKAN knowledge base agent target. Jangan klaim
        dokumen sudah ditambahkan sebelum tool ini mengembalikan success: true.

        Args:
            agent_id: UUID agent target yang akan diberi knowledge.
            filename: Nama file di workspace (opsional). Kosongkan = pakai file terbaru.
            title: Judul dokumen (opsional). Default: nama file.
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        if not session_id:
            return "[error] Konteks sesi tidak tersedia, tidak bisa membaca file. Minta user kirim ulang file-nya."

        from app.config import get_settings
        from app.core.domain.document_service import create_document
        from app.core.domain.file_processor import (
            SUPPORTED_EXTENSIONS,
            chunk_text,
            extract_text,
        )
        from app.core.infra.sandbox import get_workspace_dir

        workspace = get_workspace_dir(session_id)
        if not workspace.exists():
            return "[error] Belum ada file yang diterima di sesi ini. Minta user kirim dokumennya dulu."

        if filename.strip():
            target_file = workspace / filename.strip()
            if not target_file.is_file():
                return f"[error] File '{filename.strip()}' tidak ditemukan di workspace sesi."
        else:
            candidates = [
                p for p in workspace.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
            ]
            if not candidates:
                return (
                    "[error] Tidak ada file dokumen yang didukung di sesi ini "
                    "(PDF/DOCX/PPTX/TXT/MD/CSV). Minta user kirim file dokumennya."
                )
            target_file = max(candidates, key=lambda p: p.stat().st_mtime)

        ext = target_file.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return (
                f"[error] Tipe file '{ext}' tidak didukung. "
                f"Didukung: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )

        raw = target_file.read_bytes()
        if not raw:
            return f"[error] File {target_file.name} kosong."

        try:
            full_text = await extract_text(
                content=raw,
                filename=target_file.name,
                content_type=None,
                mistral_api_key=get_settings().mistral_api_key,
            )
        except Exception as exc:
            _get_logger().warning("builder_tools.add_agent_knowledge.extract_failed", error=str(exc))
            return f"[error] Gagal mengekstrak teks dari {target_file.name}: {exc}"

        if not full_text.strip():
            return f"[error] Tidak ada teks yang bisa diekstrak dari {target_file.name}."

        doc_title = title.strip() or target_file.name
        chunks = chunk_text(full_text)
        if not chunks:
            return f"[error] Dokumen {target_file.name} tidak menghasilkan chunk teks."

        try:
            async with db_factory() as db:
                result = await db.execute(
                    select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
                )
                agent = result.scalar_one_or_none()
                if not agent:
                    return f"[error] Agent dengan ID {agent_id} tidak ditemukan"
                if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                    return "[error] Kamu tidak punya akses ke agent ini"

                total = len(chunks)
                for i, chunk_content in enumerate(chunks, 1):
                    chunk_title = doc_title if total == 1 else f"{doc_title} (Part {i}/{total})"
                    await create_document(
                        agent_id=agent.id,
                        title=chunk_title,
                        content=chunk_content,
                        source=target_file.name,
                        doc_metadata={
                            "original_filename": target_file.name,
                            "chunk_index": i,
                            "total_chunks": total,
                            "added_by": "arthur_builder",
                        },
                        db=db,
                    )

                tc = dict(agent.tools_config) if isinstance(agent.tools_config, dict) else {}
                rag_was_enabled = bool(tc.get("rag"))
                if not rag_was_enabled:
                    tc["rag"] = True
                    agent.tools_config = tc

                await db.commit()
                agent_name = agent.name or ""
        except Exception as exc:
            _get_logger().error("builder_tools.add_agent_knowledge.error", error=str(exc))
            return f"[error] Gagal menyimpan knowledge ke agent: {exc}"

        _get_logger().info(
            "builder_tools.add_agent_knowledge.ok",
            agent_id=agent_id,
            filename=target_file.name,
            chunks=total,
            rag_was_enabled=rag_was_enabled,
        )
        msg = (
            f"{total} chunk dari '{target_file.name}' berhasil ditambahkan ke "
            f"knowledge base agent '{agent_name}'."
        )
        if not rag_was_enabled:
            msg += " RAG (search_documents) diaktifkan untuk agent ini."
        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "filename": target_file.name,
            "title": doc_title,
            "chunks_added": total,
            "extracted_chars": len(full_text),
            "rag_enabled": True,
            "rag_was_already_enabled": rag_was_enabled,
            "message": msg,
        }, ensure_ascii=False, indent=2)

    return {
        "set_agent_memory": set_agent_memory,
        "delete_agent": delete_agent,
        "get_agent_detail": get_agent_detail,
        "list_my_agents": list_my_agents,
        "renew_agent": renew_agent,
        "add_agent_knowledge": add_agent_knowledge,
    }

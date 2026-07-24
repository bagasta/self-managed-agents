"""Read-only Arthur builder tools."""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.tools.builder_catalog import (
    AGENT_PRESETS,
    RUNTIME_LIMITATIONS,
    _DEFAULT_MODEL,
    _PLATFORM_CHANNELS,
    _RECOMMENDED_MODELS,
    _TOOLS_CONFIG_DOCS,
)
from app.models.agent import Agent

logger = structlog.get_logger(__name__)


def build_builder_read_tools(
    db_factory: async_sessionmaker,
    *,
    self_agent_id: str | None = None,
) -> dict[str, Any]:
    """Build read-only catalog/config tools used by Arthur."""

    @tool
    async def get_self_config() -> str:
        """
        Dapatkan identitas dan kredensial agent builder ini sendiri (Arthur).
        Gunakan untuk mendapatkan agent_id dan konfigurasi agar bisa mengupdate
        diri sendiri lewat update_agent(), tanpa memanggil API platform.
        """
        if not self_agent_id:
            return "[error] self_agent_id tidak tersedia — hubungi administrator"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(
                    Agent.id == uuid.UUID(self_agent_id),
                    Agent.is_deleted.is_(False),
                )
            )
            agent = result.scalar_one_or_none()
        instructions_preview = ""
        if agent:
            instructions_preview = (agent.instructions or "")[:500] + (
                "..." if len(agent.instructions or "") > 500 else ""
            )

        return json.dumps({
            "self_agent_id": self_agent_id,
            "name": agent.name if agent else None,
            "model": agent.model if agent else None,
            "tools_config": agent.tools_config if agent else None,
            "operator_ids": agent.operator_ids if agent else [],
            "instructions_preview": instructions_preview,
            "note": (
                "Gunakan update_agent(agent_id=self_agent_id, ...) untuk mengupdate konfigurasi dirimu sendiri. "
                "Hanya nomor yang ada di operator_ids yang diizinkan melakukan self-update. "
                "Untuk menambah operator baru: update_agent(agent_id=self_agent_id, add_operator='+62xxx')."
            ),
        }, ensure_ascii=False, indent=2)

    @tool
    async def get_platform_capabilities() -> str:
        """
        Dapatkan ringkasan lengkap kapabilitas platform: tools yang tersedia,
        channel yang didukung, model LLM yang bisa dipakai, dan batasan platform.
        Gunakan ini sebelum merancang konfigurasi agent untuk user.
        Untuk detail preset siap pakai, gunakan get_presets().
        """
        result = {
            "default_model": _DEFAULT_MODEL,
            "tools_config_options": _TOOLS_CONFIG_DOCS,
            "supported_channels": _PLATFORM_CHANNELS,
            "recommended_models": _RECOMMENDED_MODELS,
            "available_presets": list(AGENT_PRESETS.keys()),
            "important_tools": {
                "builder": "create_agent/update_agent/delete_agent/get_agent_detail/list_my_agents/set_agent_memory",
                "payment": "get_payment_link untuk link pembayaran Starter/tier_1, Pro/tier_2, dan Enterprise/tier_3",
                "whatsapp": (
                    "create_wa_dev_trial_link untuk nomor demo Arthur; "
                    "send_agent_wa_qr untuk nomor khusus milik user"
                ),
                "coding": "sandbox + deploy + subagents sys_coder",
                "browsing": "tavily_search/tavily_extract untuk web search dan baca URL",
                "productivity": "scheduler untuk reminder, integrasi Google Workspace untuk Docs/Sheets/Drive/Gmail/Calendar",
            },
            "input_types": [
                "teks — pesan tulis biasa",
                "voice_note — audio PTT, otomatis ditranskrip ke teks via Whisper",
                "gambar — bisa dianalisis jika model mendukung vision",
                "dokumen — PDF/DOCX/TXT, bisa diindeks ke RAG",
            ],
            "critical_limitations": [
                RUNTIME_LIMITATIONS["wa_device_scan_required_before_use"]["user_message"],
                RUNTIME_LIMITATIONS["deploy_requires_docker_socket"]["user_message"],
                RUNTIME_LIMITATIONS["deploy_ttl_24h_max"]["user_message"],
                RUNTIME_LIMITATIONS["one_wa_number_per_agent"]["user_message"],
            ],
            "prohibited_agent_purposes": [
                "buzzer",
                "kampanye politik",
                "propaganda politik",
                "manipulasi opini publik",
            ],
            "platform_limitations": {k: v["description"] for k, v in RUNTIME_LIMITATIONS.items()},
            "agent_optional_params": {
                "max_tokens": {
                    "description": "Batas token output LLM per reply. Lebih kecil = lebih hemat biaya.",
                    "guide": {
                        "WA CS / asisten sederhana": "512-800",
                        "Agent dengan analisis/coding": "1024-2048",
                        "Default platform": "1024",
                    },
                },
            },
            "wa_best_practices": [
                "JANGAN gunakan markdown (*bold*, # heading) — tidak dirender di WA",
                "Batasi respons 1-3 paragraf — user WA tidak suka wall of text",
                "Tentukan bahasa eksplisit (Indonesia/Inggris) di instructions",
                "Sertakan instruksi eskalasi: kapan agent harus panggil operator",
                "Tambahkan 1-2 contoh percakapan (few-shot) di instructions",
            ],
            "next": "Gunakan get_presets(preset_id) untuk detail preset spesifik. Jangan panggil tool ini berulang dalam sesi yang sama.",
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    @tool
    async def list_available_wa_devices() -> str:
        """
        Lihat daftar WA device (nomor WhatsApp) yang tersedia di platform
        dan belum di-assign ke agent lain. Gunakan ini untuk membantu user
        memilih nomor WA yang akan dipakai agent mereka.
        """
        try:
            async with db_factory() as db:
                result = await db.execute(
                    select(Agent.wa_device_id, Agent.name)
                    .where(Agent.is_deleted.is_(False), Agent.wa_device_id.isnot(None))
                )
                assigned = {row.wa_device_id: row.name for row in result.all()}
            return json.dumps({
                "assigned_devices": [
                    {"device_id": did, "assigned_to_agent": name}
                    for did, name in assigned.items()
                ],
                "note": (
                    "Untuk membuat agent baru dengan WA: gunakan create_agent dengan "
                    "channel_type='whatsapp'. Platform akan generate device baru secara otomatis. "
                    "User kemudian perlu scan QR untuk menghubungkan nomor WA mereka."
                ),
            }, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("builder_tools.list_wa_devices.error", error=str(exc))
            return f"[error] Gagal mengambil daftar device: {exc}"

    @tool
    async def get_presets(preset_id: str = "") -> str:
        """
        Dapatkan katalog preset agent siap pakai, beserta tools_config, model default,
        batasan runtime, dan panduan smoke test.

        Args:
            preset_id: ID preset spesifik (opsional). Kosong = tampilkan semua preset.
                       Pilihan: coding_deploy_agent, cs_whatsapp_basic, faq_webchat_rag, scheduler_assistant,
                                social_media_agent, data_analyst_agent, research_agent,
                                ecommerce_cs, personal_assistant, hr_assistant
        """
        if preset_id and preset_id in AGENT_PRESETS:
            preset = AGENT_PRESETS[preset_id]
            limitations = {
                lid: RUNTIME_LIMITATIONS[lid]
                for lid in preset.get("runtime_limitations", [])
                if lid in RUNTIME_LIMITATIONS
            }
            return json.dumps({
                "preset_id": preset_id,
                **{k: v for k, v in preset.items() if k != "instruction_skeleton"},
                "instruction_skeleton_preview": preset.get("instruction_skeleton", "")[:300] + "...",
                "runtime_limitations_detail": limitations,
            }, ensure_ascii=False, indent=2)

        if preset_id:
            return json.dumps({
                "error": f"Preset '{preset_id}' tidak ditemukan",
                "available": list(AGENT_PRESETS.keys()),
            }, ensure_ascii=False)

        summary = {}
        for pid, p in AGENT_PRESETS.items():
            critical_lims = [
                RUNTIME_LIMITATIONS[l]["user_message"]
                for l in p.get("runtime_limitations", [])
                if l in RUNTIME_LIMITATIONS and RUNTIME_LIMITATIONS[l]["severity"] == "critical"
            ]
            summary[pid] = {
                "label": p["label"],
                "description": p["description"],
                "default_model": p["default_model"],
                "default_channel": p["default_channel"],
                "key_tools": p["required_tools"],
                "critical_limitations": critical_lims,
                "smoke_test_strategy": p["smoke_test"]["strategy"],
            }
        return json.dumps({"presets": summary}, ensure_ascii=False, indent=2)

    return {
        "get_self_config": get_self_config,
        "get_platform_capabilities": get_platform_capabilities,
        "list_available_wa_devices": list_available_wa_devices,
        "get_presets": get_presets,
    }

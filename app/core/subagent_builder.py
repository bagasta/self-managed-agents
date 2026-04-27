"""
subagent_builder.py — Membangun daftar sub-agent untuk Deep Agents SDK.

Dipecah dari agent_runner.py (item 2.1 production plan).

Fungsi yang diekspor:
  build_subagents(agent_ids, parent_session_id, db, log)

Konstanta:
  _SYSTEM_SUBAGENTS — list preset sub-agents bawaan sistem
"""
from __future__ import annotations

import uuid
from typing import Any

from langchain_openai import ChatOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.sandbox import DockerSandbox
from app.core.tool_builder import (
    _is_enabled,
    build_http_tools,
    build_memory_tools,
    build_sandbox_binary_tool,
    build_skill_tools,
)

settings = get_settings()


# ---------------------------------------------------------------------------
# Built-in system sub-agents
# ---------------------------------------------------------------------------

_SYSTEM_SUBAGENTS: list[dict] = [
    {
        "name": "sys_critic",
        "description": "Quality reviewer: evaluasi output agent lain, approve jika OK atau reject dengan feedback spesifik untuk diperbaiki.",
        "system_prompt": (
            "Kamu adalah agen critic dan quality reviewer. Tugasmu adalah mengevaluasi output yang diberikan kepadamu.\n\n"
            "Cara kerja:\n"
            "1. Baca output yang perlu direview dengan teliti\n"
            "2. Evaluasi berdasarkan: akurasi, kelengkapan, relevansi dengan task, dan kualitas\n"
            "3. Berikan verdict dengan format:\n\n"
            "   **VERDICT: APPROVED** — jika output sudah baik dan bisa digunakan\n"
            "   atau\n"
            "   **VERDICT: REJECTED** — jika output perlu diperbaiki\n\n"
            "4. Jika REJECTED, berikan feedback spesifik: apa yang salah, apa yang kurang, dan apa yang harus diperbaiki\n"
            "5. Jika APPROVED, berikan catatan singkat mengapa output sudah memenuhi standar\n\n"
            "Jadilah kritis tapi konstruktif. Jangan approve output yang mengandung informasi salah, "
            "kode yang error, atau tidak menjawab task dengan benar."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_researcher",
        "description": "Riset spesialis: cari dan rangkum informasi dari internet via HTTP tools.",
        "system_prompt": (
            "Kamu adalah agen riset spesialis. Tugasmu adalah mencari, mengumpulkan, dan merangkum informasi "
            "dari internet secara akurat dan terstruktur.\n\n"
            "Cara kerja:\n"
            "1. Gunakan http_get untuk mengakses URL dan mencari informasi\n"
            "2. Ringkas temuan dengan jelas dan terstruktur\n"
            "3. Sertakan sumber informasi\n"
            "4. Jika informasi tidak ditemukan, jelaskan apa yang kamu coba dan apa hasilnya\n\n"
            "Selalu kembalikan hasil riset yang lengkap, akurat, dan bisa langsung digunakan."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"http": {"enabled": True}, "sandbox": False},
    },
    {
        "name": "sys_coder",
        "description": "Programmer Python spesialis: tulis dan jalankan kode di sandbox.",
        "system_prompt": (
            "Kamu adalah agen programmer spesialis Python. Tugasmu adalah menulis, menjalankan, dan men-debug kode "
            "untuk menyelesaikan task komputasi yang diberikan.\n\n"
            "Cara kerja:\n"
            "1. Pahami task yang diminta\n"
            "2. Tulis kode Python yang bersih menggunakan write_file\n"
            "3. Jalankan di sandbox menggunakan execute\n"
            "4. Jika ada error, debug dan perbaiki\n"
            "5. Kembalikan hasil eksekusi beserta penjelasan singkat\n\n"
            "Untuk library eksternal: execute('pip install <package>')"
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": True, "http": False},
    },
    {
        "name": "sys_writer",
        "description": "Penulis dan editor spesialis: buat, edit, dan format konten tulisan.",
        "system_prompt": (
            "Kamu adalah agen penulis dan editor spesialis. Tugasmu adalah membuat, mengedit, dan memformat "
            "konten tulisan berkualitas tinggi.\n\n"
            "Kemampuan:\n"
            "- Menulis artikel, laporan, email, proposal, dan konten lainnya\n"
            "- Mengedit dan memperbaiki tulisan yang ada\n"
            "- Mengubah format dan tone tulisan sesuai kebutuhan\n"
            "- Menerjemahkan antara Bahasa Indonesia dan Inggris\n\n"
            "Selalu hasilkan tulisan yang jelas, terstruktur, dan sesuai tone yang diminta."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_analyst",
        "description": "Analis data spesialis: olah data, kalkulasi, dan buat laporan analisis.",
        "system_prompt": (
            "Kamu adalah agen analis data spesialis. Tugasmu adalah mengolah data, melakukan kalkulasi, "
            "dan membuat laporan analisis.\n\n"
            "Cara kerja:\n"
            "1. Terima data dalam bentuk teks, CSV, JSON, atau format lain\n"
            "2. Tulis kode Python dengan pandas/numpy menggunakan write_file\n"
            "3. Jalankan analisis di sandbox menggunakan execute\n"
            "4. Buat ringkasan temuan dan insight yang actionable\n"
            "5. Format hasil sebagai tabel atau laporan terstruktur\n\n"
            "Install library: execute('pip install pandas numpy')"
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": True, "http": False},
    },
]


def _build_system_subagent(spec: dict, parent_session_id: uuid.UUID) -> tuple[dict, DockerSandbox | None]:
    """Build a SubAgent dict and optional DockerSandbox from a system sub-agent spec."""
    sub_cfg = spec.get("tools_config", {})
    sub_tools: list = []
    sub_sandbox: DockerSandbox | None = None

    if _is_enabled(sub_cfg, "sandbox", default=False):
        sub_session_id = f"{parent_session_id}_sys_{spec['name']}"
        sub_sandbox = DockerSandbox(sub_session_id)
        sub_tools.extend(build_sandbox_binary_tool(sub_sandbox))

    if _is_enabled(sub_cfg, "http", default=False):
        sub_tools.extend(build_http_tools(sub_cfg))

    sub_llm = ChatOpenAI(
        model=spec["model"],
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4096,
        temperature=0.5,
    )

    sa = {
        "name": spec["name"],
        "description": spec["description"],
        "system_prompt": spec["system_prompt"],
        "tools": sub_tools,
        "model": sub_llm,
    }
    return sa, sub_sandbox


async def build_subagents(
    agent_ids: list[str],
    parent_session_id: uuid.UUID,
    db: AsyncSession,
    log: Any,
) -> tuple[list, list[DockerSandbox]]:
    """
    Build SubAgent list untuk Deep Agents SDK.

    - agent_ids kosong → pakai semua system sub-agents (tidak perlu DB)
    - agent_ids berisi UUID → load agent custom dari DB

    Returns (subagent_list, sandbox_list) — caller wajib close sandboxes di finally block.
    """
    subagents: list = []
    sub_sandboxes: list[DockerSandbox] = []

    if not agent_ids:
        for spec in _SYSTEM_SUBAGENTS:
            sa, ssb = _build_system_subagent(spec, parent_session_id)
            subagents.append(sa)
            if ssb:
                sub_sandboxes.append(ssb)
        log.info("build_subagents.system_defaults", count=len(subagents))
        return subagents, sub_sandboxes

    from app.models.agent import Agent as AgentModel

    for raw_id in agent_ids:
        try:
            agent_uuid = uuid.UUID(raw_id)
        except ValueError:
            log.warning("build_subagents.invalid_uuid", agent_id=raw_id)
            continue

        try:
            result = await db.execute(
                select(AgentModel).where(
                    AgentModel.id == agent_uuid,
                    AgentModel.is_deleted.is_(False),
                )
            )
            agent_row = result.scalar_one_or_none()
        except Exception as exc:
            log.error("build_subagents.db_error", agent_id=raw_id, error=str(exc))
            continue

        if agent_row is None:
            log.warning("build_subagents.not_found", agent_id=raw_id)
            continue

        sub_cfg: dict[str, Any] = agent_row.tools_config if isinstance(agent_row.tools_config, dict) else {}
        sub_tools: list = []
        sub_sandbox: DockerSandbox | None = None

        if _is_enabled(sub_cfg, "sandbox", default=False):
            sub_session_id = f"{parent_session_id}_sub_{agent_uuid}"
            sub_sandbox = DockerSandbox(sub_session_id)
            sub_sandboxes.append(sub_sandbox)
            sub_tools.extend(build_sandbox_binary_tool(sub_sandbox))

        if _is_enabled(sub_cfg, "memory", default=True):
            sub_tools.extend(build_memory_tools(agent_row.id, db, scope=None))

        if _is_enabled(sub_cfg, "skills", default=True):
            sub_tools.extend(build_skill_tools(agent_row.id, db))

        if _is_enabled(sub_cfg, "http", default=False):
            sub_tools.extend(build_http_tools(sub_cfg))

        # Intentionally excluded: escalation, scheduler, wa_agent_manager, tool_creator
        # Subagents do not have channels and should not trigger external side effects.

        sub_backend = None
        if sub_sandbox is not None:
            from app.core.deep_agent_backend import DockerBackend
            sub_backend = DockerBackend(sub_sandbox)

        sub_llm = ChatOpenAI(
            model=agent_row.model or "openai/gpt-4o-mini",
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            max_tokens=4096,
            temperature=getattr(agent_row, "temperature", 0.7),
        )

        sa: dict = {
            "name": agent_row.name,
            "description": (agent_row.instructions or "")[:300].replace("\n", " "),
            "system_prompt": agent_row.instructions or "You are a helpful assistant.",
            "tools": sub_tools,
            "model": sub_llm,
        }

        subagents.append(sa)
        log.info("build_subagents.loaded", name=agent_row.name, tools=len(sub_tools))

    return subagents, sub_sandboxes

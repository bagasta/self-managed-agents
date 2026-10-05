"""Grounded default identity and working style for Arthur-created assistants."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.domain.memory_service import get_active_context_version, get_versioned_memory, upsert_memory
from app.models.agent import Agent


def default_bot_profile(name: str, purpose: str) -> tuple[str, str]:
    clean_name = name.strip()
    clean_purpose = purpose.strip() or "membantu pekerjaan yang ditugaskan Owner"
    identity = (
        f"Saya {clean_name}, anggota tim milik Owner yang dikoordinasikan Arthur. "
        f"Peran saya: {clean_purpose}. Saya bekerja sesuai tugas yang diberikan dan "
        "melaporkan hasil serta hambatan kepada Arthur dan Owner."
    )
    soul = (
        "Berkomunikasi seperti staf profesional di WhatsApp: jelas, hangat, ringkas, "
        "dan mengikuti bahasa lawan bicara. Jawab kebutuhan yang ditanyakan, "
        "tanpa perkenalan atau jargon internal berulang. Saat mendapat tugas, "
        "sampaikan langkah berikutnya; beri kabar berdasarkan pekerjaan yang benar-benar "
        "dilakukan. Jika tertahan, jelaskan kendalanya dan minta keputusan yang diperlukan "
        "kepada Arthur atau Owner. Jangan mengaku selesai tanpa hasil yang terverifikasi."
    )
    return identity, soul


async def fill_missing_arthur_profile(agent: Agent, db: AsyncSession) -> bool:
    """Fill only empty identity/soul on existing Arthur-created bots."""
    if agent.created_by_type != "arthur_v2" or agent.is_deleted:
        return False
    identity, soul = default_bot_profile(agent.name, agent.description or "")
    context_version = await get_active_context_version(agent.id, db)
    changed = False
    for key, value in (("identity", identity), ("soul", soul)):
        existing = await get_versioned_memory(agent.id, key, db, active_version=context_version)
        if existing and existing.value_data.strip():
            continue
        memory_key = f"{key}:v{context_version}" if context_version else key
        await upsert_memory(agent.id, memory_key, value, db, scope=None)
        changed = True
    return changed

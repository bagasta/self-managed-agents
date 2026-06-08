"""Channel-management tools for Arthur builder."""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable
from urllib.parse import quote

from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.tools.builder_identity import (
    agent_belongs_to_owner as _agent_belongs_to_owner,
    latest_owned_agent_for_trial as _latest_owned_agent_for_trial,
)
from app.core.utils.phone_utils import normalize_phone
from app.models.agent import Agent

SettingsProvider = Callable[[], Any]


def _demo_contact_name(agent_name: str) -> str:
    clean_name = str(agent_name or "").strip()
    if clean_name:
        return f"Demo {clean_name}"
    return "Demo Agent"


def build_builder_channel_tools(
    db_factory: async_sessionmaker,
    *,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    device_id: str = "",
    default_target: str = "",
    get_settings: SettingsProvider,
) -> dict[str, Any]:
    _get_settings = get_settings

    @tool
    async def create_wa_dev_trial_link(
        agent_id: str = "",
        phone: str = "",
        force_new_code: bool = False,
        send_contact: bool = True,
    ) -> str:
        """
        Generate kode 6 karakter untuk mencoba agent lewat nomor demo Arthur.

        Gunakan setelah create_agent saat user ingin mencoba agent di WhatsApp tanpa
        punya nomor khusus. Kirimkan hasilnya ke user sebagai opsi:
        "Mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, atau
        dicoba dulu lewat nomor demo Arthur yang sudah siap pakai?"

        Args:
            agent_id: UUID agent yang akan dicoba di nomor shared Arthur. Jika kosong,
                      tool memilih agent non-builder terbaru milik user saat ini.
            phone: Nomor/JID tujuan untuk dikirimi vCard. Kosong = user saat ini.
            force_new_code: True untuk rotate kode lama
            send_contact: True untuk kirim contact card nomor shared Arthur ke user
        """
        agent_uuid: uuid.UUID | None = None
        if agent_id and self_agent_id and str(agent_id) == str(self_agent_id):
            agent_id = ""
        if agent_id:
            try:
                agent_uuid = uuid.UUID(agent_id)
            except ValueError:
                return f"[error] agent_id tidak valid: {agent_id}"

        target = phone or default_target or owner_phone or ""
        settings = _get_settings()

        async with db_factory() as db:
            if agent_uuid:
                result = await db.execute(
                    select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
                )
                agent = result.scalar_one_or_none()
            else:
                agent = await _latest_owned_agent_for_trial(
                    db,
                    owner_phone=owner_phone,
                    self_agent_id=self_agent_id,
                )
            if not agent:
                return (
                    f"[error] Agent dengan ID {agent_id} tidak ditemukan"
                    if agent_id
                    else "[error] Tidak menemukan agent terbaru milik user untuk dibuatkan trial link"
                )
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses ke agent ini"
            resolved_agent_id = str(agent.id)
            resolved_agent_name = agent.name
            contact_name = _demo_contact_name(resolved_agent_name)

            from app.core.domain.wa_dev_trial_service import ensure_wa_dev_trial_code

            code = await ensure_wa_dev_trial_code(db, agent, force_new=force_new_code)
            await db.commit()

        shared_phone = normalize_phone(settings.wa_dev_public_phone)
        wa_status_error = ""
        if not shared_phone:
            try:
                from app.core.infra.wa_client import get_wa_dev_status

                status = await get_wa_dev_status()
                shared_phone = normalize_phone(status.get("phone_number") or "")
            except Exception as exc:
                wa_status_error = str(exc)

        if not shared_phone:
            return json.dumps({
                "success": True,
                "agent_id": resolved_agent_id,
                "agent_name": resolved_agent_name,
                "code": code,
                "contact_sent": False,
                "warning": (
                    "Kode berhasil dibuat, tapi WA_DEV_PUBLIC_PHONE belum dikonfigurasi "
                    "dan nomor wa-dev-service tidak bisa dibaca."
                ),
                "wa_status_error": wa_status_error,
            }, ensure_ascii=False, indent=2)

        prefill = f"Halo Arthur, saya mau coba agent saya. Kode saya: {code}"
        wa_me_url = f"https://wa.me/{shared_phone}?text={quote(prefill)}"

        contact_sent = False
        contact_error = ""
        if send_contact and target:
            if device_id and not device_id.startswith("wadev_"):
                try:
                    from app.core.infra.wa_client import send_wa_contact

                    await send_wa_contact(device_id, target, contact_name, shared_phone)
                    contact_sent = True
                except Exception as exc:
                    contact_error = str(exc)
            elif device_id and device_id.startswith("wadev_"):
                contact_error = (
                    "Arthur sedang berjalan lewat nomor shared wa-dev, jadi vCard tidak dikirim "
                    "agar kontak tidak terlihat dikirim dari nomor trial itu sendiri."
                )
            else:
                contact_error = "Arthur session tidak punya device_id WhatsApp, jadi vCard tidak bisa dikirim dari nomor Arthur."

        return json.dumps({
            "success": True,
            "agent_id": resolved_agent_id,
            "agent_name": resolved_agent_name,
            "code": code,
            "shared_whatsapp_name": contact_name,
            "shared_whatsapp_phone": f"+{shared_phone}",
            "wa_me_url": wa_me_url,
            "contact_sent": contact_sent,
            "contact_error": contact_error,
            "instruction_for_user": (
                f"Simpan kontak {contact_name}, atau buka link wa.me. "
                f"Kirim kode {code} untuk menghubungkan WhatsApp ke agent ini. "
                "Kode bisa dipakai ulang; kirim /stop di WhatsApp kalau ingin disconnect. "
                "Untuk switch agent, minta kode baru dari Arthur lalu kirim kode baru itu."
            ),
        }, ensure_ascii=False, indent=2)

    return {"create_wa_dev_trial_link": create_wa_dev_trial_link}

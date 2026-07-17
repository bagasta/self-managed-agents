"""Channel-management tools for Arthur builder."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import quote

from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.tools.builder_identity import (
    agent_belongs_to_owner as _agent_belongs_to_owner,
    owned_agents_for_trial as _owned_agents_for_trial,
)
from app.core.utils.phone_utils import normalize_phone
from app.models.agent import Agent
from app.models.message import Message

SettingsProvider = Callable[[], Any]
_CONTACT_DEDUPE_TTL_SECONDS = 5 * 60
_contact_send_dedupe: dict[str, float] = {}


def _demo_contact_name(agent_name: str) -> str:
    clean_name = str(agent_name or "").strip()
    if clean_name:
        return f"Demo {clean_name}"
    return "Demo Agent"


def _compact_agent_match_text(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _agent_summary(agent: Agent) -> dict[str, str]:
    return {
        "agent_id": str(getattr(agent, "id", "")),
        "agent_name": str(getattr(agent, "name", "") or ""),
    }


def _match_agent_by_text(agents: list[Agent], text: str | None) -> tuple[Agent | None, list[Agent]]:
    query = _compact_agent_match_text(text)
    if len(query) < 3:
        return None, []

    exact: list[Agent] = []
    contains: list[Agent] = []
    for agent in agents:
        name = str(getattr(agent, "name", "") or "")
        compact = _compact_agent_match_text(name)
        if not compact:
            continue
        if compact == query:
            exact.append(agent)
        elif len(compact) >= 3 and (compact in query or query in compact):
            contains.append(agent)

    matches = exact or contains
    if not matches:
        return None, []
    matches = sorted(matches, key=lambda a: len(_compact_agent_match_text(getattr(a, "name", ""))), reverse=True)
    best_len = len(_compact_agent_match_text(getattr(matches[0], "name", "")))
    best = [a for a in matches if len(_compact_agent_match_text(getattr(a, "name", ""))) == best_len]
    if len(best) == 1:
        return best[0], best
    return None, best


def _contact_dedupe_key(session_id: str | None, target: str, agent_id: str) -> str:
    session_part = str(session_id or "").strip() or normalize_phone(target) or str(target or "").strip()
    return f"{session_part}:{normalize_phone(target)}:{agent_id}"


def _contact_recently_sent(key: str) -> bool:
    now = time.monotonic()
    stale = [k for k, ts in _contact_send_dedupe.items() if now - ts > _CONTACT_DEDUPE_TTL_SECONDS]
    for k in stale:
        _contact_send_dedupe.pop(k, None)
    ts = _contact_send_dedupe.get(key)
    return bool(ts and now - ts <= _CONTACT_DEDUPE_TTL_SECONDS)


def _mark_contact_sent(key: str) -> None:
    _contact_send_dedupe[key] = time.monotonic()


async def _latest_user_message_for_session(db: Any, session_id: str | None) -> str:
    if not session_id:
        return ""
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        return ""
    result = await db.execute(
        select(Message.content)
        .where(Message.session_id == sid, Message.role == "user")
        .order_by(Message.step_index.desc(), Message.timestamp.desc())
        .limit(1)
    )
    return str(result.scalar_one_or_none() or "")


def _agent_id(agent: Agent | None) -> str:
    return str(getattr(agent, "id", "") or "")


def _trial_target_error(
    *,
    error: str,
    latest_user_message: str,
    provided_agent: Agent | None,
    latest_agent: Agent | None,
    owned_agents: list[Agent],
) -> str:
    return json.dumps({
        "success": False,
        "error": error,
        "requested_message": latest_user_message,
        "provided_agent": _agent_summary(provided_agent) if provided_agent else None,
        "latest_agent": _agent_summary(latest_agent) if latest_agent else None,
        "available_agents": [_agent_summary(a) for a in owned_agents],
        "instruction_for_assistant": (
            "Jangan kirim vCard/kode untuk agent lain dari history lama. "
            "Jika user tidak menyebut nama agent dan ada agent terbaru dari konteks pembuatan terakhir, "
            "pakai latest_agent. Kalau ragu, minta user pilih nama agent."
        ),
    }, ensure_ascii=False, indent=2)


def build_builder_channel_tools(
    db_factory: async_sessionmaker,
    *,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    device_id: str = "",
    default_target: str = "",
    session_id: str | None = None,
    get_settings: SettingsProvider,
) -> dict[str, Any]:
    _get_settings = get_settings

    @tool
    async def create_wa_dev_trial_link(
        agent_id: str = "",
        agent_name: str = "",
        phone: str = "",
        force_new_code: bool = False,
        send_contact: bool = True,
    ) -> str:
        """
        Generate kode 6 karakter untuk mencoba agent lewat nomor demo Arthur.

        Gunakan setelah create_agent saat user ingin mencoba agent di WhatsApp tanpa
        setup nomor sendiri. Arahkan uji coba nomor demo terlebih dahulu; jangan
        menawarkan nomor khusus sebelum user mencoba demo dan menyatakan cocok,
        kecuali user sendiri yang meminta pemasangan nomor.

            Args:
            agent_id: UUID agent yang akan dicoba di nomor shared Arthur. Jika kosong,
                      isi agent_name saat user menyebut nama agent.
            agent_name: Nama agent target, misalnya "Mas Brew". Wajib dipakai jika
                        user menyebut agent tertentu tapi agent_id belum diketahui.
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
            owned_agents_for_context: list[Agent] = []
            latest_user_message = ""
            if agent_uuid:
                result = await db.execute(
                    select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
                )
                agent = result.scalar_one_or_none()
            else:
                owned_agents = await _owned_agents_for_trial(
                    db,
                    owner_phone=owner_phone,
                    self_agent_id=self_agent_id,
                )
                owned_agents_for_context = owned_agents
                latest_user_message = await _latest_user_message_for_session(db, session_id)
                match_source = agent_name or latest_user_message
                agent, ambiguous = _match_agent_by_text(owned_agents, match_source)
                if not agent and agent_name:
                    return json.dumps({
                        "success": False,
                        "error": "agent_name_not_found_or_ambiguous",
                        "requested_agent_name": agent_name,
                        "available_agents": [_agent_summary(a) for a in owned_agents],
                        "instruction_for_assistant": (
                            "Jangan kirim nomor demo untuk agent lain. Minta user pilih agent yang benar "
                            "atau panggil lagi dengan agent_id/agent_name yang tepat."
                        ),
                    }, ensure_ascii=False, indent=2)
                if not agent and ambiguous:
                    return json.dumps({
                        "success": False,
                        "error": "agent_name_ambiguous",
                        "requested_agent_name": agent_name or latest_user_message,
                        "candidate_agents": [_agent_summary(a) for a in ambiguous],
                    }, ensure_ascii=False, indent=2)
                if not agent and len(owned_agents) == 1:
                    agent = owned_agents[0]
                if not agent and len(owned_agents) > 1:
                    return json.dumps({
                        "success": False,
                        "error": "agent_target_required",
                        "available_agents": [_agent_summary(a) for a in owned_agents],
                        "instruction_for_assistant": (
                            "User punya beberapa agent. Jangan fallback ke agent terbaru. "
                            "Pilih agent dari nama yang disebut user atau minta user menyebut nama agent."
                        ),
                    }, ensure_ascii=False, indent=2)
            if not agent:
                return (
                    f"[error] Agent dengan ID {agent_id} tidak ditemukan"
                    if agent_id
                    else "[error] Tidak menemukan agent terbaru milik user untuk dibuatkan trial link"
                )
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses ke agent ini"
            if owner_phone and session_id:
                owned_agents = owned_agents_for_context or await _owned_agents_for_trial(
                    db,
                    owner_phone=owner_phone,
                    self_agent_id=self_agent_id,
                )
                latest_user_message = latest_user_message or await _latest_user_message_for_session(db, session_id)
                mentioned_agent, _ = _match_agent_by_text(owned_agents, latest_user_message)
                if mentioned_agent and str(getattr(mentioned_agent, "id", "")) != str(getattr(agent, "id", "")):
                    return json.dumps({
                        "success": False,
                        "error": "agent_target_conflict",
                        "requested_message": latest_user_message,
                        "provided_agent": _agent_summary(agent),
                        "detected_agent": _agent_summary(mentioned_agent),
                        "instruction_for_assistant": (
                            "Jangan kirim nomor demo untuk provided_agent. User menyebut detected_agent; "
                            "panggil create_wa_dev_trial_link lagi dengan agent_id/agent_name detected_agent."
                        ),
                    }, ensure_ascii=False, indent=2)
                if len(owned_agents) > 1 and not mentioned_agent:
                    latest_agent = owned_agents[0] if owned_agents else None
                    if latest_agent and _agent_id(agent) != _agent_id(latest_agent):
                        return _trial_target_error(
                            error="agent_target_ambiguous_for_current_request",
                            latest_user_message=latest_user_message,
                            provided_agent=agent,
                            latest_agent=latest_agent,
                            owned_agents=owned_agents,
                        )
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
        contact_already_sent = False
        link_message_sent = False
        link_message_error = ""
        if send_contact and target:
            dedupe_key = _contact_dedupe_key(session_id, target, resolved_agent_id)
            if _contact_recently_sent(dedupe_key):
                contact_already_sent = True
                contact_error = "vCard demo untuk agent ini sudah dikirim beberapa menit terakhir; tidak dikirim ulang."
            elif device_id and not device_id.startswith("wadev_"):
                try:
                    from app.core.infra.wa_client import send_wa_contact, send_wa_message

                    await send_wa_message(
                        device_id,
                        target,
                        (
                            f"{resolved_agent_name} siap dicoba lewat nomor demo Arthur.\n"
                            f"Buka: {wa_me_url}\n"
                            f"Kode: {code}\n"
                            "Setelah pesan ini saya kirim kontak nomor demonya."
                        ),
                    )
                    link_message_sent = True
                    await send_wa_contact(device_id, target, contact_name, shared_phone)
                    _mark_contact_sent(dedupe_key)
                    contact_sent = True
                except Exception as exc:
                    if link_message_sent:
                        contact_error = str(exc)
                    else:
                        link_message_error = str(exc)
                        contact_error = "vCard tidak dikirim karena pesan link/kode belum berhasil dikirim lebih dulu."
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
            "link_message_sent": link_message_sent,
            "link_message_error": link_message_error,
            "contact_sent": contact_sent,
            "contact_already_sent": contact_already_sent,
            "contact_error": contact_error,
            "instruction_for_user": (
                f"Buka link wa.me dan gunakan kode {code} terlebih dahulu. "
                f"Setelah link/kode disampaikan, simpan kontak {contact_name}. "
                "Kode bisa dipakai ulang; kirim /stop di WhatsApp kalau ingin disconnect. "
                "Untuk switch agent, minta kode baru dari Arthur lalu kirim kode baru itu."
            ),
        }, ensure_ascii=False, indent=2)

    return {"create_wa_dev_trial_link": create_wa_dev_trial_link}

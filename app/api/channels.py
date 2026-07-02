"""
Incoming channel webhook — endpoint untuk menerima pesan masuk dari channel eksternal
(WhatsApp webhook, Telegram webhook, dll).

Endpoint:
  POST /v1/channels/incoming/{session_id}   — generic channel webhook
  POST /v1/channels/wa/incoming             — WhatsApp-specific webhook dari Go wa-service

Desain operator session (WhatsApp):
  - Setiap pengirim (termasuk operator) memiliki session SENDIRI berdasarkan nomor pengirim.
  - Jika from_phone == agent.escalation_config.operator_phone:
      → Operator punya session operator (lookup by operator_phone).
      → agent_runner dijalankan dengan escalation_user_jid = JID user yang sedang dieskalasi
        (diambil dari session dengan escalation_active=True).
      → Sistem prompt memberi tahu agent bahwa ia sedang di sesi OPERATOR dan harus
        menggunakan tool reply_to_user(...) untuk mengirim ke user.
  - Jika escalation_active == True dan bukan dari operator:
      → Forward pesan user ke operator (via channel_service) sebagai notifikasi.
      → Jalankan agent dengan pesan [USER_IN_ESCALATION] untuk konteks.
  - Selain itu → proses normal per-user.

Single-worker constraint:
  Event bus (SSE) masih in-memory dengan Redis fallback (jika REDIS_URL di-set).
  Jangan jalankan lebih dari 1 uvicorn worker sampai Redis terkonfigurasi penuh.
  Lihat docs/production-plan/01-critical-blockers.md#1.2 untuk detail.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import re
import time
from typing import Annotated
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.engine.session_lock import session_run_lock
from app.core.domain.agent_quota_service import check_agent_quota, record_agent_token_usage
from app.core.infra.channel_service import send_message
from app.core.utils.input_sanitizer import sanitize_user_input
from app.core.utils.phone_utils import normalize_phone
from app.core.utils.wa_identity import extract_wa_lid, resolve_incoming_wa_phone
from app.core.utils.text_utils import markdown_to_wa
from app.core.engine.wa_reply_delivery import should_skip_whatsapp_final_reply
from app.core.infra.wa_client import resolve_wa_phones, send_wa_message
from app.database import get_db
from app.models.agent import Agent
from app.models.message import Message
from app.models.session import Session

# Helper functions untuk wa_incoming — dipecah agar bisa di-test secara independen
from app.api.wa_helpers import (
    check_wa_spam_window,
    reset_wa_spam_window,
    extract_messages_to_user,
    find_agent_by_device,
    find_escalation_context,
    find_or_create_wa_session,
    find_session_by_operator_active_route,
    find_session_by_quoted_case,
    find_session_by_quoted_message_id,
    get_wa_lookup_user_id,
    is_operator_message,
    remember_operator_escalation_route,
    process_wa_media,
    is_duplicate_message,
)

_settings = get_settings()
_DEVELOPER_PHONE: str = _settings.developer_phone
_GENERIC_ERROR_MSG = "Maaf, terjadi gangguan sementara. Silakan coba lagi dalam beberapa saat."
_WA_DEV_DISCONNECT_COMMANDS = {"/stop", "berhenti", "/disconnect", "stop"}
_LOCAL_TZ = ZoneInfo("Asia/Jakarta")

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/channels", tags=["channels"])


def _missing_media_payload_reply(media_type: str | None, media_filename: str | None) -> str:
    safe_name = sanitize_user_input(media_filename or "").strip()
    label = safe_name or sanitize_user_input(media_type or "lampiran").strip() or "lampiran"
    return (
        f"Saya menerima info lampiran {label}, tapi file-nya gagal diambil dari WhatsApp "
        "jadi belum bisa saya proses. Tolong kirim ulang file itu sekali lagi sebagai dokumen biasa."
    )


def _wa_customer_target(session: Session) -> str:
    cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    return cfg.get("user_phone") or session.external_user_id or ""


def _wa_real_customer_phone(session: Session) -> str:
    cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    phone = cfg.get("phone_number") or session.external_user_id or ""
    return normalize_phone(phone) or phone or "(tidak diketahui)"


def _is_wa_owner_sender(agent: Agent, *sender_ids: str | None) -> bool:
    owner_id = normalize_phone(getattr(agent, "owner_external_id", "") or "")
    if not owner_id:
        return False
    for sender_id in sender_ids:
        normalized = normalize_phone(str(sender_id or "").strip())
        if normalized and normalized == owner_id:
            return True
    return False


def _label_owner_wa_message(
    *,
    message: str,
    from_phone: str,
    sender_name: str | None,
    is_operator_turn: bool,
) -> str:
    role_line = "Role: OWNER/SUPERADMIN"
    name_line = f"Name WA: {sender_name}\n" if sender_name else ""
    if is_operator_turn:
        return (
            "<OPERATOR>\n"
            f"{role_line}\n"
            f"{name_line}"
            f"No Telepon/WA/Id: {from_phone or '(tidak diketahui)'}\n"
            f"Pesan: {message}"
        )
    return (
        "<OWNER>\n"
        f"{role_line}\n"
        f"{name_line}"
        f"No Telepon/WA/Id: {from_phone or '(tidak diketahui)'}\n"
        f"Pesan: {message}"
    )


def _is_wa_dev_device(device_id: str | None) -> bool:
    text = str(device_id or "")
    return text.startswith("wadev_") or text in {"wa-dev-service", "wa_dev_service"} or text.startswith("wa-dev-")


def _wa_dev_virtual_device_id(agent_id: uuid.UUID | str) -> str:
    return f"wadev_{agent_id}"


def _is_wa_dev_disconnect_command(message: str | None) -> bool:
    return str(message or "").strip().lower() in _WA_DEV_DISCONNECT_COMMANDS


async def _load_agent_by_id(agent_id: uuid.UUID | str | None, db: AsyncSession) -> Agent | None:
    if not agent_id:
        return None
    try:
        parsed = uuid.UUID(str(agent_id))
    except ValueError:
        return None
    result = await db.execute(
        select(Agent).where(
            Agent.id == parsed,
            Agent.is_deleted.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def _resolve_wa_incoming_agent(body: object, db: AsyncSession, log: structlog.BoundLogger) -> Agent | None:
    """Resolve the target agent for a WhatsApp webhook.

    wa-dev-service uses one shared WhatsApp number for many agents, so its
    gateway should pass an explicit agent_id or virtual device id. Falling back
    to a physical shared device id is unsafe because many users can switch
    demo-agent bindings concurrently.
    """
    explicit_agent_id = getattr(body, "agent_id", None)
    if explicit_agent_id:
        agent = await _load_agent_by_id(explicit_agent_id, db)
        if agent:
            return agent
        log.warning("wa_incoming.explicit_agent_not_found", agent_id=str(explicit_agent_id))
        return None

    trial_code = str(getattr(body, "trial_code", "") or "").strip()
    message = str(getattr(body, "message", "") or "").strip()
    device_id = str(getattr(body, "device_id", "") or "")
    if _is_wa_dev_device(device_id):
        from app.core.domain.wa_dev_trial_service import (
            extract_wa_dev_trial_code,
            find_agent_by_wa_dev_trial_code,
            normalize_wa_dev_trial_code,
        )

        code = normalize_wa_dev_trial_code(trial_code)
        if len(code) != 6:
            code = extract_wa_dev_trial_code(message)
        if len(code) == 6:
            agent = await find_agent_by_wa_dev_trial_code(db, code)
            if agent:
                return agent

    if _is_wa_dev_device(device_id) and not device_id.startswith("wadev_"):
        log.warning(
            "wa_incoming.wa_dev_shared_route_missing_explicit_agent",
            device_id=device_id,
            has_explicit_agent=bool(explicit_agent_id),
            has_trial_code=bool(trial_code),
        )
        return None

    agent = await find_agent_by_device(device_id, db)
    if _is_wa_dev_device(device_id) and not agent:
        log.warning(
            "wa_incoming.wa_dev_route_missing_agent",
            device_id=device_id,
            has_explicit_agent=bool(explicit_agent_id),
            has_trial_code=bool(trial_code),
        )
    return agent


def _wa_dev_session_lookup_candidates(*values: str | None) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        normalized = normalize_phone(text)
        for candidate in (normalized, text if text.endswith("@g.us") else ""):
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


async def _disconnect_wa_dev_sessions(
    *,
    agent_id: uuid.UUID,
    phone: str | None,
    from_phone: str | None,
    chat_id: str | None,
    db: AsyncSession,
) -> int:
    candidates = _wa_dev_session_lookup_candidates(phone, from_phone, chat_id)
    if not candidates:
        return 0

    result = await db.execute(
        select(Session).where(
            Session.agent_id == agent_id,
            Session.channel_type == "whatsapp",
            Session.external_user_id.in_(candidates),
        )
    )
    sessions = list(result.scalars().all())
    if not sessions:
        return 0

    from app.core.engine.session_lock import cancel_active_run

    for session in sessions:
        await cancel_active_run(session.id)
        meta = dict(session.metadata_ or {})
        meta["wa_dev_disconnected_at"] = int(time.time())
        session.metadata_ = meta
        db.add(session)
    await db.commit()
    return len(sessions)


def _looks_like_operator_media_request(text: str | None) -> bool:
    clean = (text or "").lower()
    return any(word in clean for word in ("gambar", "foto", "image", "dokumen", "document", "lampiran"))


def _build_customer_media_note(media_type: str, media_filename: str | None = None) -> str:
    if media_type in {"image", "sticker"}:
        return "Berikut gambar yang Anda minta, silakan dicek."
    if media_type == "document":
        name = sanitize_user_input(media_filename or "").strip()
        if name:
            return f"Berikut dokumen yang Anda minta, silakan dicek."
        return "Berikut dokumen yang Anda minta, silakan dicek."
    return "Berikut lampiran yang Anda minta, silakan dicek."


_OPERATOR_SEND_CONFIRM_WORDS = {
    "kirim", "ok", "oke", "ya", "iya", "yes", "ok kirim", "oke kirim", "ya kirim", "iya kirim", "yes kirim",
    "langsung kirim", "lanjut kirim",
}
_PENDING_OPERATOR_SEND_TTL_SECONDS = 60 * 60
_PAYMENT_APPROVAL_SUBJECTS = ("bayar", "pembayaran", "payment", "transfer", "paid")
_PAYMENT_APPROVAL_SIGNALS = (
    "masuk",
    "valid",
    "approve",
    "approved",
    "diterima",
    "sudah",
    "oke",
    "ok",
    "confirm",
    "confirmed",
)
_PAYMENT_REJECTION_SIGNALS = ("belum", "belom", "tidak", "gak", "ga", "nggak", "invalid", "ditolak", "reject", "gagal")


def _is_operator_send_confirmation(text: str | None) -> bool:
    clean = sanitize_user_input(text or "").strip().lower()
    if clean in _OPERATOR_SEND_CONFIRM_WORDS:
        return True
    clean = re.sub(r"[^a-z0-9\s]+", " ", clean)
    clean = " ".join(clean.split())
    return clean in _OPERATOR_SEND_CONFIRM_WORDS


def _is_operator_payment_approval(text: str | None) -> bool:
    clean = sanitize_user_input(text or "").strip().lower()
    clean = re.sub(r"[^a-z0-9\s]+", " ", clean)
    clean = " ".join(clean.split())
    if not clean:
        return False
    words = set(clean.split())
    if words.intersection(_PAYMENT_REJECTION_SIGNALS):
        return False
    return (
        any(subject in clean for subject in _PAYMENT_APPROVAL_SUBJECTS)
        and any(signal in clean for signal in _PAYMENT_APPROVAL_SIGNALS)
    )


def _append_quoted_reply_context(message: str, quoted_text: str | None) -> str:
    quote = sanitize_user_input(quoted_text or "").strip()
    if not quote:
        return message
    if len(quote) > 1200:
        quote = quote[:1200] + "..."
    body = message.strip()
    return (
        "[WHATSAPP_REPLY_CONTEXT]\n"
        "User sedang membalas/reply pesan WhatsApp berikut:\n"
        f"{quote}\n"
        "[/WHATSAPP_REPLY_CONTEXT]\n\n"
        f"{body}"
    ).strip()


def _operator_pending_expires_at() -> int:
    return int(time.time()) + _PENDING_OPERATOR_SEND_TTL_SECONDS


def _operator_pending_is_expired(pending: dict) -> bool:
    expires_at = pending.get("expires_at")
    return isinstance(expires_at, (int, float)) and expires_at < time.time()


def _operator_session_has_pending_confirmation(operator_session: Session | None) -> bool:
    if not operator_session:
        return False
    meta = dict(operator_session.metadata_ or {})
    for key in ("pending_operator_media", "pending_operator_text_reply"):
        pending = meta.get(key)
        if isinstance(pending, dict) and not _operator_pending_is_expired(pending):
            return True
    return False


def _operator_pending_text_revision_context(operator_session: Session | None, operator_message: str | None) -> str:
    if not operator_session:
        return ""
    text = sanitize_user_input(operator_message or "").strip()
    if not text or _is_operator_send_confirmation(text) or _is_operator_escalation_recap_request(text):
        return ""
    pending = dict(operator_session.metadata_ or {}).get("pending_operator_text_reply")
    if not isinstance(pending, dict) or _operator_pending_is_expired(pending):
        return ""
    draft = sanitize_user_input(str(pending.get("message") or "")).strip()
    if not draft:
        return ""
    case_id = str(pending.get("case_id") or "-")
    target = str(pending.get("target") or "")
    return (
        "[OPERATOR_DRAFT_REVISION]\n"
        "Operator sedang merevisi draft balasan customer yang MASIH pending, bukan bertanya topik lain.\n"
        f"Case ID: {case_id}\n"
        f"Target customer: {target or '-'}\n"
        "PENTING: Draft ini adalah pesan yang akan DIKIRIM dari agent/operator KEPADA customer, "
        "bukan pesan dari customer kepada agent. Revisi harus tetap memakai sudut pandang agent/operator "
        "yang berbicara kepada customer (misalnya: 'Berikut nomor resinya...', bukan 'Terima kasih telah mengirimkan...').\n"
        "Draft pending saat ini:\n"
        "----\n"
        f"{draft}\n"
        "----\n"
        f"Instruksi revisi operator: {text}\n"
        "Tugas wajib: revisi HANYA draft pending di atas sesuai instruksi operator. "
        "Jangan menjawab rekap eskalasi, jangan memakai topik/history lama, dan jangan kirim ke customer dulu. "
        "Tampilkan draft revisi baru lalu minta operator ketik 'kirim' jika sudah sesuai.\n"
        "[/OPERATOR_DRAFT_REVISION]"
    )


def _is_operator_escalation_recap_request(message: str | None) -> bool:
    text = sanitize_user_input(message or "").lower()
    if "eskalasi" not in text:
        return False
    markers = (
        "berapa",
        "rekap",
        "rangkuman",
        "ringkasan",
        "daftar",
        "list",
        "laporan",
        "hari ini",
        "masuk",
        "tercatat",
        "kasus",
        "pesan",
    )
    return any(marker in text for marker in markers)


def _extract_escalation_field(content: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}\s*:\s*(.+)$", content, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _format_operator_escalation_recap(rows: list[tuple[Message, Session]]) -> str:
    lines = [
        "### Rekap eskalasi hari ini",
        f"Total eskalasi tercatat hari ini: {len(rows)}.",
    ]
    if not rows:
        lines.append("Tidak ada pesan eskalasi yang tercatat hari ini.")
        return "\n".join(lines)

    lines.append("Eskalasi terbaru:")
    for message, session in rows[:10]:
        content = str(message.content or "")
        case_id = _extract_escalation_field(content, "ID Kasus") or "-"
        customer = (
            _extract_escalation_field(content, "Nomor customer/user")
            or _extract_escalation_field(content, "Nomor customer")
            or (session.external_user_id or "-")
        )
        name = _extract_escalation_field(content, "Nama customer")
        reason = _extract_escalation_field(content, "Alasan eskalasi")
        payload = _extract_escalation_field(content, "Pesan") or _extract_escalation_field(content, "Pesan terakhir")
        summary_parts = [part for part in (reason, payload) if part]
        summary = " | ".join(summary_parts) or content.replace("\n", " ")[:220]
        local_time = message.timestamp.astimezone(_LOCAL_TZ) if getattr(message, "timestamp", None) else None
        time_label = local_time.strftime("%H:%M") if local_time else "-"
        customer_label = f"{name} ({customer})" if name else customer
        lines.append(f"- {time_label} WIB | {case_id} | {customer_label} | {summary[:300]}")
    return "\n".join(lines)


async def _build_operator_escalation_recap_context(
    *,
    agent: Agent,
    db: AsyncSession,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(_LOCAL_TZ)
    local_start = current.astimezone(_LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(Message, Session)
        .join(Session, Message.session_id == Session.id)
        .where(
            Session.agent_id == agent.id,
            Message.role == "escalation",
            Message.timestamp >= local_start,
        )
        .order_by(Message.timestamp.desc())
    )
    rows = list(result.all())
    return _format_operator_escalation_recap(rows)


async def _find_existing_operator_session(
    *,
    agent: Agent,
    from_phone: str,
    db: AsyncSession,
) -> Session | None:
    escalation_cfg: dict = agent.escalation_config or {}
    operator_lookup = escalation_cfg.get("operator_phone") or from_phone
    normalized_lookup = normalize_phone(operator_lookup)
    if not normalized_lookup:
        return None
    result = await db.execute(
        select(Session).where(
            Session.agent_id == agent.id,
            Session.channel_type == "whatsapp",
            Session.external_user_id == normalized_lookup,
        )
    )
    return result.scalars().first()


async def _has_explicit_operator_escalation_reply(
    *,
    agent: Agent,
    db: AsyncSession,
    quoted_text: str | None,
    quoted_stanza_id: str | None,
) -> bool:
    target_session, _case_id = await find_session_by_quoted_message_id(agent, db, quoted_stanza_id)
    if target_session:
        return True
    target_session, _case_id = await find_session_by_quoted_case(agent, db, quoted_text)
    return target_session is not None


async def _should_treat_as_operator_turn(
    *,
    agent: Agent,
    db: AsyncSession,
    from_phone: str,
    reply_target: str | None,
    message: str,
    media_type: str | None,
    quoted_text: str | None,
    quoted_stanza_id: str | None,
) -> bool:
    if not is_operator_message(from_phone, reply_target, agent):
        return False

    if await _has_explicit_operator_escalation_reply(
        agent=agent,
        db=db,
        quoted_text=quoted_text,
        quoted_stanza_id=quoted_stanza_id,
    ):
        return True

    # Once a quoted escalation reply has created a pending draft, the operator's
    # next text can be either a send confirmation ("kirim") or a revision request
    # ("buat lebih sopan", "ganti jadi ..."). Keep that conversation in operator
    # mode so the revised draft can overwrite the previous pending message.
    if not media_type:
        operator_session = await _find_existing_operator_session(
            agent=agent,
            from_phone=from_phone,
            db=db,
        )
        if _operator_session_has_pending_confirmation(operator_session):
            return True

    if not media_type and _is_operator_escalation_recap_request(message):
        return True

    return False


async def _forget_pending_operator_item(
    operator_session: Session,
    key: str,
    db: AsyncSession,
) -> None:
    sess_meta = dict(operator_session.metadata_ or {})
    if key not in sess_meta:
        return
    sess_meta.pop(key, None)
    operator_session.metadata_ = sess_meta
    db.add(operator_session)
    await db.commit()


async def _reply_no_pending_operator_confirmation(
    *,
    device_id: str,
    operator_reply_target: str,
) -> dict:
    reply = (
        "Belum ada draft atau lampiran yang menunggu dikirim. "
        "Reply pesan eskalasi customer, tulis jawaban/lampirkan file, lalu ketik 'kirim' setelah draft-nya saya tampilkan."
    )
    await send_wa_message(device_id, operator_reply_target, reply)
    return {"status": "ok", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}


async def _reply_agent_quota_blocked(
    *,
    device_id: str,
    reply_target: str,
    message: str,
    reason: str,
    log: structlog.BoundLogger,
) -> dict:
    reply = message or "Maaf, agent ini sedang tidak bisa dipakai karena kuota atau subscription sudah habis."
    try:
        await send_wa_message(device_id, reply_target, reply)
    except Exception as exc:
        log.warning("wa_incoming.quota_block_reply_failed", error=str(exc), reason=reason)
    return {
        "status": "quota_exhausted",
        "reason": reason,
        "reply": reply,
        "run_id": "",
        "steps": [],
        "messages_to_user": [],
    }


def _extract_media_draft_from_history(rows: list[Message]) -> str:
    for row in reversed(rows):
        if row.role not in {"agent", "assistant"}:
            continue
        content = str(row.content or "").strip()
        if not content or content.lower().startswith("terkirim"):
            continue
        quoted = re.findall(r'"([^"\n]{6,1000})"|“([^”\n]{6,1000})”', content)
        candidates = [a or b for a, b in quoted if (a or b)]
        if candidates:
            return candidates[-1].strip()
        marker_match = re.search(
            r"(?:draft(?:\s+untuk\s+[^:]+)?|pesan(?:\s+sopan)?(?:\s+untuk\s+[^:]+)?|isi pesan)\s*:\s*(.+)",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if marker_match:
            candidate = marker_match.group(1).strip()
            candidate = re.split(r"\b(?:ketik|balas|konfirmasi)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip(" \n\t\"'")
            if len(candidate) >= 6:
                return candidate
    return ""


def _clean_operator_draft_candidate(candidate: str) -> str:
    candidate = re.split(
        r"\b(?:sudah\s+ok|ketik|balas|konfirmasi)\b",
        candidate,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    lines = []
    for line in candidate.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"[-_=*`]{3,}", stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip(" \n\t\"'")


def _extract_operator_text_draft(content: str | None) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("terkirim") or "sudah saya kirim" in lowered:
        return ""

    fenced = re.findall(r"(?:^|\n)[-_]{3,}\s*\n(.+?)\n[-_]{3,}", text, flags=re.DOTALL)
    if fenced:
        candidate = _clean_operator_draft_candidate(fenced[-1])
        if len(candidate) >= 6:
            return candidate

    quoted = re.findall(r'"([^"\n]{6,1000})"|“([^”\n]{6,1000})”', text)
    candidates = [a or b for a, b in quoted if (a or b)]
    if candidates:
        candidate = _clean_operator_draft_candidate(candidates[-1])
        if len(candidate) >= 6:
            return candidate

    marker_match = re.search(
        r"(?:draft(?:\s+pesan)?(?:\s+untuk\s+[^:]+)?|pesan(?:\s+sopan)?(?:\s+untuk\s+[^:]+)?|isi pesan)\s*:\s*(.+)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if marker_match:
        candidate = _clean_operator_draft_candidate(marker_match.group(1))
        if len(candidate) >= 6:
            return candidate
    return ""


async def _remember_pending_operator_text_reply(
    *,
    operator_session: Session,
    target_session: Session,
    case_id: str | None,
    draft_message: str,
    db: AsyncSession,
) -> None:
    target = _wa_customer_target(target_session)
    if not target or not draft_message.strip():
        return
    await remember_operator_escalation_route(operator_session, target_session, case_id, db)
    sess_meta = dict(operator_session.metadata_ or {})
    sess_meta["pending_operator_text_reply"] = {
        "target_session_id": str(target_session.id),
        "target": target,
        "case_id": case_id,
        "message": draft_message.strip(),
        "created_at": int(time.time()),
        "expires_at": _operator_pending_expires_at(),
    }
    operator_session.metadata_ = sess_meta
    db.add(operator_session)
    await db.commit()


async def _maybe_stage_operator_text_draft(
    *,
    agent: Agent,
    operator_session: Session,
    quoted_text: str | None,
    quoted_stanza_id: str | None,
    operator_message: str,
    device_id: str,
    operator_reply_target: str,
    db: AsyncSession,
    log: structlog.BoundLogger,
) -> dict | None:
    """Deterministically stage an operator's quoted escalation reply as a draft.

    When an operator REPLIES TO (quotes) an escalation notification and types a
    plain message, treat that text as the reply meant for the customer WITHOUT
    relying on the LLM to produce a draft (gpt-4.1-mini often just acknowledges
    instead of forwarding). Stage it as a pending draft and ask the operator to
    confirm with 'kirim'. Returns a response dict, or None when this is not a
    quoted escalation reply so the normal agent turn can handle it.
    """
    draft_message = sanitize_user_input(operator_message or "").strip()
    if not draft_message:
        return None
    # The media draft-first flow re-enters the operator branch with an internal
    # prompt as the message; let the agent compose the media caption instead of
    # staging that prompt as a text draft.
    if "[OPERATOR_MEDIA_PENDING]" in draft_message:
        return None

    target_session, case_id = await find_session_by_quoted_message_id(agent, db, quoted_stanza_id)
    if target_session is None:
        target_session, case_id = await find_session_by_quoted_case(agent, db, quoted_text)
    if target_session is None:
        return None

    # Case already answered: a reply was sent for this exact case. Don't re-open
    # a draft when the operator replies to the SAME old escalation notification.
    tmeta = target_session.metadata_ if isinstance(target_session.metadata_, dict) else {}
    if case_id and tmeta.get("escalation_replied_case") == case_id:
        reply = (
            "Balasan untuk kasus ini sudah dikirim ke customer sebelumnya, jadi kasusnya sudah saya tutup. "
            "Kalau ada update baru, reply chat terakhir dari customer-nya ya — bukan notifikasi eskalasi yang lama."
        )
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "ok", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    await _remember_pending_operator_text_reply(
        operator_session=operator_session,
        target_session=target_session,
        case_id=case_id,
        draft_message=draft_message,
        db=db,
    )

    cc = target_session.channel_config if isinstance(target_session.channel_config, dict) else {}
    customer_name = str(cc.get("sender_name") or "").strip()
    reply = (
        "Draft balasan untuk customer"
        + (f" ({customer_name})" if customer_name else "")
        + ":\n----\n"
        + draft_message
        + "\n----\nKetik *kirim* untuk teruskan ke customer, atau ketik ulang pesannya kalau mau revisi."
    )
    await send_wa_message(device_id, operator_reply_target, reply)
    log.info(
        "wa_incoming.operator_text_draft_staged",
        case_id=case_id,
        target_session_id=str(target_session.id),
    )
    return {"status": "ok", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}


async def _stop_customer_typing(device_id: str, session: Session, log: structlog.BoundLogger) -> None:
    """Kill the WhatsApp typing keep-alive for a customer chat.

    The Go wa-service refreshes the "composing" presence on a loop until a
    Paused presence is sent. When AI is disabled (spam / ai_disabled) the run
    path is skipped, so without this the indicator would keep showing forever.
    """
    cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    target = cfg.get("user_phone") or ""
    if not (device_id and target):
        return
    try:
        from app.core.infra.wa_client import stop_wa_typing

        await stop_wa_typing(device_id, target)
    except Exception as exc:
        warning = getattr(log, "warning", None)
        if callable(warning):
            warning("wa_incoming.stop_typing_failed", error=str(exc)[:200])


async def _notify_operator_spam_autostop(
    *,
    agent: Agent,
    session: Session,
    db: AsyncSession,
    device_id: str,
    count: int,
    window_seconds: int,
    last_message: str,
    log: structlog.BoundLogger,
) -> str | None:
    """Disable a spammy customer session and notify the human operator once."""
    import time

    escalation_cfg = agent.escalation_config if isinstance(agent.escalation_config, dict) else {}
    operator_channel = escalation_cfg.get("channel_type", "whatsapp")
    operator_phone = escalation_cfg.get("operator_phone", "")

    case_id = f"esc_{int(time.time())}_{str(session.id)[:6]}"
    customer_phone = _wa_real_customer_phone(session)
    clean_last_message = sanitize_user_input(last_message or "").strip() or "(kosong/media)"
    notif_text = (
        "ESKALASI PESAN DARI CUSTOMER\n"
        f"ID Kasus: {case_id}\n"
        f"Nomor customer/user: {customer_phone}\n"
        f"Alasan eskalasi: spam terdeteksi ({count} pesan dalam {window_seconds} detik)\n"
        f"Pesan terakhir: {clean_last_message[:1000]}\n\n"
        "Sistem otomatis mematikan balasan AI untuk customer ini agar backend tidak terus memproses spam.\n"
        "Cara aktifkan lagi:\n"
        "Reply pesan ini di WhatsApp dengan /aktif."
    )

    meta = dict(session.metadata_ or {})
    meta["spam_auto_disabled"] = True
    meta["spam_case_id"] = case_id
    meta["escalation_case_id"] = case_id
    meta["spam_count"] = count
    meta["spam_window_seconds"] = window_seconds
    session.metadata_ = meta
    session.ai_disabled = True
    db.add(Message(
        session_id=session.id,
        role="escalation",
        content=notif_text,
        step_index=9000,
    ))
    db.add(session)
    await db.commit()

    # Kill the typing indicator so it doesn't keep showing on a disabled chat.
    await _stop_customer_typing(device_id, session, log)

    if not operator_phone:
        log.warning("wa_incoming.spam_no_operator_config", case_id=case_id)
        return case_id

    operator_config = {
        **escalation_cfg,
        "user_phone": operator_phone,
        "device_id": device_id,
    }
    try:
        send_result = await send_message(
            channel_type=operator_channel,
            channel_config=operator_config,
            text=notif_text,
        )
        if isinstance(send_result, dict) and send_result.get("message_id"):
            meta = dict(session.metadata_ or {})
            message_id = str(send_result["message_id"])
            meta["escalation_message_id"] = message_id
            meta["escalation_message_ids"] = [message_id]
            session.metadata_ = meta
            db.add(session)
            await db.commit()
    except Exception as exc:
        log.warning("wa_incoming.spam_operator_notify_failed", error=str(exc))
    return case_id


async def _handle_operator_activate_command(
    *,
    agent: Agent,
    quoted_text: str | None,
    device_id: str,
    operator_reply_target: str,
    db: AsyncSession,
    log: structlog.BoundLogger,
    quoted_stanza_id: str | None = None,
) -> dict:
    target_session, case_id = await find_session_by_quoted_message_id(agent, db, quoted_stanza_id)
    if not target_session:
        target_session, case_id = await find_session_by_quoted_case(agent, db, quoted_text)
    if not target_session:
        reply = (
            "Reply pesan eskalasi/spam yang benar lalu kirim /aktif. "
            "Saya perlu ID Kasus dari pesan yang di-reply agar tidak salah customer."
        )
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "ok", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    meta = dict(target_session.metadata_ or {})
    meta["spam_auto_disabled"] = False
    meta.pop("spam_count", None)
    meta.pop("spam_window_seconds", None)
    target_session.metadata_ = meta
    target_session.ai_disabled = False
    db.add(target_session)
    await db.commit()

    # Clear the spam window so the next customer message starts fresh instead of
    # immediately re-tripping the still-full window and disabling AI again.
    await reset_wa_spam_window(
        agent_id=str(agent.id),
        session_id=str(target_session.id),
        sender_id=target_session.external_user_id or "",
    )
    # Stop any leftover typing indicator from before the disable.
    await _stop_customer_typing(device_id, target_session, log)

    customer_phone = _wa_real_customer_phone(target_session)
    reply = f"AI untuk customer {customer_phone} sudah aktif kembali."
    await send_wa_message(device_id, operator_reply_target, reply)
    log.info("wa_incoming.operator_activated_ai", case_id=case_id, target_session_id=str(target_session.id))
    return {"status": "ok", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}


async def _reactivate_disabled_session_for_owner_turn(
    *,
    agent: Agent,
    session: Session,
    db: AsyncSession,
    device_id: str,
    log: structlog.BoundLogger,
) -> None:
    """Let owner/operator test messages resume a spam-disabled session.

    Spam auto-stop is meant to protect the backend from untrusted customer
    bursts. If the owner/operator sends a normal test message from their own
    WhatsApp identity, do not require them to quote the escalation card first.
    """
    meta = dict(session.metadata_ or {})
    meta["spam_auto_disabled"] = False
    meta.pop("spam_count", None)
    meta.pop("spam_window_seconds", None)
    session.metadata_ = meta
    session.ai_disabled = False
    db.add(session)
    await db.commit()
    await reset_wa_spam_window(
        agent_id=str(agent.id),
        session_id=str(session.id),
        sender_id=session.external_user_id or "",
    )
    await _stop_customer_typing(device_id, session, log)
    log.info("wa_incoming.owner_reactivated_ai", session_id=str(session.id))


def _is_low_information_wa_text(message: str | None) -> bool:
    """Return True for text that should not enter LLM context at all."""
    text = sanitize_user_input(message or "").strip()
    if not text or text.startswith("/"):
        return False
    return len(text) == 1


async def _handle_low_information_customer_message(
    *,
    agent: Agent,
    session: Session,
    db: AsyncSession,
    device_id: str,
    reply_target: str,
    message: str,
    log: structlog.BoundLogger,
) -> dict | None:
    """Short-circuit one-character WhatsApp noise before it reaches the LLM.

    A single-letter spam burst should not cause the agent to infer intent from
    older session history/workspace state. Send one throttled clarification,
    cancel any active run, then ignore repeated noise.
    """
    if not _is_low_information_wa_text(message):
        return None

    from app.core.engine.session_lock import cancel_active_run

    await cancel_active_run(session.id)
    await _stop_customer_typing(device_id, session, log)

    now = int(time.time())
    meta = dict(session.metadata_ or {})
    last_reply_at = int(meta.get("short_noise_reply_at") or 0)
    reply = ""
    status = "short_message_ignored"

    if now - last_reply_at >= 45:
        reply = "Pesan Anda terlalu singkat. Mohon kirim instruksi lengkap agar saya bisa membantu."
        await send_wa_message(device_id, reply_target, reply)
        meta["short_noise_reply_at"] = now
        status = "short_message_replied"

    meta["short_noise_last_text"] = sanitize_user_input(message or "").strip()
    meta["short_noise_last_at"] = now
    session.metadata_ = meta
    db.add(session)
    await db.commit()
    log.info("wa_incoming.low_information_message_ignored", session_id=str(session.id), status=status)
    return {
        "status": status,
        "reply": reply,
        "run_id": "",
        "steps": [],
        "messages_to_user": [reply] if reply else [],
    }


async def _forward_operator_media_to_customer(
    *,
    agent: Agent,
    quoted_text: str | None,
    device_id: str,
    operator_reply_target: str,
    media_type: str,
    media_data: str,
    media_filename: str | None,
    caption: str,
    operator_session: Session,
    db: AsyncSession,
    log: structlog.BoundLogger,
    quoted_stanza_id: str | None = None,
) -> dict | None:
    """Queue operator media for draft-first flow; send only after explicit confirmation."""
    if media_type not in {"image", "sticker", "document"}:
        return None

    target_session, case_id = await find_session_by_quoted_message_id(agent, db, quoted_stanza_id)
    if not target_session:
        target_session, case_id = await find_session_by_quoted_case(agent, db, quoted_text)
    if not target_session:
        reply = (
            "Reply pesan eskalasi yang benar saat mengirim gambar/dokumen, "
            "supaya saya tahu customer mana yang harus menerima lampiran ini."
        )
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "ok", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}
    await remember_operator_escalation_route(operator_session, target_session, case_id, db)

    media_context, _, _, media_meta = await process_wa_media(
        media_type=media_type,
        media_data=media_data,
        media_filename=media_filename,
        session_id=operator_session.id,
        logger=log,
    )
    if not media_meta:
        reply = "Gagal memproses lampiran operator. Coba kirim ulang gambarnya/dokumennya."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    target = _wa_customer_target(target_session)
    if not target:
        reply = "Gagal menyiapkan kirim media: target customer tidak ditemukan di session."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    sess_meta = dict(operator_session.metadata_ or {})
    sess_meta["pending_operator_media"] = {
        "target_session_id": str(target_session.id),
        "target": target,
        "case_id": case_id,
        "caption_hint": sanitize_user_input(caption or "").strip(),
        "created_at": int(time.time()),
        "expires_at": _operator_pending_expires_at(),
        **media_meta,
    }
    operator_session.metadata_ = sess_meta
    db.add(operator_session)
    await db.commit()

    draft_hint = sanitize_user_input(caption or "").strip()
    prompt = (
        f"[OPERATOR_MEDIA_PENDING] Case {case_id}. Lampiran {media_type} `{media_meta.get('filename', 'lampiran')}` siap dikirim ke customer. "
        "Buatkan draft pesan pendamping yang sopan dan profesional. Jangan kirim dulu; tunggu konfirmasi `kirim`."
    )
    if draft_hint:
        prompt += f" Preferensi operator: {draft_hint}."

    return {
        "status": "queued",
        "reply": prompt + media_context,
        "run_id": "",
        "steps": [],
        "messages_to_user": [],
    }


async def _send_pending_operator_media(
    *,
    operator_session: Session,
    device_id: str,
    operator_reply_target: str,
    db: AsyncSession,
    log: structlog.BoundLogger,
) -> dict | None:
    pending = dict(operator_session.metadata_ or {}).get("pending_operator_media")
    if not isinstance(pending, dict):
        return None
    if _operator_pending_is_expired(pending):
        await _forget_pending_operator_item(operator_session, "pending_operator_media", db)
        reply = "Draft lampiran sebelumnya sudah kedaluwarsa. Reply pesan eskalasi customer lalu kirim ulang lampirannya."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    target_session_id = pending.get("target_session_id")
    if not target_session_id:
        return None

    target_session = await db.get(Session, uuid.UUID(str(target_session_id)))
    if not target_session:
        reply = "Draft lampiran sebelumnya sudah tidak punya target customer yang valid. Silakan kirim ulang lampirannya."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    history_rows = list((await db.execute(
        select(Message).where(Message.session_id == operator_session.id).order_by(Message.timestamp.asc())
    )).scalars().all())[-12:]
    draft_message = _extract_media_draft_from_history(history_rows) or _build_customer_media_note(
        str(pending.get("media_type") or ""),
        str(pending.get("filename") or "") or None,
    )

    workspace_path = str(pending.get("workspace_path") or "")
    if not workspace_path:
        reply = "File lampiran draft sebelumnya tidak ditemukan. Silakan kirim ulang lampirannya."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    try:
        raw_b64 = base64.b64encode(Path(workspace_path).read_bytes()).decode("ascii")
        media_type = str(pending.get("media_type") or "")
        target = str(pending.get("target") or _wa_customer_target(target_session))
        filename = str(pending.get("filename") or "lampiran")
        mimetype = str(pending.get("mimetype") or "application/octet-stream")

        if media_type in {"image", "sticker"}:
            from app.core.infra.wa_client import send_wa_image

            await send_wa_image(device_id, target, raw_b64, draft_message, mimetype)
            tool_result = f"[IMAGE_SENT_TO_USER:{target}] {draft_message}"
        else:
            from app.core.infra.wa_client import send_wa_document

            await send_wa_document(device_id, target, raw_b64, filename, draft_message, mimetype)
            tool_result = f"[DOCUMENT_SENT_TO_USER:{target}] {filename} | {draft_message}"

        db.add(Message(
            session_id=target_session.id,
            role="agent",
            content=f"[TO_USER_MEDIA] {tool_result}",
            step_index=9003,
        ))
        sess_meta = dict(operator_session.metadata_ or {})
        sess_meta.pop("pending_operator_media", None)
        operator_session.metadata_ = sess_meta
        db.add(operator_session)
        await db.commit()
        reply = "Terkirim ✓"
        await send_wa_message(device_id, operator_reply_target, reply)
        return {
            "status": "ok",
            "reply": reply,
            "run_id": "",
            "steps": [{"tool": "operator_media_forward", "result": tool_result}],
            "messages_to_user": [{"type": "operator_media_forward", "target": target}],
        }
    except Exception as exc:
        reply = f"Gagal mengirim lampiran ke customer: {exc}"
        await send_wa_message(device_id, operator_reply_target, reply)
        log.warning("wa_incoming.operator_media_forward_failed", error=str(exc), case_id=pending.get("case_id"))
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}


async def _send_pending_operator_text_reply(
    *,
    operator_session: Session,
    device_id: str,
    operator_reply_target: str,
    db: AsyncSession,
    log: structlog.BoundLogger,
) -> dict | None:
    pending = dict(operator_session.metadata_ or {}).get("pending_operator_text_reply")
    if not isinstance(pending, dict):
        return None
    if _operator_pending_is_expired(pending):
        await _forget_pending_operator_item(operator_session, "pending_operator_text_reply", db)
        reply = "Draft pesan sebelumnya sudah kedaluwarsa. Reply pesan eskalasi customer lalu buat draft baru."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    target_session_id = pending.get("target_session_id")
    if not target_session_id:
        return None

    try:
        target_session = await db.get(Session, uuid.UUID(str(target_session_id)))
    except (ValueError, TypeError):
        target_session = None
    if not target_session:
        reply = "Draft sebelumnya sudah tidak punya target customer yang valid. Silakan reply ulang pesan eskalasinya."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    message = sanitize_user_input(str(pending.get("message") or "")).strip()
    target = str(pending.get("target") or _wa_customer_target(target_session))
    if not message or not target:
        reply = "Draft sebelumnya kosong atau target customer tidak ditemukan. Silakan buat draft ulang."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    try:
        await send_wa_message(device_id, target, message)
        tool_result = f"[SENT_TO_USER] {message}"
        db.add(Message(
            session_id=target_session.id,
            role="agent",
            content=f"[TO_USER] {message}",
            step_index=9001,
        ))
        sess_meta = dict(operator_session.metadata_ or {})
        sess_meta.pop("pending_operator_text_reply", None)
        operator_session.metadata_ = sess_meta
        db.add(operator_session)
        # Close the case so re-replying to the same old escalation notification
        # is not treated as a new outbound message.
        tmeta = dict(target_session.metadata_ or {})
        if pending.get("case_id"):
            tmeta["escalation_replied_case"] = pending.get("case_id")
            tmeta["escalation_replied_at"] = int(time.time())
            target_session.metadata_ = tmeta
            db.add(target_session)
        await db.commit()
        reply = "Terkirim ✓"
        await send_wa_message(device_id, operator_reply_target, reply)
        log.info("wa_incoming.operator_text_reply_sent", target=target, case_id=pending.get("case_id"))
        return {
            "status": "ok",
            "reply": reply,
            "run_id": "",
            "steps": [{"tool": "reply_to_user", "result": tool_result}],
            "messages_to_user": [{"type": "reply_to_user", "target": target}],
        }
    except Exception as exc:
        reply = f"Gagal mengirim pesan ke customer: {exc}"
        await send_wa_message(device_id, operator_reply_target, reply)
        log.warning("wa_incoming.operator_text_reply_failed", error=str(exc), case_id=pending.get("case_id"))
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}


async def _resume_customer_workflow_after_operator_approval(
    *,
    agent: Agent,
    target_session: Session,
    case_id: str | None,
    approval_text: str,
    device_id: str,
    operator_reply_target: str,
    db: AsyncSession,
    log: structlog.BoundLogger,
) -> dict:
    """Run the approved business workflow in the customer session, not the operator session."""
    customer_target = _wa_customer_target(target_session)
    if not customer_target:
        reply = "Approval pembayaran diterima, tapi target WhatsApp customer tidak ditemukan di session."
        await send_wa_message(device_id, operator_reply_target, reply)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    meta = dict(target_session.metadata_ or {})
    meta["last_operator_approval"] = {
        "type": "payment",
        "case_id": case_id,
        "message": sanitize_user_input(approval_text or "").strip(),
        "approved_at": int(time.time()),
    }
    target_session.metadata_ = meta
    db.add(target_session)
    await db.commit()

    synthetic_message = (
        "[SYSTEM_OPERATOR_APPROVAL]\n"
        f"Case ID: {case_id or '-'}\n"
        "Jenis approval: pembayaran customer sudah dikonfirmasi oleh operator/admin.\n"
        f"Pesan operator: {sanitize_user_input(approval_text or '').strip()}\n\n"
        "Lanjutkan workflow customer dari riwayat percakapan sesi ini. "
        "Jika semua data customer sudah lengkap, buat dan/atau kirim deliverable berbayar sekarang memakai tool yang tersedia. "
        "Jika deliverable berupa file/gambar dan tool WhatsApp media tersedia, kirim file/gambar langsung ke customer. "
        "Jangan eskalasi pembayaran lagi. Jika ada data yang masih kurang, tanyakan hanya data yang kurang ke customer."
    )

    try:
        from app.core.engine.agent_runner import run_agent
        from app.core.engine.session_lock import session_run_lock

        async with session_run_lock(target_session.id):
            result = await run_agent(
                agent_model=agent,
                session=target_session,
                user_message=synthetic_message,
                db=db,
                escalation_user_jid=None,
                escalation_context=None,
                media_image_b64=None,
                media_image_mime=None,
                sender_name=(target_session.channel_config or {}).get("sender_name")
                if isinstance(target_session.channel_config, dict)
                else None,
                prior_run_was_interrupted=False,
            )
    except Exception as exc:
        reply = f"Approval pembayaran diterima, tapi gagal melanjutkan workflow customer: {exc}"
        await send_wa_message(device_id, operator_reply_target, reply)
        log.warning("wa_incoming.operator_payment_resume_failed", error=str(exc), case_id=case_id)
        return {"status": "error", "reply": reply, "run_id": "", "steps": [], "messages_to_user": []}

    _tokens_this_run = int(result.get("tokens_used", 0) or 0)
    if _tokens_this_run > 0:
        await record_agent_token_usage(agent, _tokens_this_run, db)
        await db.flush()
        await db.commit()

    customer_reply = str(result.get("reply") or "").strip()
    steps = result.get("steps", [])
    messages_to_user: list[dict] = []
    if customer_reply and not should_skip_whatsapp_final_reply(customer_reply, steps):
        wa_reply = markdown_to_wa(customer_reply) or customer_reply
        if wa_reply.strip():
            await send_wa_message(device_id, customer_target, wa_reply.strip())
            messages_to_user.append({"type": "approval_resume_reply", "target": customer_target})

    reply = "Pembayaran saya catat approved. Workflow customer sudah saya lanjutkan dari sesi customer."
    await send_wa_message(device_id, operator_reply_target, reply)
    log.info(
        "wa_incoming.operator_payment_approval_resumed_customer",
        case_id=case_id,
        target_session_id=str(target_session.id),
    )
    return {
        "status": "ok",
        "reply": reply,
        "run_id": str(result.get("run_id", "")),
        "steps": steps,
        "messages_to_user": messages_to_user,
    }


@router.get("/wa-dev/operator-route")
async def wa_dev_operator_route(
    phone: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Used by wa-dev-service: given a phone number, find which agent this phone is an operator for.
    Returns {agent_id, agent_name} or 404 if not an operator for any agent.
    This allows wa-dev to auto-route operator messages without requiring the operator
    to do 'connect {agentID}' explicitly.
    """
    normalized = normalize_phone(phone)
    agents_result = await db.execute(
        select(Agent).where(Agent.is_deleted.is_(False))
    )
    for agent in agents_result.scalars().all():
        op_ids: list = getattr(agent, "operator_ids", None) or []
        esc_cfg: dict = agent.escalation_config or {}
        op_phone: str = esc_cfg.get("operator_phone", "")

        all_ops = {normalize_phone(p) for p in op_ids if p}
        if op_phone:
            all_ops.add(normalize_phone(op_phone))

        if normalized in all_ops:
            return {"agent_id": str(agent.id), "agent_name": agent.name}

    raise HTTPException(status_code=404, detail="Not an operator for any agent")


class WADevClaimCodeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=32)
    phone: str | None = None
    chat_id: str | None = None
    push_name: str | None = None


@router.post("/wa-dev/claim-code")
async def wa_dev_claim_code(
    body: WADevClaimCodeRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Used by wa-dev-service shared trial number.

    A user sends a short reusable code generated by Arthur. The Go gateway calls
    this endpoint to resolve that code to an agent, then stores phone->agent
    routing locally so future messages from that WA number go to the right agent.
    """
    from app.core.domain.wa_dev_trial_service import (
        find_agent_by_wa_dev_trial_code,
        normalize_wa_dev_trial_code,
    )

    code = normalize_wa_dev_trial_code(body.code)
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Kode harus 6 karakter")

    agent = await find_agent_by_wa_dev_trial_code(db, code)
    if not agent:
        raise HTTPException(status_code=404, detail="Kode tidak ditemukan atau sudah tidak aktif")

    virtual_device_id = _wa_dev_virtual_device_id(agent.id)
    if body.phone or body.chat_id:
        reply_target = body.chat_id or body.phone or ""
        lookup_user_id = body.phone or body.chat_id or ""
        session, _ = await find_or_create_wa_session(
            agent=agent,
            lookup_user_id=lookup_user_id,
            effective_reply_target=reply_target,
            device_id=virtual_device_id,
            db=db,
            is_operator=False,
            phone_number=normalize_phone(body.phone) if body.phone else None,
            sender_name=body.push_name or None,
        )
        meta = dict(session.metadata_ or {})
        meta["wa_dev_trial_code"] = code
        meta["wa_dev_claimed_at"] = int(time.time())
        meta["wa_dev_virtual_device_id"] = virtual_device_id
        session.metadata_ = meta
        db.add(session)
        await db.commit()

    return {
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "code": code,
        "device_id": virtual_device_id,
        "virtual_device_id": virtual_device_id,
        "routing": {
            "agent_id": str(agent.id),
            "device_id": virtual_device_id,
            "phone": normalize_phone(body.phone) if body.phone else "",
            "chat_id": body.chat_id or "",
        },
    }


class WADevDisconnectRequest(BaseModel):
    agent_id: uuid.UUID
    phone: str | None = None
    from_phone: str | None = None
    chat_id: str | None = None


@router.post("/wa-dev/disconnect")
async def wa_dev_disconnect(
    body: WADevDisconnectRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Used by wa-dev-service when a shared-number trial user sends /stop.

    The Go gateway owns phone->agent routing, but Python owns active run
    cancellation. This endpoint prevents an old run from sending a late final
    reply after the trial connection was disconnected.
    """
    disconnected_sessions = await _disconnect_wa_dev_sessions(
        agent_id=body.agent_id,
        phone=body.phone,
        from_phone=body.from_phone,
        chat_id=body.chat_id,
        db=db,
    )
    return {"status": "ok", "disconnected_sessions": disconnected_sessions}


class IncomingMessage(BaseModel):
    from_phone: str | None = None
    message: str = Field(..., max_length=10_000)


class WAIncomingMessage(BaseModel):
    device_id: str
    agent_id: uuid.UUID | None = None  # wa-dev-service should pass resolved target agent for shared demo number
    trial_code: str | None = None      # optional first-message code fallback for wa-dev-service
    from_: Annotated[str, Field(alias="from")]
    phone_from: str | None = None      # resolved phone number from Go (LID → phone); fallback ke from_
    chat_id: str | None = None  # group JID (xxx@g.us) atau nomor DM; kalau None fallback ke from_
    sender_alt: str | None = None      # alternate sender JID from whatsmeow; often phone@s.whatsapp.net for LID DMs
    addressing_mode: str | None = None
    message: str = Field(..., max_length=10_000)
    message_id: str | None = None      # WhatsApp stanza/message ID; lebih akurat untuk dedupe dibanding timestamp
    timestamp: int | None = None
    push_name: str | None = None       # WhatsApp display name of sender
    # Media fields — diisi oleh Go service saat pesan mengandung gambar/dokumen/sticker/audio
    media_type: str | None = None      # "image" | "document" | "sticker" | "audio" | "ptt" | None
    media_data: str | None = Field(None, max_length=10_000_000)  # base64-encoded raw bytes
    media_filename: str | None = None  # original filename (dokumen) atau generated (gambar/audio)
    media_mimetype: str | None = None
    quoted_text: str | None = None     # text of the quoted/replied-to message (for escalation routing)
    quoted_stanza_id: str | None = None
    quoted_participant: str | None = None
    quoted_remote_jid: str | None = None

    model_config = {"populate_by_name": True}


@router.post("/incoming/{session_id}")
async def incoming_message(
    session_id: uuid.UUID,
    body: IncomingMessage,
    db: AsyncSession = Depends(get_db),
):
    """
    Terima pesan masuk dari channel eksternal.
    Dipakai sebagai webhook target untuk WhatsApp, Telegram, dll.
    """
    # Load session
    sess_result = await db.execute(select(Session).where(Session.id == session_id))
    session = sess_result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session tidak ditemukan")

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == session.agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent tidak ditemukan")

    from_phone = body.from_phone or ""
    raw_message = body.message
    escalation_cfg: dict = agent.escalation_config or {}
    operator_phone: str = escalation_cfg.get("operator_phone", "")

    log = logger.bind(session_id=str(session_id), from_phone=from_phone)

    # --- Tentukan jenis pengirim ---
    _op_ids: list = getattr(agent, "operator_ids", None) or []
    _norm_op_ids = {normalize_phone(oid) for oid in _op_ids if oid}
    if operator_phone:
        _norm_op_ids.add(normalize_phone(operator_phone))
    is_operator = bool(_norm_op_ids and from_phone and normalize_phone(from_phone) in _norm_op_ids)

    if is_operator:
        user_message = (
            f"<OPERATOR>\n"
            f"No Telepon/WA/Id: {from_phone or '(tidak diketahui)'}\n"
            f"Pesan: {raw_message}"
        )
        log.info("channels.incoming.operator_command")
    else:
        user_message = raw_message
        log.info("channels.incoming.normal")

    # --- /reset intercept ---
    if not is_operator and user_message.strip().lower() == "/reset":
        await db.execute(delete(Message).where(Message.session_id == session.id))
        session.metadata_ = {}
        db.add(session)
        await db.commit()
        return {"status": "ok", "reply": "Percakapan direset.", "run_id": "", "steps": [], "messages_to_user": []}

    quota_check = await check_agent_quota(agent, db)
    if not quota_check.allowed:
        log.warning(
            "channels.incoming.quota_blocked",
            reason=quota_check.reason,
            detail=quota_check.detail,
        )
        if session.channel_type:
            try:
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
                    text=quota_check.user_message,
                )
            except Exception as exc:
                log.warning("channels.incoming.quota_block_reply_failed", error=str(exc))
        return {
            "status": "quota_exhausted",
            "reason": quota_check.reason,
            "reply": quota_check.user_message,
            "run_id": "",
            "steps": [],
            "messages_to_user": [],
        }

    # --- Jalankan agent ---
    from app.core.engine.session_lock import (
        cancel_active_run,
        is_latest_session_turn,
        mark_latest_session_turn,
        register_active_task,
        session_run_lock,
        unregister_active_task,
    )
    from app.core.engine.agent_runner import run_agent  # deferred to avoid circular import

    _prior_interrupted = False
    session_id = session.id
    turn_generation = 0
    if not is_operator:
        turn_generation = await mark_latest_session_turn(session_id)
        _prior_interrupted = await cancel_active_run(session_id)

    try:
        async with session_run_lock(session_id):
            if not is_operator and not await is_latest_session_turn(session_id, turn_generation):
                log.info(
                    "channels.incoming.stale_turn_ignored",
                    session_id=str(session_id),
                    turn_generation=turn_generation,
                )
                return {"status": "stale_ignored", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
            if not is_operator:
                await db.refresh(session, attribute_names=["ai_disabled"])
                if getattr(session, "ai_disabled", False):
                    log.info("channels.incoming.ai_disabled_after_wait", session_id=str(session_id))
                    return {"status": "ai_disabled", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
            current_task = asyncio.current_task()
            if current_task and not is_operator:
                await register_active_task(session_id, current_task)
            result = await run_agent(
                agent_model=agent,
                session=session,
                user_message=user_message,
                db=db,
                prior_run_was_interrupted=_prior_interrupted,
            )
    except asyncio.CancelledError:
        await unregister_active_task(session_id, asyncio.current_task())
        await db.rollback()
        return {"status": "cancelled", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
    except (TimeoutError, asyncio.TimeoutError):
        await unregister_active_task(session_id, asyncio.current_task())
        await db.rollback()
        log.warning("channels.incoming.session_lock_timeout", session_id=str(session_id))
        return {"status": "timeout", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
    except Exception as exc:
        await unregister_active_task(session_id, asyncio.current_task())
        log.error("channels.incoming.agent_error", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Agent error: {exc}")
    finally:
        await unregister_active_task(session_id, asyncio.current_task())

    if not is_operator and not await is_latest_session_turn(session_id, turn_generation):
        log.info(
            "channels.incoming.stale_result_suppressed",
            session_id=str(session_id),
            turn_generation=turn_generation,
        )
        return {
            "status": "stale_ignored",
            "reply": "",
            "run_id": str(result.get("run_id", "")),
            "steps": result.get("steps", []),
            "messages_to_user": [],
        }

    _tokens_this_run = int(result.get("tokens_used", 0) or 0)
    if _tokens_this_run > 0:
        await record_agent_token_usage(agent, _tokens_this_run, db)
        await db.flush()
        await db.commit()

    reply = result.get("reply", "")

    # --- Kirim reply ke channel ---
    if session.channel_type and reply:
        try:
            if is_operator:
                op_cfg = {**escalation_cfg, "user_phone": operator_phone}
                await send_message(
                    channel_type=escalation_cfg.get("channel_type", session.channel_type),
                    channel_config=op_cfg,
                    text=reply,
                )
            else:
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config if isinstance(session.channel_config, dict) else {},
                    text=reply,
                )
        except Exception as exc:
            log.warning("channels.incoming.send_reply_failed", error=str(exc))

    steps = result.get("steps", [])
    return {
        "status": "ok",
        "reply": reply,
        "run_id": str(result.get("run_id", "")),
        "steps": steps,
        "messages_to_user": extract_messages_to_user(steps),
    }


@router.post("/wa/incoming")
async def wa_incoming(
    body: WAIncomingMessage,
    db: AsyncSession = Depends(get_db),
):
    """
    Webhook called by the Go wa-service when a WhatsApp message arrives.
    Finds the agent by device_id, finds or creates a session for the sender,
    runs the agent, and replies via wa-service.

    Flow:
    1. Find agent by device_id (via find_agent_by_device)
    2. Check if sender is operator (via is_operator_message)
    3. If operator: load escalation context (via find_escalation_context)
    4. Find or create WA session (via find_or_create_wa_session)
    5. Process media if any (via process_wa_media)
    6. Run agent
    7. Send reply via wa-service
    """
    log = logger.bind(device_id=body.device_id, from_phone=body.from_)

    # 1. Find agent
    agent = await _resolve_wa_incoming_agent(body, db, log)
    if not agent:
        log.warning("wa_incoming.agent_not_found")
        raise HTTPException(status_code=404, detail="No agent found for this WhatsApp device")

    # phone_from: phone number yang sudah di-resolve dari LID oleh Go wa-service.
    # Untuk akun LID: body.from_ bisa berisi LID/JID, body.phone_from berisi phone number asli.
    # Kalau phone_from tersedia, itu yang dipakai. Kalau tidak, hanya pakai from_
    # jika memang bukan identifier LID.
    real_from_phone = (
        resolve_incoming_wa_phone(body.from_, body.phone_from)
        or resolve_incoming_wa_phone(body.sender_alt, None)
        or resolve_incoming_wa_phone(body.chat_id, body.phone_from)
    )
    sender_lid = extract_wa_lid(body.from_, body.sender_alt, body.chat_id)
    if not real_from_phone and sender_lid:
        # wa-service gagal resolve LID→PN turn ini; pakai mapping yang pernah
        # dipelajari (users.wa_lid) supaya identitas tetap nomor telepon asli.
        try:
            from app.core.domain.subscription_service import resolve_phone_for_wa_lid

            real_from_phone = await resolve_phone_for_wa_lid(sender_lid, db)
            if real_from_phone:
                log.info(
                    "wa_incoming.lid_resolved_from_learned_mapping",
                    lid=sender_lid,
                )
        except Exception as _lid_exc:
            log.warning("wa_incoming.lid_mapping_lookup_failed", error=str(_lid_exc)[:200])
            try:
                await db.rollback()
            except Exception:
                pass
    from_phone = real_from_phone or body.from_
    reply_target = body.chat_id or body.from_
    _is_owner_sender = _is_wa_owner_sender(
        agent,
        from_phone,
        real_from_phone,
        body.phone_from,
        body.sender_alt,
        reply_target,
    )

    if _is_wa_dev_device(body.device_id) and _is_wa_dev_disconnect_command(body.message):
        disconnected_sessions = await _disconnect_wa_dev_sessions(
            agent_id=agent.id,
            phone=real_from_phone,
            from_phone=body.from_,
            chat_id=body.chat_id,
            db=db,
        )
        try:
            await send_wa_message(
                body.device_id,
                reply_target,
                "✅ Kamu sudah disconnect dari agent.\n\nKirim kode baru dari Arthur kapan saja untuk connect ke agent lagi.",
            )
        except Exception as exc:
            log.warning("wa_incoming.wa_dev_disconnect_reply_failed", error=str(exc)[:200])
        log.info("wa_incoming.wa_dev_disconnect_ignored", sessions=disconnected_sessions)
        return {
            "status": "disconnected",
            "reply": "",
            "run_id": "",
            "steps": [],
            "messages_to_user": [],
        }

    # 1.5. Cek deduplikasi WA (handling multiple webhook calls for the same message)
    if body.message_id or body.timestamp:
        if await is_duplicate_message(body.device_id, from_phone, body.timestamp, db, body.message_id):
            log.info("wa_incoming.duplicate_ignored")
            return {"status": "ignored", "reason": "duplicate message"}

    # 2. Cek apakah pesan ini benar-benar turn operator eskalasi.
    # Identitas admin/operator saja tidak cukup: admin sering mengetes agent
    # dari nomor yang sama. Operator mode hanya aktif saat reply ke pesan
    # eskalasi, atau saat mengonfirmasi draft pending dari reply eskalasi itu.
    _operator_identity = is_operator_message(from_phone, reply_target, agent)
    _is_operator = False
    if _operator_identity:
        _is_operator = await _should_treat_as_operator_turn(
            agent=agent,
            db=db,
            from_phone=from_phone,
            reply_target=reply_target,
            message=body.message,
            media_type=body.media_type,
            quoted_text=body.quoted_text,
            quoted_stanza_id=body.quoted_stanza_id,
        )
        if not _is_operator:
            log.info("wa_incoming.operator_identity_treated_as_customer")

    # 2.5. Fitur 1 — cek allowlist (hanya untuk non-operator)
    if not _is_operator:
        allowed = getattr(agent, "allowed_senders", None)
        if allowed:  # null/[] = semua diizinkan
            # Resolve setiap phone number di allowed_senders ke WA JID via Go IsOnWhatsApp.
            # Ini krusial untuk akun LID: phone +6282xxx bisa jadi JID "9876@lid" di WA.
            # resolved: {"6282xxx": "9876@lid", "1234": "1234@s.whatsapp.net", ...}
            resolved = await resolve_wa_phones(body.device_id, [p for p in allowed if p])

            # Bangun allowed_set berisi SEMUA identifier yang valid:
            # - JID yang di-resolve oleh WA (bisa @s.whatsapp.net atau @lid)
            # - Phone number asli (normalized, fallback jika resolve gagal)
            allowed_set: set[str] = set()
            for p in allowed:
                if not p:
                    continue
                normalized = normalize_phone(p)
                allowed_set.add(normalized)                      # raw phone
                jid = resolved.get(normalized)                   # resolved JID
                if jid:
                    allowed_set.add(normalize_phone(jid))        # JID user part (strip @domain)

            # Cek: incoming sender (from_phone atau chat_id) ada di allowed_set?
            candidates = {normalize_phone(from_phone)}
            if reply_target:
                candidates.add(normalize_phone(reply_target))
            if not candidates.intersection(allowed_set):
                log.info("wa_incoming.blocked_sender", from_phone=from_phone, chat_id=reply_target)
                return {"status": "ignored", "reason": "sender not in allowlist"}

    # 3. Jika operator, context eskalasi di-resolve setelah session operator
    # tersedia supaya target customer bisa disimpan untuk turn berikutnya.
    escalation_user_jid: str | None = None
    escalation_context: str | None = None

    # Tentukan lookup_user_id untuk session
    lookup_user_id = get_wa_lookup_user_id(
        from_phone=from_phone,
        chat_id=body.chat_id,
        is_operator=_is_operator,
        agent=agent,
    )

    # effective_reply_target: selalu pakai chat_id (atau from_ fallback).
    # chat_id dari wa-service sudah mengandung server info yang benar (@s.whatsapp.net atau @lid).
    effective_reply_target = reply_target

    # 4. Find or create session
    # from_phone = nomor asli (phone_from dari Go, sudah di-resolve dari LID)
    # effective_reply_target = JID untuk reply (bisa LID/chat_id)
    session, was_created = await find_or_create_wa_session(
        agent=agent,
        lookup_user_id=lookup_user_id,
        effective_reply_target=effective_reply_target,
        device_id=body.device_id,
        db=db,
        is_operator=_is_operator,
        # Only store the resolved phone number; never persist LID/JID as phone_number.
        phone_number=real_from_phone if not _is_operator else None,
        sender_name=body.push_name or None,
    )
    if was_created:
        log.info("wa_incoming.session_created", session_id=str(session.id), is_operator=_is_operator)
        # Commit agar session_id visible ke koneksi DB terpisah (e.g. scheduler_tool)
        await db.commit()

    provision_external_id = real_from_phone
    if provision_external_id and not _is_operator:
        try:
            from app.core.domain.subscription_service import get_or_create_wa_user

            # Simpan juga mapping LID→user saat turn ini membawa keduanya,
            # supaya turn LID-only berikutnya tetap ter-resolve ke nomor asli.
            await get_or_create_wa_user(provision_external_id, db, wa_lid=sender_lid)
            await db.commit()
        except Exception as _prov_exc:
            # Provisioning boleh gagal tanpa mematikan turn, tapi transaksi
            # yang aborted harus di-rollback agar query berikutnya tetap jalan.
            log.warning("wa_incoming.user_provision_failed", error=str(_prov_exc)[:300])
            try:
                await db.rollback()
            except Exception:
                pass

    _operator_recap_request = _is_operator and _is_operator_escalation_recap_request(body.message)
    if _is_operator and _operator_recap_request:
        escalation_context = await _build_operator_escalation_recap_context(agent=agent, db=db)
        log.info("wa_incoming.operator_escalation_recap_context")
    elif _is_operator:
        escalation_user_jid, escalation_context = await find_escalation_context(
            agent,
            db,
            quoted_text=body.quoted_text,
            quoted_stanza_id=body.quoted_stanza_id,
            operator_session=session,
        )
        log.info("wa_incoming.operator_session", escalation_user_jid=escalation_user_jid)

    if _is_operator and body.message.strip().lower() in {"/aktif", "/ active"}:
        return await _handle_operator_activate_command(
            agent=agent,
            quoted_text=body.quoted_text,
            device_id=body.device_id,
            operator_reply_target=reply_target,
            db=db,
            log=log,
            quoted_stanza_id=body.quoted_stanza_id,
        )

    if not _is_operator and _operator_identity and getattr(session, "ai_disabled", False):
        await _reactivate_disabled_session_for_owner_turn(
            agent=agent,
            session=session,
            db=db,
            device_id=body.device_id,
            log=log,
        )

    quota_check = await check_agent_quota(agent, db)
    if not quota_check.allowed:
        log.warning(
            "wa_incoming.quota_blocked",
            reason=quota_check.reason,
            detail=quota_check.detail,
            session_id=str(session.id),
        )
        return await _reply_agent_quota_blocked(
            device_id=body.device_id,
            reply_target=reply_target,
            message=quota_check.user_message,
            reason=quota_check.reason,
            log=log,
        )

    if _is_operator and not body.media_type and escalation_user_jid and _is_operator_payment_approval(body.message):
        target_session, case_id = await find_session_by_operator_active_route(agent, db, session)
        if target_session:
            return await _resume_customer_workflow_after_operator_approval(
                agent=agent,
                target_session=target_session,
                case_id=case_id,
                approval_text=body.message,
                device_id=body.device_id,
                operator_reply_target=reply_target,
                db=db,
                log=log,
            )

    if _is_operator and not body.media_type and _is_operator_send_confirmation(body.message):
        sent = await _send_pending_operator_media(
            operator_session=session,
            device_id=body.device_id,
            operator_reply_target=reply_target,
            db=db,
            log=log,
        )
        if sent is not None:
            return sent
        sent = await _send_pending_operator_text_reply(
            operator_session=session,
            device_id=body.device_id,
            operator_reply_target=reply_target,
            db=db,
            log=log,
        )
        if sent is not None:
            return sent
        return await _reply_no_pending_operator_confirmation(
            device_id=body.device_id,
            operator_reply_target=reply_target,
        )

    if _is_operator and body.media_type and body.media_data:
        forwarded = await _forward_operator_media_to_customer(
            agent=agent,
            quoted_text=body.quoted_text,
            device_id=body.device_id,
            operator_reply_target=reply_target,
            media_type=body.media_type,
            media_data=body.media_data,
            media_filename=body.media_filename,
            caption=body.message,
            operator_session=session,
            db=db,
            log=log,
            quoted_stanza_id=body.quoted_stanza_id,
        )
        if forwarded is not None and forwarded.get("status") == "queued":
            body.media_type = None
            body.media_data = None
            body.media_filename = None
            body.message = str(forwarded.get("reply") or "")
        elif forwarded is not None:
            return forwarded

    if not _is_operator and not getattr(session, "ai_disabled", False):
        spam_window_seconds = 60
        is_spam, spam_count = await check_wa_spam_window(
            agent_id=str(agent.id),
            session_id=str(session.id),
            sender_id=session.external_user_id or from_phone,
            limit=5,
            window_seconds=spam_window_seconds,
        )
        if is_spam:
            from app.core.engine.session_lock import cancel_active_run

            await cancel_active_run(session.id)
            case_id = await _notify_operator_spam_autostop(
                agent=agent,
                session=session,
                db=db,
                device_id=body.device_id,
                count=spam_count,
                window_seconds=spam_window_seconds,
                last_message=body.message,
                log=log,
            )
            log.warning(
                "wa_incoming.spam_auto_disabled",
                session_id=str(session.id),
                count=spam_count,
                case_id=case_id,
            )
            return {
                "status": "ai_disabled",
                "reason": "spam_auto_disabled",
                "case_id": case_id,
                "reply": "",
                "run_id": "",
                "steps": [],
                "messages_to_user": [],
            }

    # 4.5. Fitur 2 — cek ai_disabled (hanya untuk non-operator)
    if not _is_operator and getattr(session, "ai_disabled", False):
        log.info("wa_incoming.ai_disabled", session_id=str(session.id))
        await _stop_customer_typing(body.device_id, session, log)
        return {"status": "ai_disabled"}

    if not _is_operator and not body.media_type and not body.quoted_text:
        low_info_result = await _handle_low_information_customer_message(
            agent=agent,
            session=session,
            db=db,
            device_id=body.device_id,
            reply_target=reply_target,
            message=body.message,
            log=log,
        )
        if low_info_result is not None:
            return low_info_result

    # 5. Proses media jika ada
    media_context = ""
    media_image_b64: str | None = None
    media_image_mime: str | None = None
    media_meta: dict | None = None

    if body.media_type and not body.media_data:
        reply = _missing_media_payload_reply(body.media_type, body.media_filename)
        log.warning(
            "wa_incoming.media_payload_missing",
            media_type=body.media_type,
            media_filename=body.media_filename,
            media_mimetype=body.media_mimetype,
            message_id=body.message_id,
            session_id=str(session.id),
        )
        if not _is_operator:
            await _stop_customer_typing(body.device_id, session, log)
        await send_wa_message(body.device_id, reply_target, reply)
        return {"status": "media_payload_missing", "reply": reply, "run_id": "", "steps": [], "messages_to_user": [reply]}

    if body.media_type and body.media_data:
        media_context, media_image_b64, media_image_mime, media_meta = await process_wa_media(
            media_type=body.media_type,
            media_data=body.media_data,
            media_filename=body.media_filename,
            session_id=session.id,
            logger=log,
        )
        if media_meta:
            import time as _time
            _saved_at = int(_time.time())
            _source_message_id = str(body.message_id or "").strip()
            sess_meta = dict(session.metadata_ or {})
            sess_meta["last_incoming_media"] = {
                **media_meta,
                "saved_at": _saved_at,
                "from_operator": _is_operator,
                "source_message_id": _source_message_id,
            }
            sess_meta["current_turn_media"] = {
                "workspace_path": media_meta.get("workspace_path"),
                "source_message_id": _source_message_id,
                "saved_at": _saved_at,
            }
            sess_meta["current_attachment"] = {
                "media_type": media_meta.get("media_type"),
                "filename": media_meta.get("filename"),
                "input_path": media_meta.get("current_input_path") or media_meta.get("shared_alias"),
                "subagent_input_path": (
                    media_meta.get("subagent_current_input_path")
                    or media_meta.get("incoming_alias")
                ),
                "shared_path": media_meta.get("current_input_path") or media_meta.get("shared_alias"),
                "legacy_shared_path": media_meta.get("shared_alias"),
                "extracted_text_path": media_meta.get("extracted_text_path"),
                "extracted_text_subagent_path": media_meta.get("extracted_text_subagent_path"),
                "saved_at": _saved_at,
                "from_operator": _is_operator,
            }
            session.metadata_ = sess_meta
            db.add(session)
            await db.commit()
    elif not body.media_type:
        sess_meta = dict(session.metadata_ or {})
        if sess_meta.pop("current_turn_media", None) is not None:
            session.metadata_ = sess_meta
            db.add(session)
            await db.commit()

    # For audio/ptt, media_context already contains the full transcript label —
    # drop the raw "[Voice note]"/"[Audio]" placeholder from Go to avoid confusion.
    if body.media_type in ("ptt", "audio") and media_context:
        user_message = media_context.strip()
    else:
        user_message = sanitize_user_input(body.message) + media_context
    if not _is_operator:
        user_message = _append_quoted_reply_context(user_message, body.quoted_text)
    if _is_operator:
        _pending_revision_context = _operator_pending_text_revision_context(session, body.message)
        if _pending_revision_context:
            user_message = f"{_pending_revision_context}\n\n{user_message}".strip()
        user_message = _label_owner_wa_message(
            message=user_message,
            from_phone=from_phone,
            sender_name=body.push_name or None,
            is_operator_turn=True,
        ) if _is_owner_sender else (
            f"<OPERATOR>\n"
            + (f"Name WA: {body.push_name}\n" if body.push_name else "")
            + f"No Telepon/WA/Id: {from_phone}\n"
            f"Pesan: {user_message}"
        )
        log.info("wa_incoming.operator_command")
        # Deterministic operator escalation reply: if the operator quoted an
        # escalation message and typed plain text, stage it as a draft for the
        # customer instead of leaving it to the LLM (which may just acknowledge).
        _staged_draft = await _maybe_stage_operator_text_draft(
            agent=agent,
            operator_session=session,
            quoted_text=body.quoted_text,
            quoted_stanza_id=body.quoted_stanza_id,
            operator_message=body.message,
            device_id=body.device_id,
            operator_reply_target=reply_target,
            db=db,
            log=log,
        )
        if _staged_draft is not None:
            return _staged_draft
    else:
        if _is_owner_sender:
            user_message = _label_owner_wa_message(
                message=user_message,
                from_phone=from_phone,
                sender_name=body.push_name or None,
                is_operator_turn=False,
            )
        log.info("wa_incoming.normal")

    sender_name: str | None = body.push_name or None

    # 5.5. Handle /reset keyword — hapus history sesi, bersihkan metadata
    _raw_msg_stripped = user_message.strip()
    if not _is_operator and _raw_msg_stripped.lower() in ("/reset", "/ reset"):
        log.info("wa_incoming.reset_requested", session_id=str(session.id))
        await db.execute(delete(Message).where(Message.session_id == session.id))
        _clean_meta = {}
        session.metadata_ = _clean_meta
        db.add(session)
        await db.commit()
        try:
            await send_wa_message(
                body.device_id,
                effective_reply_target,
                "✅ Percakapan direset. Memori sesi ini telah dibersihkan — kita mulai dari awal!",
            )
        except Exception as _reset_exc:
            log.warning("wa_incoming.reset_reply_failed", error=str(_reset_exc))
        return {"status": "ok", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}

    # 6. Run agent
    import asyncio as _asyncio
    from app.core.engine.agent_runner import run_agent  # deferred to avoid circular import
    from app.core.engine.session_lock import (
        cancel_active_run,
        is_latest_session_turn,
        mark_latest_session_turn,
        register_active_task,
        session_run_lock,
        unregister_active_task,
    )

    # Bind the file uploaded in THIS turn as the single source of truth so the
    # agent cannot fall back to a previously uploaded file (see audit fix B).
    current_attachment_name: str | None = None
    if body.media_type in ("document", "image") and body.media_data:
        current_attachment_name = sanitize_user_input(body.media_filename or "").strip() or None

    # Cancel any in-progress run for this session (human interrupt).
    # Operator messages are never interrupted — they're short command turns.
    _prior_interrupted = False
    session_id = session.id
    turn_generation = 0
    if not _is_operator:
        turn_generation = await mark_latest_session_turn(session_id)
        _prior_interrupted = await cancel_active_run(session_id)

    try:
        async with session_run_lock(session_id):
            if not _is_operator and not await is_latest_session_turn(session_id, turn_generation):
                log.info(
                    "wa_incoming.stale_turn_ignored",
                    session_id=str(session_id),
                    turn_generation=turn_generation,
                )
                return {"status": "stale_ignored", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
            if not _is_operator:
                await db.refresh(session, attribute_names=["ai_disabled"])
                if getattr(session, "ai_disabled", False):
                    log.info("wa_incoming.ai_disabled_after_wait", session_id=str(session_id))
                    return {"status": "ai_disabled", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
            # Register task INSIDE the lock — ensures cancel_active_run always
            # targets the task that actually holds the lock and is running.
            _current_task = _asyncio.current_task()
            if _current_task and not _is_operator:
                await register_active_task(session_id, _current_task)
            result = await run_agent(
                agent_model=agent,
                session=session,
                user_message=user_message,
                db=db,
                escalation_user_jid=escalation_user_jid,
                escalation_context=escalation_context,
                media_image_b64=media_image_b64,
                media_image_mime=media_image_mime,
                current_attachment_name=current_attachment_name,
                sender_name=sender_name,
                prior_run_was_interrupted=_prior_interrupted,
            )
    except _asyncio.CancelledError:
        log.info("wa_incoming.cancelled_by_interrupt", session_id=str(session_id))
        await unregister_active_task(session_id, _asyncio.current_task())
        await db.rollback()
        return {"status": "cancelled", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
    except (TimeoutError, _asyncio.TimeoutError):
        await unregister_active_task(session_id, _asyncio.current_task())
        await db.rollback()
        log.warning("wa_incoming.session_lock_timeout", session_id=str(session_id))
        try:
            await send_wa_message(body.device_id, effective_reply_target,
                "⏳ Sedang memproses pesan sebelumnya, mohon tunggu sebentar lalu kirim ulang.")
        except Exception:
            pass
        return {"status": "timeout", "reply": "", "run_id": "", "steps": [], "messages_to_user": []}
    except Exception as exc:
        await unregister_active_task(session_id, _asyncio.current_task())
        log.error("wa_incoming.agent_error", error=str(exc), exc_info=True)
        import traceback as _tb
        err_detail = _tb.format_exc()
        if _DEVELOPER_PHONE:
            try:
                await send_wa_message(
                    body.device_id,
                    _DEVELOPER_PHONE,
                    f"⚠️ *Agent Error*\nAgent: {agent.name}\nFrom: {from_phone}\n\n```\n{err_detail[:3000]}\n```",
                )
            except Exception as _notify_exc:
                log.warning("wa_incoming.developer_notify_failed", error=str(_notify_exc))
        try:
            await send_wa_message(body.device_id, effective_reply_target, _GENERIC_ERROR_MSG)
        except Exception as _send_exc:
            log.warning("wa_incoming.error_reply_failed", error=str(_send_exc))
        return {"status": "error", "reply": _GENERIC_ERROR_MSG, "run_id": "", "steps": [], "messages_to_user": []}
    finally:
        await unregister_active_task(session_id, _asyncio.current_task())

    if not _is_operator and not await is_latest_session_turn(session_id, turn_generation):
        log.info(
            "wa_incoming.stale_result_suppressed",
            session_id=str(session_id),
            turn_generation=turn_generation,
        )
        return {
            "status": "stale_ignored",
            "reply": "",
            "run_id": str(result.get("run_id", "")),
            "steps": result.get("steps", []),
            "messages_to_user": [],
        }

    reply = result.get("reply", "")
    steps = result.get("steps", [])
    messages_to_user = extract_messages_to_user(steps)
    final_reply_sent = False
    final_reply_suppressed = False
    final_reply_send_error = ""
    delivered_reply = ""

    if _is_operator and escalation_user_jid and reply:
        draft_message = _extract_operator_text_draft(reply)
        if draft_message:
            target_session, case_id = await find_session_by_operator_active_route(agent, db, session)
            if target_session:
                await _remember_pending_operator_text_reply(
                    operator_session=session,
                    target_session=target_session,
                    case_id=case_id,
                    draft_message=draft_message,
                    db=db,
                )
                log.info(
                    "wa_incoming.operator_text_draft_pending",
                    case_id=case_id,
                    target_session_id=str(target_session.id),
                )

    # 7a. Update token usage on agent + user subscription
    _tokens_this_run: int = result.get("tokens_used", 0)
    if _tokens_this_run > 0:
        await record_agent_token_usage(agent, _tokens_this_run, db)
        await db.flush()
        await db.commit()

    # 7. Kirim reply ke channel
    if reply:
        try:
            if not _is_operator and should_skip_whatsapp_final_reply(reply, steps):
                log.info("wa_incoming.final_reply_suppressed_duplicate_outbound")
                final_reply_suppressed = True
                return {
                    "status": "ok",
                    "reply": "",
                    "run_id": str(result.get("run_id", "")),
                    "steps": steps,
                    "messages_to_user": messages_to_user,
                    "reply_delivery": {
                        "final_reply_sent": False,
                        "final_reply_suppressed": True,
                        "error": "",
                    },
                }
            wa_reply = markdown_to_wa(reply) or reply.strip()
            if not wa_reply:
                log.warning("wa_incoming.empty_reply_after_conversion", reply_len=len(reply))
                final_reply_suppressed = True
            else:
                escalation_cfg: dict = agent.escalation_config or {}
                operator_phone: str = escalation_cfg.get("operator_phone", "")

                if _is_operator:
                    # Kirim final reply ke operator
                    await send_wa_message(body.device_id, reply_target, wa_reply)
                    delivered_reply = wa_reply
                    final_reply_sent = True
                    messages_to_user.append({"type": "final_reply", "target": reply_target})
                    log.info(
                        "wa_incoming.final_reply_sent",
                        target=reply_target,
                        reply_len=len(wa_reply),
                        reply_preview=wa_reply[:220],
                    )
                else:
                    # Guard customer replies from accidental escalation/operator
                    # targets, but allow the operator to test the agent as a
                    # normal customer when no escalation context is active.
                    normalized_target = normalize_phone(reply_target)
                    normalized_operator = normalize_phone(operator_phone) if operator_phone else ""
                    if normalized_operator and normalized_target == normalized_operator and not _operator_identity:
                        log.warning("wa_incoming.reply_target_is_operator_suppressed", reply_target=reply_target)
                        final_reply_suppressed = True
                    else:
                        await send_wa_message(body.device_id, reply_target, wa_reply)
                        delivered_reply = wa_reply
                        final_reply_sent = True
                        messages_to_user.append({"type": "final_reply", "target": reply_target})
                        log.info(
                            "wa_incoming.final_reply_sent",
                            target=reply_target,
                            reply_len=len(wa_reply),
                            reply_preview=wa_reply[:220],
                        )
        except Exception as exc:
            final_reply_send_error = str(exc)
            log.error("wa_incoming.send_reply_failed", target=reply_target, error=final_reply_send_error)

    return {
        "status": "send_failed" if reply and not final_reply_sent and not final_reply_suppressed else "ok",
        "reply": delivered_reply or reply,
        "run_id": str(result.get("run_id", "")),
        "steps": steps,
        "messages_to_user": messages_to_user,
        "reply_delivery": {
            "final_reply_sent": final_reply_sent,
            "final_reply_suppressed": final_reply_suppressed,
            "error": final_reply_send_error,
            "raw_reply": reply,
            "sent_reply": delivered_reply,
        },
    }

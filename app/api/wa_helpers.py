"""
Helper functions untuk wa_incoming() endpoint di channels.py.

Dipecah dari channels.py (yang tadinya 325+ baris) menjadi fungsi-fungsi
kecil yang bisa di-test dan di-maintain secara independen.
"""
from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.phone_utils import normalize_phone

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.session import Session

log = structlog.get_logger(__name__)


async def find_agent_by_device(device_id: str, db: AsyncSession):
    """
    Cari agent berdasarkan device_id.
    - prefix 'wadev_' → lookup by agent UUID (virtual device dari wa-dev-service)
    - lainnya → lookup by wa_device_id field
    Returns Agent atau None.
    """
    from app.models.agent import Agent

    if device_id.startswith("wadev_"):
        try:
            agent_uuid = uuid.UUID(device_id[len("wadev_"):])
        except ValueError:
            return None
        result = await db.execute(
            select(Agent).where(
                Agent.id == agent_uuid,
                Agent.is_deleted.is_(False),
            )
        )
    else:
        result = await db.execute(
            select(Agent).where(
                Agent.wa_device_id == device_id,
                Agent.is_deleted.is_(False),
            )
        )
    return result.scalar_one_or_none()


async def find_or_create_wa_session(
    *,
    agent,
    lookup_user_id: str,
    effective_reply_target: str,
    device_id: str,
    db: AsyncSession,
    is_operator: bool,
) -> tuple:
    """
    Cari session WhatsApp yang sudah ada; buat baru jika belum ada.
    Juga update device_id dan user_phone jika berubah.

    Returns (session, was_created: bool)
    """
    from app.models.session import Session

    result = await db.execute(
        select(Session).where(
            Session.agent_id == agent.id,
            Session.channel_type == "whatsapp",
            Session.external_user_id == lookup_user_id,
        )
    )
    session = result.scalars().first()

    if session:
        # Pastikan device_id dan user_phone selalu up-to-date
        raw_cfg = session.channel_config
        new_config = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        if (
            new_config.get("device_id") != device_id
            or new_config.get("user_phone") != effective_reply_target
        ):
            new_config["device_id"] = device_id
            new_config["user_phone"] = effective_reply_target
            session.channel_config = new_config
            await db.flush()
        return session, False

    # Buat session baru
    session = Session(
        agent_id=agent.id,
        external_user_id=lookup_user_id,
        channel_type="whatsapp",
        channel_config={
            "user_phone": effective_reply_target,
            "device_id": device_id,
        },
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session, True


async def find_escalation_context(agent, db: AsyncSession) -> tuple[str | None, str | None]:
    """
    Cari session user yang sedang dalam eskalasi aktif untuk agent ini.

    Returns (escalation_user_jid, escalation_context_text)
    - escalation_user_jid: JID user yang dieskalasi (untuk dikirim reply)
    - escalation_context_text: ringkasan pesan terakhir user (untuk context operator)
    """
    from app.models.message import Message
    from app.models.session import Session

    esc_result = await db.execute(
        select(Session)
        .join(Message, Message.session_id == Session.id)
        .where(
            Session.agent_id == agent.id,
            Message.role == "escalation",
        )
        .order_by(desc(Message.timestamp))
        .limit(1)
    )
    esc_session = esc_result.scalars().first()
    if not esc_session:
        return None, None

    raw_ch = esc_session.channel_config
    ch = raw_ch if isinstance(raw_ch, dict) else {}
    escalation_user_jid = ch.get("user_phone") or esc_session.external_user_id

    # Ambil 5 pesan terakhir dari user dalam sesi eskalasi
    recent_result = await db.execute(
        select(Message)
        .where(
            Message.session_id == esc_session.id,
            Message.role == "user",
        )
        .order_by(desc(Message.step_index))
        .limit(5)
    )
    recent_msgs = list(reversed(recent_result.scalars().all()))

    escalation_context: str | None = None
    if recent_msgs:
        lines = []
        for m in recent_msgs:
            content = m.content or ""
            content = content.removeprefix("[USER_IN_ESCALATION] ").strip()
            if content:
                lines.append(f"- {content}")
        if lines:
            escalation_context = "\n".join(lines)

    return escalation_user_jid, escalation_context


async def process_wa_media(
    *,
    media_type: str,
    media_data: str,
    media_filename: str | None,
    session_id: uuid.UUID,
    logger: structlog.BoundLogger,
) -> tuple[str, str | None, str | None]:
    """
    Proses media (gambar/dokumen/stiker) dari pesan WhatsApp.

    Returns (media_context, media_image_b64, media_image_mime)
    - media_context: teks tambahan untuk disertakan ke LLM
    - media_image_b64: base64 gambar untuk multimodal input (hanya untuk image)
    - media_image_mime: MIME type gambar (hanya untuk image)
    """
    from app.config import get_settings
    from app.core.sandbox import get_workspace_dir

    media_context = ""
    media_image_b64: str | None = None
    media_image_mime: str | None = None

    try:
        raw_bytes = base64.b64decode(media_data)
        workspace = get_workspace_dir(session_id)
        filename = media_filename or f"incoming_{media_type}"
        if "." not in filename:
            ext_map = {"image": ".jpg", "document": ".bin", "sticker": ".webp"}
            filename += ext_map.get(media_type, ".bin")
        target_path = workspace / filename
        target_path.write_bytes(raw_bytes)
        logger.info("wa_incoming.media_saved", media_type=media_type, filename=filename)

        if media_type == "image":
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
            mime_map = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
            }
            media_image_mime = mime_map.get(ext, "image/jpeg")
            media_image_b64 = media_data
            media_context = (
                f"\n[Gambar diterima dan ditampilkan di atas. "
                f"File juga tersimpan di /workspace/{filename}]"
            )

        elif media_type == "document":
            ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
            doc_extractable = {".pdf", ".docx", ".pptx", ".txt", ".md", ".csv"}
            if ext in doc_extractable:
                try:
                    from app.core.file_processor import extract_text
                    extracted = await extract_text(
                        content=raw_bytes,
                        filename=filename,
                        content_type=None,
                        mistral_api_key=get_settings().mistral_api_key,
                    )
                    max_chars = get_settings().media_doc_max_chars
                    if len(extracted) > max_chars:
                        extracted = extracted[:max_chars] + f"\n... [dipotong, total {len(extracted)} karakter]"
                    media_context = (
                        f"\n[Dokumen diterima: {filename}]\n"
                        f"Isi dokumen:\n```\n{extracted}\n```"
                    )
                except Exception as exc:
                    logger.warning("wa_incoming.doc_extract_failed", error=str(exc))
                    media_context = f"\n[Dokumen diterima: {filename}, tersimpan di /workspace/{filename}]"
            else:
                media_context = f"\n[Dokumen diterima: {filename}, tersimpan di /workspace/{filename}]"

        elif media_type == "sticker":
            media_context = f"\n[Stiker diterima, tersimpan di /workspace/{filename}]"

    except Exception as exc:
        logger.warning("wa_incoming.media_save_failed", error=str(exc))

    return media_context, media_image_b64, media_image_mime


def is_operator_message(
    from_phone: str,
    reply_target: str | None,
    agent,
) -> bool:
    """
    Cek apakah pesan berasal dari operator berdasarkan operator_ids dan operator_phone legacy.
    """
    escalation_cfg: dict = agent.escalation_config or {}
    operator_phone: str = escalation_cfg.get("operator_phone", "")
    operator_ids: list = getattr(agent, "operator_ids", None) or []

    normalized_op_ids = {normalize_phone(oid) for oid in operator_ids if oid}
    if operator_phone:
        normalized_op_ids.add(normalize_phone(operator_phone))

    if not normalized_op_ids:
        return False

    if normalize_phone(from_phone) in normalized_op_ids:
        return True
    if reply_target and normalize_phone(reply_target) in normalized_op_ids:
        return True
    return False


def get_wa_lookup_user_id(
    *,
    from_phone: str,
    chat_id: str | None,
    is_operator: bool,
    agent,
) -> str:
    """
    Tentukan external_user_id untuk session lookup:
    - Operator → pakai operator_phone (session milik operator sendiri)
    - Pesan grup → pakai group JID (chat_id berakhiran @g.us)
    - DM → pakai nomor pengirim (from_phone)
    """
    escalation_cfg: dict = agent.escalation_config or {}
    operator_phone: str = escalation_cfg.get("operator_phone", "")

    is_group = bool(chat_id and chat_id.endswith("@g.us"))
    if is_operator:
        return operator_phone
    if is_group:
        return chat_id  # type: ignore[return-value]
    return from_phone


def extract_messages_to_user(steps: list[dict]) -> list[dict]:
    """
    Ekstrak pesan yang dikirim ke user dari daftar tool call steps.
    Digunakan untuk response body agar client tahu pesan apa yang terkirim ke user.
    """
    import re

    messages_to_user = []
    for step in steps:
        if step.get("tool") == "reply_to_user":
            msg = step.get("args", {}).get("message") or ""
            if not msg:
                res_str = step.get("result", "")
                m = re.search(r'\[SENT_TO_USER\]\s*(.+)', res_str, re.DOTALL)
                if m:
                    msg = m.group(1).strip()
            if msg:
                messages_to_user.append({"type": "reply_to_user", "message": msg})
        elif step.get("tool") == "send_to_number":
            msg = step.get("args", {}).get("message") or ""
            target = step.get("args", {}).get("phone_or_target") or ""
            if msg:
                messages_to_user.append({"type": "send_to_number", "message": msg, "target": target})
    return messages_to_user

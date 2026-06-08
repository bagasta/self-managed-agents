"""
Helper functions untuk wa_incoming() endpoint di channels.py.

Dipecah dari channels.py (yang tadinya 325+ baris) menjadi fungsi-fungsi
kecil yang bisa di-test dan di-maintain secara independen.
"""
from __future__ import annotations

import base64
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.phone_utils import normalize_phone

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.session import Session

log = structlog.get_logger(__name__)
_ACTIVE_ESCALATION_ROUTE_TTL_SECONDS = 6 * 60 * 60

# Fallback in-memory cache jika Redis mati/tidak ada.
# Menghindari db.execute (lambat) untuk deduplikasi.
_mem_dedup_cache: dict[str, float] = {}
_mem_spam_windows: dict[str, list[float]] = {}


def extract_escalation_case_id(text: str | None) -> str | None:
    """Extract an escalation/spam case id from quoted WhatsApp text."""
    if not text:
        return None
    match = re.search(r"\b(esc_\d+_[a-zA-Z0-9]+)\b", text)
    return match.group(1) if match else None


def extract_escalation_customer_phone(text: str | None) -> str | None:
    """Extract customer phone from an operator-facing escalation quote."""
    if not text:
        return None
    match = re.search(
        r"Nomor\s+customer/user\s*:\s*([+\d][\d \t().-]{6,24})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    normalized = normalize_phone(match.group(1))
    return normalized or None


async def find_session_by_quoted_case(
    agent,
    db: AsyncSession,
    quoted_text: str | None,
):
    """Strict quoted lookup for operator actions that must not fallback to latest."""
    from app.models.session import Session

    case_id = extract_escalation_case_id(quoted_text)
    if case_id:
        result = await db.execute(
            select(Session).where(
                Session.agent_id == agent.id,
                (
                    (Session.metadata_["escalation_case_id"].astext == case_id)
                    | (Session.metadata_["spam_case_id"].astext == case_id)
                ),
            )
        )
        session = result.scalars().first()
        if session:
            return session, case_id

    customer_phone = extract_escalation_customer_phone(quoted_text)
    if not customer_phone:
        return None, case_id

    result = await db.execute(
        select(Session).where(
            Session.agent_id == agent.id,
            Session.external_user_id == customer_phone,
        )
    )
    return result.scalars().first(), case_id or customer_phone


async def find_session_by_quoted_message_id(
    agent,
    db: AsyncSession,
    quoted_stanza_id: str | None,
):
    """Lookup escalation target by WhatsApp quoted message ID."""
    from app.models.session import Session

    message_id = (quoted_stanza_id or "").strip()
    if not message_id:
        return None, None

    result = await db.execute(
        select(Session).where(
            Session.agent_id == agent.id,
            (
                (Session.metadata_["escalation_message_id"].astext == message_id)
                | (Session.metadata_.contains({"escalation_message_ids": [message_id]}))
            ),
        )
    )
    session = result.scalars().first()
    if not session:
        return None, message_id

    meta = session.metadata_ or {}
    case_id = meta.get("escalation_case_id") or meta.get("spam_case_id") or message_id
    return session, case_id


def _route_case_id(session, fallback: str | None = None) -> str | None:
    meta = session.metadata_ or {}
    if isinstance(meta, dict):
        return meta.get("escalation_case_id") or meta.get("spam_case_id") or fallback
    return fallback


async def remember_operator_escalation_route(
    operator_session,
    target_session,
    case_id: str | None,
    db: AsyncSession,
) -> None:
    """Persist the customer target selected by an operator quoted reply."""
    ch = target_session.channel_config if isinstance(target_session.channel_config, dict) else {}
    target = ch.get("user_phone") or target_session.external_user_id or ""
    customer_phone = ch.get("phone_number") or target_session.external_user_id or ""
    route = {
        "target_session_id": str(target_session.id),
        "target": target,
        "case_id": case_id or _route_case_id(target_session),
        "customer_phone": normalize_phone(customer_phone) or customer_phone,
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + _ACTIVE_ESCALATION_ROUTE_TTL_SECONDS,
    }
    meta = dict(operator_session.metadata_ or {})
    meta["active_escalation_reply"] = route
    operator_session.metadata_ = meta
    db.add(operator_session)
    await db.commit()


async def find_session_by_operator_active_route(
    agent,
    db: AsyncSession,
    operator_session,
):
    """Find the escalation customer currently selected in the operator session."""
    from app.models.session import Session

    meta = dict(operator_session.metadata_ or {})
    route = meta.get("active_escalation_reply")
    if not isinstance(route, dict):
        return None, None
    expires_at = route.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at < time.time():
        meta.pop("active_escalation_reply", None)
        operator_session.metadata_ = meta
        db.add(operator_session)
        await db.commit()
        return None, route.get("case_id")

    target_session_id = route.get("target_session_id")
    if not target_session_id:
        return None, route.get("case_id")

    try:
        target_session = await db.get(Session, uuid.UUID(str(target_session_id)))
    except (ValueError, TypeError):
        return None, route.get("case_id")
    if not target_session or target_session.agent_id != agent.id:
        return None, route.get("case_id")
    return target_session, route.get("case_id") or _route_case_id(target_session)


async def check_wa_spam_window(
    *,
    agent_id: str,
    session_id: str,
    sender_id: str,
    limit: int = 5,
    window_seconds: int = 60,
) -> tuple[bool, int]:
    """
    Sliding-window spam detector per agent + user/session.

    Returns (is_spam, count). The first `limit` messages are allowed; message
    number `limit + 1` inside the window triggers auto-disable.
    """
    import time
    from app.core.infra.redis_client import get_redis

    now = time.time()
    identity = normalize_phone(sender_id) or str(session_id)
    key = f"wa_spam:{agent_id}:{identity}"
    r = await get_redis()

    if r:
        try:
            await r.zremrangebyscore(key, 0, now - window_seconds)
            await r.zadd(key, {str(now): now})
            await r.expire(key, window_seconds * 2)
            count = int(await r.zcard(key))
            return count > limit, count
        except Exception as exc:
            log.warning("wa_spam.redis_fail", error=str(exc))

    timestamps = _mem_spam_windows.setdefault(key, [])
    timestamps[:] = [ts for ts in timestamps if now - ts <= window_seconds]
    timestamps.append(now)

    if len(_mem_spam_windows) > 5000:
        stale_keys = [
            k for k, values in _mem_spam_windows.items()
            if not values or now - values[-1] > window_seconds * 2
        ]
        for stale_key in stale_keys:
            _mem_spam_windows.pop(stale_key, None)

    count = len(timestamps)
    return count > limit, count


async def reset_wa_spam_window(
    *,
    agent_id: str,
    session_id: str,
    sender_id: str,
) -> None:
    """Clear the spam sliding window for an agent + customer.

    Called when an operator re-enables AI so the very next customer message
    starts from a clean window instead of re-tripping the still-full window.
    """
    from app.core.infra.redis_client import get_redis

    identity = normalize_phone(sender_id) or str(session_id)
    key = f"wa_spam:{agent_id}:{identity}"
    r = await get_redis()
    if r:
        try:
            await r.delete(key)
        except Exception as exc:
            log.warning("wa_spam.reset_redis_fail", error=str(exc))
    _mem_spam_windows.pop(key, None)


async def is_duplicate_message(
    device_id: str,
    from_phone: str,
    timestamp: int | None,
    db: AsyncSession,
    message_id: str | None = None,
) -> bool:
    """
    Cek apakah pesan WhatsApp sudah pernah diproses dalam 5 menit terakhir.

    Prefer message_id asli WhatsApp. Timestamp WA hanya presisi detik, jadi tidak
    cukup untuk membedakan beberapa pesan valid yang dikirim cepat dalam detik yang sama.
    """
    import time
    from app.core.infra.redis_client import get_redis

    clean_message_id = str(message_id or "").strip()
    if clean_message_id:
        key = f"wa_dedup_msg:{device_id}:{from_phone}:{clean_message_id}"
    else:
        key = f"wa_dedup_ts:{device_id}:{from_phone}:{timestamp}"
    r = await get_redis()
    
    if r:
        try:
            # redis.set returns None if NX fails
            is_new = await r.set(key, "1", ex=300, nx=True)
            return not bool(is_new)
        except Exception as exc:
            log.warning("wa_dedup.redis_fail", error=str(exc))
    
    # Fallback to in-memory TTL mechanism
    now = time.time()
    
    # Prune old cache to avoid memory leak
    if len(_mem_dedup_cache) > 5000:
        keys_to_delete = [k for k, v in _mem_dedup_cache.items() if now - v > 300]
        for k in keys_to_delete:
            _mem_dedup_cache.pop(k, None)
            
    if key in _mem_dedup_cache:
        # Cache hit - duplicate
        return True
        
    _mem_dedup_cache[key] = now
    return False

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
    phone_number: str | None = None,
    sender_name: str | None = None,
) -> tuple:
    """
    Cari session WhatsApp yang sudah ada; buat baru jika belum ada.
    Juga update device_id dan user_phone jika berubah.

    Returns (session, was_created: bool)
    """
    from app.models.session import Session
    from app.core.utils.phone_utils import normalize_phone

    # Normalize agar konsisten dengan format yang dipakai operator_tools
    # (strip '+' dan '@s.whatsapp.net' / '@lid' suffix)
    normalized_lookup = normalize_phone(lookup_user_id)

    # Serialize session lookup/create per agent + WhatsApp identity so spam bursts
    # from the same sender cannot create multiple parallel sessions.
    await db.execute(
        text(
            """
            SELECT pg_advisory_xact_lock(
                hashtext(:agent_key),
                hashtext(:user_key)
            )
            """
        ),
        {
            "agent_key": f"wa_session:{agent.id}",
            "user_key": normalized_lookup,
        },
    )

    result = await db.execute(
        select(Session).where(
            Session.agent_id == agent.id,
            Session.channel_type == "whatsapp",
            Session.external_user_id == normalized_lookup,
        )
    )
    session = result.scalars().first()

    if session:
        # Pastikan device_id dan user_phone selalu up-to-date
        raw_cfg = session.channel_config
        new_config = dict(raw_cfg) if isinstance(raw_cfg, dict) else {}
        changed = (
            new_config.get("device_id") != device_id
            or new_config.get("user_phone") != effective_reply_target
        )
        if phone_number and new_config.get("phone_number") != phone_number:
            changed = True
        if sender_name and new_config.get("sender_name") != sender_name:
            changed = True
        if changed:
            new_config["device_id"] = device_id
            new_config["user_phone"] = effective_reply_target
            if phone_number:
                new_config["phone_number"] = phone_number
            if sender_name:
                new_config["sender_name"] = sender_name
            session.channel_config = new_config
            await db.flush()
        return session, False

    # Buat session baru — simpan dalam format normalized
    cfg: dict = {"user_phone": effective_reply_target, "device_id": device_id}
    if phone_number:
        cfg["phone_number"] = phone_number
    if sender_name:
        cfg["sender_name"] = sender_name
    session = Session(
        agent_id=agent.id,
        external_user_id=normalized_lookup,
        channel_type="whatsapp",
        channel_config=cfg,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session, True


async def find_escalation_context(
    agent,
    db: AsyncSession,
    quoted_text: str | None = None,
    quoted_stanza_id: str | None = None,
    operator_session = None,
) -> tuple[str | None, str | None]:
    """
    Cari session user yang sedang dalam eskalasi aktif untuk agent ini.

    Jika operator me-reply (quote) pesan eskalasi, `quoted_text` berisi teks pesan
    yang di-quote. Kita parse `case_id` dari teks itu untuk routing yang tepat —
    krusial saat ada banyak eskalasi paralel.

    Returns (escalation_user_jid, escalation_context_text)
    - escalation_user_jid: JID user yang dieskalasi (untuk dikirim reply)
    - escalation_context_text: ringkasan pesan terakhir user (untuk context operator)
    """
    from app.models.message import Message
    from app.models.session import Session

    esc_session = None

    routed_by_quoted_case = False

    # Strategy 1: operator reply (quote) pesan eskalasi → match WhatsApp message ID.
    esc_session, case_id = await find_session_by_quoted_message_id(agent, db, quoted_stanza_id)
    if esc_session:
        routed_by_quoted_case = True
        log.info("find_escalation_context.by_message_id", case_id=case_id, session_id=str(esc_session.id))

    # Strategy 2: fallback parse case_id/customer phone from quoted_text.
    if not esc_session:
        esc_session, case_id = await find_session_by_quoted_case(agent, db, quoted_text)
        if esc_session:
            routed_by_quoted_case = True
            log.info("find_escalation_context.by_case_id", case_id=case_id, session_id=str(esc_session.id))

    if esc_session and operator_session is not None:
        await remember_operator_escalation_route(operator_session, esc_session, case_id, db)

    # Strategy 3: continue the customer selected by an earlier quoted operator reply.
    if not esc_session and operator_session is not None:
        esc_session, case_id = await find_session_by_operator_active_route(agent, db, operator_session)
        if esc_session:
            routed_by_quoted_case = True
            log.info("find_escalation_context.by_operator_active_route", case_id=case_id, session_id=str(esc_session.id))

    # Strategy 4: fallback — ambil session eskalasi terbaru.
    # Ini hanya aman untuk konteks non-operator/dev. Untuk operator WhatsApp,
    # target harus berasal dari quoted case/message atau active_route yang masih valid.
    if not esc_session and operator_session is None:
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
        if esc_session:
            case_id = _route_case_id(esc_session)
            if operator_session is not None:
                await remember_operator_escalation_route(operator_session, esc_session, case_id, db)
            log.info("find_escalation_context.by_latest", session_id=str(esc_session.id))

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

    if routed_by_quoted_case:
        route_note = (
            "ROUTING: operator_reply_quoted_escalation. "
            "Operator sedang berada dalam konteks reply eskalasi WhatsApp; pesan operator saat ini ditujukan untuk customer ini."
        )
        escalation_context = f"{route_note}\n{escalation_context or ''}".strip()

    return escalation_user_jid, escalation_context


async def process_wa_media(
    *,
    media_type: str,
    media_data: str,
    media_filename: str | None,
    session_id: uuid.UUID,
    logger: structlog.BoundLogger,
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """
    Proses media (gambar/dokumen/stiker/audio) dari pesan WhatsApp.

    media_type yang didukung:
    - "image"    : gambar (dikirim ke LLM sebagai multimodal input)
    - "document" : dokumen (teks diekstrak jika memungkinkan)
    - "sticker"  : stiker
    - "audio"    : file audio biasa
    - "ptt"      : push-to-talk / voice note

    Returns (media_context, media_image_b64, media_image_mime, media_meta)
    - media_context: teks tambahan untuk disertakan ke LLM
    - media_image_b64: base64 gambar untuk multimodal input (hanya untuk image)
    - media_image_mime: MIME type gambar (hanya untuk image)
    - media_meta: metadata file tersimpan untuk escalation forwarding
    """
    from app.config import get_settings
    from app.core.infra.sandbox import get_workspace_dir

    media_context = ""
    media_image_b64: str | None = None
    media_image_mime: str | None = None
    media_meta: dict[str, Any] | None = None

    try:
        raw_bytes = base64.b64decode(media_data)
        workspace = get_workspace_dir(session_id)
        filename = Path(media_filename or f"incoming_{media_type}").name
        if not filename:
            filename = f"incoming_{media_type}"
        if "." not in filename:
            ext_map = {"image": ".jpg", "document": ".bin", "sticker": ".webp"}
            filename += ext_map.get(media_type, ".bin")
        target_path = workspace / filename
        shared_path = workspace / "shared" / filename
        target_path.write_bytes(raw_bytes)
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.write_bytes(raw_bytes)
        logger.info(
            "wa_incoming.media_saved",
            media_type=media_type,
            filename=filename,
            shared_workspace_path=str(shared_path),
        )
        media_meta = {
            "media_type": media_type,
            "filename": filename,
            "workspace_path": str(target_path),
            "shared_workspace_path": str(shared_path),
            "size_bytes": len(raw_bytes),
        }
        workspace_hint = (
            f"tersimpan di /workspace/{filename}. "
            f"Untuk workflow file/sandbox/subagent, gunakan /workspace/shared/{filename}"
        )

        if media_type == "image":
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
            mime_map = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "webp": "image/webp",
            }
            media_image_mime = mime_map.get(ext, "image/jpeg")
            media_meta["mimetype"] = media_image_mime
            media_image_b64 = media_data
            media_context = (
                f"\n[Gambar diterima dan ditampilkan di atas. "
                f"File juga {workspace_hint}]"
            )

        elif media_type == "document":
            ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
            media_meta["mimetype"] = "application/octet-stream"
            doc_extractable = {".pdf", ".docx", ".pptx", ".txt", ".md", ".csv"}
            if ext in doc_extractable:
                try:
                    from app.core.domain.file_processor import extract_text
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
                        f"\n[Dokumen diterima: {filename}, {workspace_hint}]\n"
                        f"Isi dokumen:\n```\n{extracted}\n```"
                    )
                except Exception as exc:
                    logger.warning("wa_incoming.doc_extract_failed", error=str(exc))
                    media_context = f"\n[Dokumen diterima: {filename}, {workspace_hint}]"
            else:
                media_context = f"\n[Dokumen diterima: {filename}, {workspace_hint}]"

        elif media_type == "sticker":
            media_meta["mimetype"] = "image/webp"
            media_context = f"\n[Stiker diterima, {workspace_hint}]"

        elif media_type in ("audio", "ptt"):
            media_meta["mimetype"] = "audio/ogg"
            # Transkripsi audio/voice note menggunakan openai/gpt-audio-mini via OpenRouter
            from app.core.infra.transcription_service import transcribe_audio

            # Tentukan format audio dari ekstensi filename
            audio_format = "ogg"  # default WhatsApp voice note
            if filename and "." in filename:
                audio_format = filename.rsplit(".", 1)[-1].lower()
                # Normalisasi format yang dikenal
                if audio_format in ("oga",):
                    audio_format = "ogg"
                elif audio_format not in ("ogg", "mp3", "wav", "m4a", "flac", "aac", "mp4"):
                    audio_format = "ogg"  # fallback

            transcript = await transcribe_audio(
                audio_b64=media_data,
                audio_format=audio_format,
                openrouter_api_key=get_settings().openrouter_api_key,
            )
            label = "pesan suara" if media_type == "ptt" else "file audio"
            media_context = (
                f"\n[Sistem: Pengguna mengirim {label}. "
                f"Berikut hasil transkripsi otomatis — balas berdasarkan isi ini]\n"
                f"Transkripsi: {transcript}"
            )
            logger.info(
                "wa_incoming.audio_transcribed",
                media_type=media_type,
                transcript_length=len(transcript),
            )

    except Exception as exc:
        logger.warning("wa_incoming.media_save_failed", error=str(exc))

    return media_context, media_image_b64, media_image_mime, media_meta


def is_operator_message(
    from_phone: str,
    reply_target: str | None,
    agent,
) -> bool:
    """
    Cek apakah pesan berasal dari operator escalation.

    Catatan: owner_external_id adalah pemilik agent dan diperlakukan sebagai
    operator identity. Operator turn tetap hanya aktif jika _should_treat_as_operator_turn
    melihat reply eskalasi/pending draft, jadi owner masih bisa mengetes agent
    tanpa otomatis masuk flow forwarding.
    """
    escalation_cfg: dict = agent.escalation_config or {}
    operator_phone: str = escalation_cfg.get("operator_phone", "")
    operator_ids: list = getattr(agent, "operator_ids", None) or []
    owner_external_id = getattr(agent, "owner_external_id", None)
    normalized_owner = normalize_phone(owner_external_id) if owner_external_id else ""

    normalized_op_ids: set[str] = set()
    if normalized_owner:
        normalized_op_ids.add(normalized_owner)
    if operator_phone:
        normalized_op_ids.add(normalize_phone(operator_phone))
    for oid in operator_ids:
        normalized = normalize_phone(oid)
        if not normalized:
            continue
        normalized_op_ids.add(normalized)

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

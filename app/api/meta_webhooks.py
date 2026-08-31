"""Verified inbound WhatsApp Cloud API webhooks."""
from __future__ import annotations

import base64
import json

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.infra.meta_embedded_signup import verify_webhook_signature
from app.database import AsyncSessionLocal, get_db
from app.models.agent import Agent

router = APIRouter(prefix="/v1/webhooks", tags=["meta-webhooks"])
logger = structlog.get_logger(__name__)


def _account_update_summary(change: dict) -> dict | None:
    """Return a non-sensitive summary of a Hosted Embedded Signup event.

    The `PARTNER_ADDED` event is Meta's server-side signal that a Hosted
    Embedded Signup shared a customer's WABA with this partner.  IDs are
    deliberately excluded from the log: they are customer assets, and the
    webhook itself is not yet enough to associate an event with a particular
    signed agent launch.
    """
    if change.get("field") != "account_update":
        return None
    value = change.get("value") or {}
    event_name = str(value.get("event") or "")
    if not event_name:
        return None
    waba_info = value.get("waba_info") or {}
    return {
        "account_event": event_name,
        "has_waba": bool(waba_info.get("waba_id")),
        "has_business_portfolio": bool(waba_info.get("owner_business_id")),
    }


async def _send_cloud_reply(session, reply: str) -> None:
    """Deliver an agent reply through the Cloud API config on the session."""
    if not reply or session.ai_disabled:
        return
    from app.core.infra.channel_service import send_message

    await send_message(
        channel_type="whatsapp",
        channel_config=dict(session.channel_config or {}),
        text=reply,
    )


@router.get("/meta")
async def verify_meta_webhook(hub_mode: str | None = Query(None, alias="hub.mode"), hub_verify_token: str | None = Query(None, alias="hub.verify_token"), hub_challenge: str | None = Query(None, alias="hub.challenge")) -> Response:
    from app.config import get_settings
    if hub_mode == "subscribe" and hub_challenge and hub_verify_token and hub_verify_token == get_settings().meta_webhook_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")


async def _process(agent_id: str, phone_number_id: str, message: dict, sender_name: str) -> None:
    from app.api.wa_helpers import find_or_create_wa_session
    from app.core.engine.agent_runner import run_agent
    from app.core.infra.channel_service import decrypt_value
    from app.core.infra.wa_cloud_client import download_media, mark_message_read
    from app.models.agent import Agent
    async with AsyncSessionLocal() as db:
        agent = await db.get(Agent, agent_id)
        if agent is None or not agent.wa_access_token_encrypted:
            return
        sender = str(message.get("from") or "")
        message_type = str(message.get("type") or "")
        text = str((message.get("text") or {}).get("body") or "").strip()
        media = message.get(message_type) or {}
        if not sender or message_type not in {"text", "image", "document"}:
            return
        session, _ = await find_or_create_wa_session(agent=agent, lookup_user_id=sender, effective_reply_target=sender, device_id=f"meta:{phone_number_id}", db=db, is_operator=False, phone_number=sender, sender_name=sender_name)
        config = dict(session.channel_config or {})
        config["meta_access_token"] = agent.wa_access_token_encrypted
        config["meta_phone_number_id"] = phone_number_id
        session.channel_config = config
        token = decrypt_value(agent.wa_access_token_encrypted)
        try:
            await mark_message_read(phone_number_id, str(message.get("id") or ""), token)
        except Exception:
            pass
        media_image_b64: str | None = None
        media_image_mime: str | None = None
        current_attachment_name: str | None = None
        if message_type in {"image", "document"}:
            media_id = str(media.get("id") or "")
            if not media_id:
                logger.warning("meta_webhook.media_missing_id", media_type=message_type)
                return
            try:
                raw, mime_type = await download_media(media_id, token)
                filename = str(media.get("filename") or "").strip() or None
                if not filename:
                    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime_type, ".bin")
                    filename = f"incoming_{message_type}{extension}"
                from app.api.wa_helpers import process_wa_media
                media_context, media_image_b64, media_image_mime, media_meta = await process_wa_media(
                    media_type=message_type,
                    media_data=base64.b64encode(raw).decode("ascii"),
                    media_filename=filename,
                    session_id=session.id,
                    logger=logger,
                    arthur_model_routing=bool(getattr(agent, "is_arthur", False)),
                )
                current_attachment_name = (media_meta or {}).get("filename") or filename
                caption = str(media.get("caption") or "").strip()
                text = (caption or f"Pengguna mengirim {message_type}.") + media_context
            except Exception as exc:
                logger.warning("meta_webhook.media_download_failed", media_type=message_type, error_type=type(exc).__name__)
                text = (
                    f"[Lampiran {message_type} diterima tetapi tidak dapat diunduh dari WhatsApp. "
                    "Minta pengguna mengirim ulang lampiran tersebut.]"
                )
        result = await run_agent(
            agent_model=agent,
            session=session,
            user_message=text,
            db=db,
            sender_name=sender_name,
            media_image_b64=media_image_b64,
            media_image_mime=media_image_mime,
            current_attachment_name=current_attachment_name,
        )
        await _send_cloud_reply(session, str(result.get("reply") or ""))
        await db.commit()


@router.post("/meta")
async def receive_meta_webhook(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)) -> dict:
    body = await request.body()
    if not verify_webhook_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc
    if payload.get("object") != "whatsapp_business_account":
        return {"ok": True, "ignored": True}
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            account_update = _account_update_summary(change)
            if account_update is not None:
                logger.info("meta_webhook.account_update", **account_update)
                value = change.get("value") or {}
                info = value.get("waba_info") or {}
                if value.get("event") == "PARTNER_ADDED" and info.get("waba_id") and info.get("owner_business_id"):
                    from app.api.meta_signup import record_hosted_signup_handoff
                    matched = await record_hosted_signup_handoff(str(info["waba_id"]), str(info["owner_business_id"]))
                    logger.info("meta_webhook.hosted_signup_handoff", matched=matched)
                continue
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "")
            if change.get("field") != "messages" or not phone_number_id:
                continue
            agent_id = (await db.execute(select(Agent.id).where(Agent.wa_phone_number_id == phone_number_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
            if agent_id is None:
                continue
            contact_names = {str(item.get("wa_id") or ""): str(item.get("profile", {}).get("name") or "") for item in value.get("contacts") or []}
            for message in value.get("messages") or []:
                if message.get("type") in {"text", "image", "document"}:
                    background_tasks.add_task(_process, str(agent_id), phone_number_id, message, contact_names.get(str(message.get("from") or ""), ""))
    return {"ok": True}

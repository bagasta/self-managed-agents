"""Meta WhatsApp Cloud API Webhook Handler.

Endpoint:
  GET  /v1/webhooks/meta  — Webhook verification from Meta (hub.mode=subscribe)
  POST /v1/webhooks/meta  — Incoming WhatsApp Cloud API messages and status updates
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.wa_helpers import find_agent_by_phone_number_id, find_or_create_wa_session
from app.config import get_settings
from app.core.engine.agent_runner import run_agent
from app.core.infra.channel_service import send_message
from app.core.infra.meta_embedded_signup import validate_webhook_verification
from app.core.infra.wa_cloud_client import mark_message_read
from app.database import get_db

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["meta-webhooks"])


@router.get("/meta")
async def verify_meta_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """GET verification endpoint called by Meta when configuring webhooks."""
    challenge = validate_webhook_verification(hub_mode, hub_verify_token, hub_challenge)
    if challenge:
        logger.info("meta_webhook.verification_success")
        return Response(content=challenge, media_type="text/plain")
    logger.warning("meta_webhook.verification_failed", mode=hub_mode)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification token mismatch")


@router.post("/meta")
async def handle_meta_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """POST webhook endpoint for incoming Meta Cloud API messages and status events."""
    body = await request.json()
    logger.debug("meta_webhook.received", body=body)

    if body.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    entries = body.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            if change.get("field") != "messages":
                continue

            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")
            if not phone_number_id:
                continue

            # Lookup agent bound to this Meta phone_number_id
            agent = await find_agent_by_phone_number_id(phone_number_id, db)
            if not agent:
                logger.warning("meta_webhook.agent_not_found", phone_number_id=phone_number_id)
                continue

            messages = value.get("messages", [])
            contacts = value.get("contacts", [])
            sender_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

            for msg in messages:
                await _process_incoming_cloud_message(
                    agent=agent,
                    phone_number_id=phone_number_id,
                    msg=msg,
                    sender_name=sender_name,
                    db=db,
                )

    return {"status": "ok"}


async def _process_incoming_cloud_message(
    *,
    agent,
    phone_number_id: str,
    msg: dict,
    sender_name: str,
    db: AsyncSession,
) -> None:
    msg_id = msg.get("id", "")
    from_phone = msg.get("from", "")
    msg_type = msg.get("type", "text")

    if not from_phone:
        return

    # Text content extraction
    message_text = ""
    if msg_type == "text":
        message_text = msg.get("text", {}).get("body", "")
    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if interactive.get("type") == "button_reply":
            message_text = interactive.get("button_reply", {}).get("title", "")
        elif interactive.get("type") == "list_reply":
            message_text = interactive.get("list_reply", {}).get("title", "")
    elif msg_type in {"image", "document", "audio", "video"}:
        caption = msg.get(msg_type, {}).get("caption", "")
        message_text = caption or f"[{msg_type.upper()} RECEIVED]"

    if not message_text.strip():
        return

    # Use virtual device_id prefix for Cloud API: "cloud_{phone_number_id}"
    virtual_device_id = f"cloud_{phone_number_id}"

    # Get or create WhatsApp session for customer
    session, was_created = await find_or_create_wa_session(
        agent=agent,
        lookup_user_id=from_phone,
        effective_reply_target=from_phone,
        device_id=virtual_device_id,
        db=db,
        is_operator=False,
        phone_number=from_phone,
        sender_name=sender_name,
    )

    # Run agent runner pipeline
    try:
        run_result = await run_agent(
            agent_id=agent.id,
            session_id=session.id,
            user_message=message_text,
            db=db,
        )
        agent_reply = run_result.get("reply", "")
        if agent_reply and not session.ai_disabled:
            # Deliver response via channel_service
            await send_message(
                channel_type="whatsapp",
                channel_config={"device_id": virtual_device_id, "user_phone": from_phone},
                text=agent_reply,
            )
    except Exception as exc:
        logger.error(
            "meta_webhook.agent_run_failed",
            agent_id=str(agent.id),
            from_phone=from_phone,
            error=str(exc),
        )

"""Verified inbound WhatsApp Cloud API webhooks."""
from __future__ import annotations

import json

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
    from app.core.infra.channel_service import decrypt_value, encrypt_value
    from app.core.infra.wa_cloud_client import mark_message_read
    from app.models.agent import Agent
    async with AsyncSessionLocal() as db:
        agent = await db.get(Agent, agent_id)
        if agent is None or not agent.wa_access_token_encrypted:
            return
        sender = str(message.get("from") or "")
        text = str((message.get("text") or {}).get("body") or "").strip()
        if not sender or not text:
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
        result = await run_agent(agent_model=agent, session=session, user_message=text, db=db, sender_name=sender_name)
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
                if message.get("type") == "text":
                    background_tasks.add_task(_process, str(agent_id), phone_number_id, message, contact_names.get(str(message.get("from") or ""), ""))
    return {"ok": True}

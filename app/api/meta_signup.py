"""Secure Meta Embedded Signup endpoint for a caller-owned assistant."""
from __future__ import annotations

import html
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.infra.channel_service import encrypt_value
from app.core.infra.meta_embedded_signup import (
    build_signup_state,
    exchange_code_for_token,
    get_waba_phone_numbers,
    subscribe_waba_to_webhooks,
    verify_signup_state,
)
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent

router = APIRouter(prefix="/v1/meta/signup", tags=["meta-signup"])


class EmbeddedSignupCompleteRequest(BaseModel):
    state: str = Field(..., min_length=32)
    code: str = Field(..., min_length=3)
    waba_id: str = Field(..., min_length=1, max_length=64)
    phone_number_id: str = Field(..., min_length=1, max_length=64)


def _agent_for_state(state_value: str) -> uuid.UUID:
    try:
        return verify_signup_state(state_value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signup link invalid or expired") from exc


@router.post("/links/{agent_id}")
async def create_signup_link(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict:
    """Dashboard/admin entry point; browser launch itself uses the signed state."""
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    from app.config import get_settings
    settings = get_settings()
    if not settings.app_public_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="APP_PUBLIC_URL is required for Meta Embedded Signup")
    try:
        state = build_signup_state(agent.id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {
        "agent_id": str(agent.id),
        "signup_url": f"{settings.app_public_url.rstrip('/')}/v1/meta/signup/l/{state}",
        "expires_in_seconds": settings.meta_signup_state_ttl_seconds,
    }


@router.get("/launch", response_class=HTMLResponse)
async def launch(state: str = Query(..., min_length=32), db: AsyncSession = Depends(get_db)) -> str:
    agent_id = _agent_for_state(state)
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    from app.config import get_settings
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_embedded_signup_config_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Meta Embedded Signup is not configured")
    name = html.escape(agent.name)
    # The code comes from FB.login; WABA and phone-number IDs arrive via the
    # official WA_EMBEDDED_SIGNUP postMessage event in either order.
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect WhatsApp</title></head>
<body><main><h1>Connect WhatsApp Business</h1><p>Connect <strong>{name}</strong> through Meta's official Embedded Signup.</p><button id="connect">Continue with Meta</button><p id="status" role="status"></p></main>
<script>const state={state!r};let code='',waba='',phone='';
const statusEl=document.getElementById('status'); const ready=()=>code&&waba&&phone;
async function complete(){{if(!ready())return;statusEl.textContent='Saving connection…';const r=await fetch('/v1/meta/signup/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state:state,code:code,waba_id:waba,phone_number_id:phone}})}});const data=await r.json();statusEl.textContent=r.ok?'Connected. You can return to WhatsApp.':(data.detail||'Connection failed');}}
window.addEventListener('message',e=>{{if(e.origin!=='https://www.facebook.com'&&e.origin!=='https://web.facebook.com')return;let d=e.data;try{{if(typeof d==='string')d=JSON.parse(d)}}catch(_e){{return}}if(d&&d.type==='WA_EMBEDDED_SIGNUP'){{waba=d.data?.waba_id||waba;phone=d.data?.phone_number_id||phone;complete();}}}});
window.fbAsyncInit=()=>FB.init({{appId:{settings.meta_app_id!r},cookie:true,xfbml:true,version:{settings.meta_graph_api_version!r}}});
document.getElementById('connect').onclick=()=>FB.login(r=>{{code=r.authResponse?.code||'';if(!code){{statusEl.textContent='Meta login was cancelled.';return}}complete();}},{{config_id:{settings.meta_embedded_signup_config_id!r},response_type:'code',override_default_response_type:true,extras:{{setup:{{}}}}}});
</script><script async defer crossorigin="anonymous" src="https://connect.facebook.net/en_US/sdk.js"></script></body></html>'''


@router.get("/l/{state}", response_class=HTMLResponse)
async def launch_short(state: str, db: AsyncSession = Depends(get_db)) -> str:
    """Compact, WhatsApp-friendly alias for the Embedded Signup launch URL."""
    return await launch(state=state, db=db)


@router.post("/complete")
async def complete(payload: EmbeddedSignupCompleteRequest, db: AsyncSession = Depends(get_db)) -> dict:
    agent_id = _agent_for_state(payload.state)
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    existing = (await db.execute(select(Agent.id).where(Agent.wa_phone_number_id == payload.phone_number_id, Agent.id != agent.id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This WhatsApp number is already connected to another assistant")
    try:
        token = await exchange_code_for_token(payload.code)
        numbers = await get_waba_phone_numbers(payload.waba_id, token)
        selected = next((item for item in numbers if str(item.get("id")) == payload.phone_number_id), None)
        if selected is None:
            raise ValueError("Selected phone number does not belong to the shared WABA")
        encrypted = encrypt_value(token)
        if not encrypted.startswith("enc:"):
            raise RuntimeError("CHANNEL_SECRET_KEY must be configured before Cloud API credentials can be stored")
        await subscribe_waba_to_webhooks(payload.waba_id, token)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    agent.wa_phone_number_id = payload.phone_number_id
    agent.wa_waba_id = payload.waba_id
    agent.wa_access_token_encrypted = encrypted
    agent.wa_display_phone = str(selected.get("display_phone_number") or "") or None
    agent.wa_business_name = str(selected.get("verified_name") or "") or None
    agent.wa_connection_type = "cloud_api"
    agent.channel_type = "whatsapp"
    agent.version += 1
    await db.commit()
    return {"ok": True, "agent_id": str(agent.id), "connection_type": "cloud_api", "display_phone": agent.wa_display_phone}

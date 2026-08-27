"""Secure Meta Embedded Signup endpoint for a caller-owned assistant."""
from __future__ import annotations

import hashlib
import html
import json
import uuid
from typing import Literal
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.infra.channel_service import decrypt_value, encrypt_value
from app.core.infra.meta_embedded_signup import (
    build_signup_state,
    exchange_code_for_token,
    get_business_token,
    get_shared_waba_ids,
    get_user_businesses,
    get_waba_phone_numbers,
    subscribe_waba_to_webhooks,
    verify_signup_state,
)
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent

router = APIRouter(prefix="/v1/meta/signup", tags=["meta-signup"])
logger = structlog.get_logger(__name__)


def _callback_url() -> str:
    from app.config import get_settings

    settings = get_settings()
    if not settings.app_public_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="APP_PUBLIC_URL is required")
    return f"{settings.app_public_url.rstrip('/')}/v1/meta/signup/callback"


def _handoff_key(state_value: str) -> str:
    return "meta-signup-handoff:" + hashlib.sha256(state_value.encode()).hexdigest()


def _identity_key(nonce: str) -> str:
    return "meta-signup-identity:" + hashlib.sha256(nonce.encode()).hexdigest()


def _identity_businesses_key(state_value: str) -> str:
    return "meta-signup-identity-businesses:" + hashlib.sha256(state_value.encode()).hexdigest()


def _hosted_business_key(business_id: str) -> str:
    return "meta-signup-hosted-business:" + hashlib.sha256(business_id.encode()).hexdigest()


def _identity_callback_url() -> str:
    from app.config import get_settings
    settings = get_settings()
    return f"{settings.app_public_url.rstrip('/')}/v1/meta/signup/identity/callback"


async def _bind_hosted_business(state_value: str, business_id: str) -> None:
    from app.config import get_settings
    from app.core.infra.redis_client import get_redis
    redis = await get_redis()
    if redis is None:
        raise HTTPException(status_code=503, detail="Temporary signup storage is unavailable")
    reserved = await redis.set(_hosted_business_key(business_id), state_value, ex=get_settings().meta_signup_state_ttl_seconds, nx=True)
    if not reserved:
        raise HTTPException(status_code=409, detail="This Meta business already has a signup in progress")


def _hosted_signup_url() -> str:
    from app.config import get_settings
    settings = get_settings()
    extras = json.dumps({"sessionInfoVersion": "3", "version": "v4"}, separators=(",", ":"))
    return "https://business.facebook.com/messaging/whatsapp/onboard/?" + urlencode({
        "app_id": settings.meta_app_id,
        "config_id": settings.meta_embedded_signup_config_id,
        "extras": extras,
    })


def _callback_state(state_value: str | None, cookie_state: str | None) -> str:
    """Resolve callback state from the signed query or its HTTP-only browser binding."""
    # Facebook Login may include an SDK-managed `state` query parameter. The
    # HTTP-only value is the authoritative binding for browser redirects.
    resolved = cookie_state or state_value
    if not resolved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signup callback session is missing or expired")
    _agent_for_state(resolved)
    return resolved


async def _save_handoff(state_value: str, handoff: dict) -> None:
    from app.config import get_settings
    from app.core.infra.redis_client import get_redis

    redis = await get_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Temporary signup storage is unavailable")
    await redis.set(_handoff_key(state_value), json.dumps(handoff), ex=get_settings().meta_signup_state_ttl_seconds)


async def _get_handoff(state_value: str) -> dict | None:
    from app.core.infra.redis_client import get_redis

    redis = await get_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Temporary signup storage is unavailable")
    raw = await redis.get(_handoff_key(state_value))
    return json.loads(raw) if raw else None


async def _clear_handoff(state_value: str) -> None:
    from app.core.infra.redis_client import get_redis

    redis = await get_redis()
    if redis is not None:
        await redis.delete(_handoff_key(state_value))


async def record_hosted_signup_handoff(waba_id: str, business_id: str) -> bool:
    """Match a signed mobile launch to Meta's PARTNER_ADDED webhook.

    The customer never sees the business ID.  It was selected through the
    short-lived User-token flow and is only used as the server-side join key.
    """
    from app.core.infra.redis_client import get_redis
    redis = await get_redis()
    if redis is None:
        return False
    state_value = await redis.getdel(_hosted_business_key(business_id))
    if not state_value:
        return False
    try:
        _agent_for_state(state_value)
        token = await get_business_token(business_id)
        encrypted = encrypt_value(token)
        if not encrypted.startswith("enc:"):
            raise RuntimeError("Credential storage is not configured")
        candidates = []
        for number in await get_waba_phone_numbers(waba_id, token):
            if number.get("id"):
                candidates.append({
                    "waba_id": waba_id,
                    "phone_number_id": str(number["id"]),
                    "display_phone": str(number.get("display_phone_number") or ""),
                    "business_name": str(number.get("verified_name") or ""),
                })
        if not candidates:
            raise ValueError("No phone number returned by Meta")
        await _save_handoff(state_value, {"token": encrypted, "candidates": candidates})
        return True
    except Exception as exc:
        logger.warning("meta_signup.hosted_handoff_failed", error_type=type(exc).__name__)
        return False


class EmbeddedSignupCompleteRequest(BaseModel):
    state: str = Field(..., min_length=32)
    code: str = Field(..., min_length=3)
    waba_id: str = Field(..., min_length=1, max_length=64)
    phone_number_id: str | None = Field(None, min_length=1, max_length=64)
    connection_mode: Literal["cloud_api_new", "coexistence"] = "cloud_api_new"


class EmbeddedSignupActivationRequest(BaseModel):
    """One-time WhatsApp registration request for a signed signup link."""

    state: str = Field(..., min_length=32)
    pin: str = Field(..., pattern=r"^\d{6}$")


class EmbeddedSignupHandoffCompleteRequest(BaseModel):
    state: str = Field(..., min_length=32)
    waba_id: str = Field(..., min_length=1, max_length=64)
    phone_number_id: str = Field(..., min_length=1, max_length=64)


class EmbeddedSignupTelemetryRequest(BaseModel):
    """Non-sensitive browser lifecycle marker for diagnosing Meta SDK launches."""

    state: str = Field(..., min_length=32)
    event: Literal[
        "page_loaded",
        "sdk_ready",
        "launch_clicked",
        "sdk_launch_requested",
        "sdk_callback_with_code",
        "sdk_callback_without_code",
        "session_event_received",
        "sdk_load_failed",
        "external_browser_requested",
    ]
    mobile: bool = False


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
        "signup_url": f"{settings.app_public_url.rstrip('/')}/v1/meta/signup/launch?state={state}",
        "expires_in_seconds": settings.meta_signup_state_ttl_seconds,
    }


@router.get("/callback", response_class=HTMLResponse)
async def oauth_callback(
    request: Request,
    state: str | None = Query(None, max_length=512),
    code: str | None = Query(None, min_length=3),
    error: str | None = Query(None),
) -> HTMLResponse:
    """Receive mobile OAuth return and retain only encrypted server-side data."""
    state = _callback_state(state, request.cookies.get("meta_signup_state"))
    if error or not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Meta authorization was not completed")
    try:
        token = await exchange_code_for_token(code, redirect_uri=_callback_url())
        encrypted = encrypt_value(token)
        if not encrypted.startswith("enc:"):
            raise RuntimeError("CHANNEL_SECRET_KEY must be configured before Cloud API credentials can be stored")
        candidates: list[dict[str, str]] = []
        for waba_id in await get_shared_waba_ids(token):
            for number in await get_waba_phone_numbers(waba_id, token):
                phone_number_id = str(number.get("id") or "")
                if phone_number_id:
                    candidates.append({
                        "waba_id": waba_id,
                        "phone_number_id": phone_number_id,
                        "display_phone": str(number.get("display_phone_number") or ""),
                        "business_name": str(number.get("verified_name") or ""),
                    })
        if not candidates:
            raise ValueError("Meta did not return a shared WhatsApp phone number")
        await _save_handoff(state, {"token": encrypted, "candidates": candidates})
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("cloud_api.mobile_handoff_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to prepare the Meta signup result") from exc
    # With a popup, Facebook returns to this same-origin page in the popup.
    # Tell its opener to resume the signed launch rather than leaving the
    # customer on a blank callback page.  On mobile there is normally no
    # opener, so the current top-level tab simply resumes the launch page.
    launch_url = f"/v1/meta/signup/l/{state}"
    encoded_url = json.dumps(launch_url)
    return HTMLResponse(
        "<!doctype html><title>Melanjutkan koneksi WhatsApp…</title>"
        "<script>const next=" + encoded_url + ";"
        "if(window.opener&&!window.opener.closed){window.opener.location.replace(next);window.close();}"
        "else{window.location.replace(next);}</script>"
    )


@router.get("/identity/start")
async def identity_start(request: Request, state: str = Query(..., min_length=32)) -> RedirectResponse:
    """Use a mobile-safe full-page Login for Business redirect before Hosted ES."""
    _agent_for_state(state)
    from app.config import get_settings
    from app.core.infra.redis_client import get_redis
    settings = get_settings()
    if not settings.meta_business_identity_config_id:
        raise HTTPException(status_code=503, detail="Mobile Meta business identification is not configured")
    redis = await get_redis()
    if redis is None:
        raise HTTPException(status_code=503, detail="Temporary signup storage is unavailable")
    nonce = uuid.uuid4().hex
    await redis.set(_identity_key(nonce), state, ex=settings.meta_signup_state_ttl_seconds)
    target = "https://www.facebook.com/" + settings.meta_graph_api_version + "/dialog/oauth?" + urlencode({
        "client_id": settings.meta_app_id,
        "redirect_uri": _identity_callback_url(),
        "state": nonce,
        "config_id": settings.meta_business_identity_config_id,
        "response_type": "code",
        "override_default_response_type": "true",
    })
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("meta_signup_state", state, max_age=settings.meta_signup_state_ttl_seconds, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.get("/identity/callback", response_class=HTMLResponse)
async def identity_callback(request: Request, state: str = Query(..., min_length=16), code: str | None = Query(None, min_length=3)) -> HTMLResponse:
    from app.core.infra.redis_client import get_redis
    redis = await get_redis()
    if redis is None or not code:
        raise HTTPException(status_code=400, detail="Meta business identification was not completed")
    signed_state = await redis.getdel(_identity_key(state))
    if not signed_state or signed_state != request.cookies.get("meta_signup_state"):
        raise HTTPException(status_code=403, detail="Meta business identification expired or is invalid")
    _agent_for_state(signed_state)
    try:
        user_token = await exchange_code_for_token(code, redirect_uri=_identity_callback_url())
        businesses = await get_user_businesses(user_token)
    except Exception as exc:
        logger.warning("meta_signup.identity_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Unable to identify the Meta business") from exc
    if not businesses:
        raise HTTPException(status_code=400, detail="No Meta business was available for this Facebook account")
    await redis.set(_identity_businesses_key(signed_state), json.dumps(businesses), ex=3600)
    options = "".join(f'<button name="choice" value="{index}" type="submit">{html.escape(item["name"])}</button>' for index, item in enumerate(businesses))
    return HTMLResponse('<!doctype html><title>Pilih bisnis Meta</title><form method="get" action="/v1/meta/signup/identity/select"><h1>Pilih bisnis Meta</h1><p>Pilih bisnis yang akan dihubungkan ke WhatsApp. Kami tidak menampilkan atau meminta ID bisnis.</p>' + options + '</form>')


@router.get("/identity/select")
async def identity_select(request: Request, choice: int = Query(..., ge=0, le=100)) -> RedirectResponse:
    from app.core.infra.redis_client import get_redis
    state = request.cookies.get("meta_signup_state")
    if not state:
        raise HTTPException(status_code=403, detail="Signup session expired")
    _agent_for_state(state)
    redis = await get_redis()
    raw = await redis.get(_identity_businesses_key(state)) if redis else None
    businesses = json.loads(raw) if raw else []
    if choice >= len(businesses):
        raise HTTPException(status_code=400, detail="Invalid Meta business selection")
    await _bind_hosted_business(state, businesses[choice]["id"])
    await redis.delete(_identity_businesses_key(state))
    return RedirectResponse(_hosted_signup_url(), status_code=status.HTTP_303_SEE_OTHER)


@router.get("/handoff/status")
async def handoff_status(state: str = Query(..., min_length=32)) -> dict:
    _agent_for_state(state)
    handoff = await _get_handoff(state)
    return {"ready": bool(handoff), "candidates": (handoff or {}).get("candidates", [])}


@router.post("/telemetry", status_code=status.HTTP_204_NO_CONTENT)
async def signup_telemetry(payload: EmbeddedSignupTelemetryRequest) -> Response:
    """Record only lifecycle markers; never codes, tokens, IDs, or browser URLs."""
    _agent_for_state(payload.state)
    logger.info("meta_signup.client_event", client_event=payload.event, mobile=payload.mobile)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _render_standard_signup_page(*, state: str, name: str, app_id: str, config_id: str, ttl_ms: int) -> str:
    """Render the documented Facebook JS SDK launch, including Coexistence.

    Only the short-lived code and non-secret asset IDs are retained in
    sessionStorage.  The signed state remains the server-side authority.
    """
    return rf'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hubungkan WhatsApp · {name}</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif}}body{{margin:0;min-height:100vh;background:#08111f;color:#edf3ff}}main{{max-width:560px;margin:auto;padding:32px 20px}}section{{padding:28px;border:1px solid #29466e;border-radius:20px;background:#0f1b30}}h1{{font-size:32px;margin:12px 0}}p{{color:#c6d3e6;line-height:1.5}}button,a.button{{display:block;width:100%;box-sizing:border-box;margin-top:12px;padding:15px;border:0;border-radius:12px;background:#1877f2;color:#fff;font:700 16px inherit;text-align:center;text-decoration:none;cursor:pointer}}button.secondary{{background:#243956;border:1px solid #4c6485}}button:disabled{{opacity:.6;cursor:wait}}#status{{min-height:24px;text-align:center}}#status[data-state=error]{{color:#ff9ca6}}#status[data-state=success]{{color:#68e3ac}}#in-app,#selection{{display:none;padding:12px;margin-top:16px;border-radius:10px;background:#3b2b12;color:#ffe0a4;font-size:14px}}#selection{{background:#10243f;color:#d9e8ff}}#selection button{{background:#243956;border:1px solid #4c6485}}</style></head><body><main><section><small>◉ META EMBEDDED SIGNUP</small><h1>Hubungkan WhatsApp Business</h1><p>Sambungkan nomor resmi untuk <strong>{name}</strong>.</p><p><strong>Pakai WhatsApp Business yang sudah ada</strong> membuat AI Arthur dan tim Anda tetap dapat membalas dari nomor yang sama. <strong>Nomor baru</strong> memakai WhatsApp Cloud API khusus.</p><button id="coexist" type="button">Pakai WhatsApp Business yang sudah ada</button><button id="new-number" class="secondary" type="button">Gunakan nomor baru khusus Cloud API</button><div id="in-app"><strong>Buka di browser eksternal.</strong> Browser bawaan aplikasi seperti WhatsApp, Instagram, atau Facebook sering memblokir halaman Login for Business Meta.<a id="external-browser" class="button" target="_blank" rel="noopener">Buka di Chrome / Safari</a></div><section id="selection" aria-live="polite"><strong>Pilih nomor WhatsApp yang akan dihubungkan</strong><div id="selection-options"></div></section><p id="status" role="status" aria-live="polite"></p></section></main><script>
const state={state!r},appId={app_id!r},configId={config_id!r},ttlMs={ttl_ms!r};
const key=`meta-es:${{state}}`,launchUrl=`/v1/meta/signup/l/${{encodeURIComponent(state)}}`,statusEl=document.querySelector('#status'),coexist=document.querySelector('#coexist'),newNumber=document.querySelector('#new-number'),selection=document.querySelector('#selection'),selectionOptions=document.querySelector('#selection-options');let sdkReady=false,submitting=false,fragments=read();
document.querySelector('#external-browser').href=launchUrl;
function read(){{try{{const x=JSON.parse(sessionStorage.getItem(key)||'{{}}');return x&&x.expires_at>Date.now()?x:{{}}}}catch(_e){{return {{}}}}}}
function save(){{fragments.expires_at=Date.now()+ttlMs;sessionStorage.setItem(key,JSON.stringify(fragments));}}
function clear(){{sessionStorage.removeItem(key)}}
function status(message,type=''){{statusEl.textContent=message;statusEl.dataset.state=type}}
function setButtons(disabled){{coexist.disabled=disabled;newNumber.disabled=disabled}}
function telemetry(event){{fetch('/v1/meta/signup/telemetry',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state,event,mobile:matchMedia('(max-width:768px)').matches}}),keepalive:true}}).catch(()=>{{}})}}
function isMetaOrigin(origin){{return origin==='https://www.facebook.com'||origin==='https://web.facebook.com'||origin==='https://business.facebook.com'}}
function applySession(value){{let data=value;try{{if(typeof value==='string')data=JSON.parse(value)}}catch(_e){{return}}if(!data||data.type!=='WA_EMBEDDED_SIGNUP')return;const d=data.data||{{}};if(d.waba_id)fragments.waba_id=String(d.waba_id);if(d.phone_number_id)fragments.phone_number_id=String(d.phone_number_id);if(String(data.event||'').toUpperCase()==='FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING')fragments.connection_mode='coexistence';save();telemetry('session_event_received');finish();}}
window.addEventListener('message',event=>{{if(isMetaOrigin(event.origin))applySession(event.data)}});
async function selectNumber(candidate){{setButtons(true);status('Menyimpan nomor WhatsApp…');try{{const r=await fetch('/v1/meta/signup/handoff/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state,waba_id:candidate.waba_id,phone_number_id:candidate.phone_number_id}})}});const d=await r.json();if(!r.ok)throw Error(d.detail||'Koneksi belum berhasil.');selection.style.display='none';clear();status('Terhubung via Meta Cloud API dan WhatsApp Business App.','success')}}catch(e){{setButtons(false);status(e.message,'error')}}}}
function chooseNumber(candidates){{selectionOptions.replaceChildren();candidates.forEach(candidate=>{{const button=document.createElement('button');button.type='button';button.textContent=candidate.display_phone||candidate.business_name||'Nomor WhatsApp';button.onclick=()=>selectNumber(candidate);selectionOptions.appendChild(button)}});selection.style.display='block';status('Meta selesai. Pilih nomor WhatsApp untuk dilanjutkan.')}}
function finish(){{const coex=fragments.connection_mode==='coexistence';if(submitting||!fragments.code||!fragments.waba_id||(!coex&&!fragments.phone_number_id)){{if(fragments.code||fragments.waba_id)status('Menunggu konfirmasi Meta melengkapi koneksi…');return}}submitting=true;setButtons(true);status('Menyimpan koneksi WhatsApp…');fetch('/v1/meta/signup/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state,code:fragments.code,waba_id:fragments.waba_id,phone_number_id:fragments.phone_number_id,connection_mode:fragments.connection_mode||'cloud_api_new'}})}}).then(async r=>{{const d=await r.json();if(!r.ok)throw Error(d.detail||'Koneksi belum berhasil.');if(d.selection_required){{submitting=false;chooseNumber(d.candidates||[]);return}}clear();status(d.activation_required?'Koneksi tersimpan. Selesaikan PIN enam digit untuk mengaktifkan nomor baru di Meta.':'Terhubung via Meta Cloud API dan WhatsApp Business App.','success')}}).catch(e=>{{submitting=false;setButtons(false);status(e.message,'error')}})}}
function login(mode){{if(!sdkReady){{status('Halaman Login for Business Meta belum siap. Coba lagi atau buka di Chrome / Safari.','error');return}}fragments={{connection_mode:mode,expires_at:Date.now()+ttlMs}};save();telemetry('sdk_launch_requested');setButtons(true);status('Membuka Login for Business Meta…');const extras=mode==='coexistence'?{{setup:{{}},featureType:'whatsapp_business_app_onboarding',sessionInfoVersion:'3'}}:{{setup:{{}}}};/* Must stay directly in the trusted user click; do not await before FB.login. The v4 configuration in Meta Builder owns Cloud API version and permissions. */FB.login(response=>{{setButtons(false);if(response&&response.authResponse&&response.authResponse.code){{fragments.code=response.authResponse.code;save();telemetry('sdk_callback_with_code');finish()}}else{{telemetry('sdk_callback_without_code');status('Login Meta belum selesai atau diblokir. Buka tautan ini di Chrome / Safari lalu coba lagi.','error')}}}},{{config_id:configId,response_type:'code',override_default_response_type:true,extras}})}}
coexist.addEventListener('click',()=>login('coexistence'));newNumber.addEventListener('click',()=>login('cloud_api_new'));
window.fbAsyncInit=()=>{{FB.init({{appId,cookie:true,xfbml:false,version:'v26.0'}});sdkReady=true;telemetry('sdk_ready')}};(function(d,s,id){{const js=d.createElement(s);js.id=id;js.src='https://connect.facebook.net/en_US/sdk.js';js.onerror=()=>{{telemetry('sdk_load_failed');status('Halaman Login for Business Meta gagal dimuat. Periksa koneksi atau buka di Chrome / Safari.','error')}};d.head.appendChild(js)}})(document,'script','facebook-jssdk');
const inApp=/FBAN|FBAV|Instagram|WhatsApp/i.test(navigator.userAgent);if(inApp){{document.querySelector('#in-app').style.display='block';telemetry('external_browser_requested')}}window.addEventListener('pageshow',()=>finish());document.addEventListener('visibilitychange',()=>{{if(document.visibilityState==='visible')finish()}});telemetry('page_loaded');finish();
</script></body></html>'''


@router.get("/launch", response_class=HTMLResponse)
async def launch(
    response: Response,
    state: str = Query(..., min_length=32),
    db: AsyncSession = Depends(get_db),
) -> str:
    agent_id = _agent_for_state(state)
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    from app.config import get_settings
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_embedded_signup_config_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Meta Embedded Signup is not configured")
    response.set_cookie(
        "meta_signup_state",
        state,
        max_age=settings.meta_signup_state_ttl_seconds,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    # A signed, short-lived onboarding page must never be served from a mobile
    # browser's history/cache after the deployment has changed its flow.
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    name = html.escape(agent.name)
    return _render_standard_signup_page(
        state=state,
        name=name,
        app_id=settings.meta_app_id,
        config_id=settings.meta_embedded_signup_config_id,
        ttl_ms=settings.meta_signup_state_ttl_seconds * 1000,
    )

    # Kept temporarily below only to make old, already-open pages harmless;
    # new launches always use the official JS SDK path above.
    return rf'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hubungkan WhatsApp · {name}</title><style>
:root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ min-height:100vh; margin:0; color:#edf3ff; background:#08111f; background-image:radial-gradient(circle at 15% 4%,rgba(41,98,255,.24),transparent 32rem),radial-gradient(circle at 88% 88%,rgba(0,187,126,.14),transparent 27rem); }}
.signup-shell {{ width:min(100% - 32px, 570px); margin:0 auto; padding:64px 0 40px; }}
.brand {{ display:flex; align-items:center; gap:10px; margin:0 0 30px 4px; color:#bac9df; font-size:14px; font-weight:700; letter-spacing:.02em; }}
.brand-mark {{ display:grid; place-items:center; width:32px; height:32px; border-radius:10px; background:linear-gradient(145deg,#1c72ff,#814dff); box-shadow:0 8px 20px rgba(37,103,255,.32); color:white; }}
.signup-card {{ overflow:hidden; border:1px solid rgba(173,199,255,.17); border-radius:24px; background:rgba(15,27,48,.9); box-shadow:0 28px 70px rgba(0,0,0,.36); }}
.card-main {{ padding:35px; }}
.eyebrow {{ display:inline-flex; align-items:center; gap:7px; padding:6px 10px; border:1px solid rgba(102,157,255,.3); border-radius:999px; background:rgba(47,111,255,.12); color:#9cc0ff; font-size:12px; font-weight:800; letter-spacing:.04em; text-transform:uppercase; }}
h1 {{ max-width:430px; margin:18px 0 12px; color:#fff; font-size:clamp(29px,6vw,39px); line-height:1.1; letter-spacing:-.045em; }}
.lead {{ margin:0; color:#b9c8dd; font-size:16px; line-height:1.58; }}
.agent {{ color:#fff; font-weight:750; }}
.steps {{ display:grid; gap:13px; margin:29px 0; }}
.step {{ display:flex; align-items:flex-start; gap:12px; color:#c6d3e6; font-size:14px; line-height:1.45; }}
.step-number {{ display:grid; flex:0 0 auto; place-items:center; width:24px; height:24px; border:1px solid rgba(112,160,255,.38); border-radius:50%; color:#a9c8ff; font-size:12px; font-weight:800; }}
#connect {{ width:100%; border:0; border-radius:13px; padding:15px 18px; cursor:pointer; background:linear-gradient(100deg,#1877f2,#2c65db); box-shadow:0 12px 25px rgba(24,119,242,.28); color:white; font:inherit; font-size:16px; font-weight:800; transition:transform .16s ease,filter .16s ease; }}
#connect:hover {{ filter:brightness(1.09); transform:translateY(-1px); }} #connect:focus-visible {{ outline:3px solid #96baff; outline-offset:3px; }} #connect:disabled {{ cursor:wait; opacity:.68; transform:none; }}
#status {{ min-height:22px; margin:16px 0 0; color:#b9c8dd; font-size:13px; line-height:1.45; text-align:center; }} #status[data-state="success"] {{ color:#68e3ac; }} #status[data-state="error"] {{ color:#ff9ca6; }}
#activation {{ display:none; margin:18px 0 0; padding:15px; border:1px solid rgba(112,160,255,.25); border-radius:13px; background:rgba(10,22,43,.72); }} #activation p {{ margin:0 0 10px; color:#c6d3e6; font-size:13px; line-height:1.45; }} #activation-row {{ display:flex; gap:8px; }} #activation-pin {{ min-width:0; flex:1; border:1px solid rgba(173,199,255,.28); border-radius:10px; padding:11px 12px; background:#08111f; color:#fff; font:inherit; }} #activate {{ border:0; border-radius:10px; padding:11px 13px; cursor:pointer; background:#2b7df0; color:white; font:inherit; font-weight:750; }} #activate:disabled {{ cursor:wait; opacity:.68; }}
#handoff {{ display:none; margin:18px 0 0; padding:15px; border:1px solid rgba(112,160,255,.25); border-radius:13px; background:rgba(10,22,43,.72); }} #handoff p {{ margin:0 0 10px; color:#c6d3e6; font-size:13px; line-height:1.45; }} #handoff-select {{ width:100%; border:1px solid rgba(173,199,255,.28); border-radius:10px; padding:11px 12px; background:#08111f; color:#fff; font:inherit; }} #handoff-complete {{ width:100%; margin-top:8px; border:0; border-radius:10px; padding:11px 13px; cursor:pointer; background:#2b7df0; color:white; font:inherit; font-weight:750; }}
.security {{ display:flex; gap:10px; padding:18px 35px; border-top:1px solid rgba(173,199,255,.12); background:rgba(5,13,27,.32); color:#91a3bd; font-size:12px; line-height:1.5; }}
.security-icon {{ color:#70dba4; }}
@media (max-width:520px) {{ .signup-shell {{ width:min(100% - 24px,570px); padding-top:24px; }} .card-main {{ padding:28px 23px; }} .security {{ padding:17px 23px; }} }}
</style></head>
<body><main class="signup-shell"><div class="brand"><span class="brand-mark">✦</span><span>Chief AI Officer</span></div><section class="signup-card" aria-labelledby="page-title"><div class="card-main"><span class="eyebrow">◉ Meta Embedded Signup</span><h1 id="page-title">Hubungkan WhatsApp Business</h1><p class="lead">Sambungkan nomor WhatsApp Business resmi untuk <span class="agent">{name}</span> lewat proses aman dari Meta.</p><div class="steps" aria-label="Langkah koneksi"><div class="step"><span class="step-number">1</span><span>Lanjutkan ke akun Facebook/Meta yang mengelola WhatsApp Business Anda.</span></div><div class="step"><span class="step-number">2</span><span>Pilih atau daftarkan nomor yang ingin digunakan oleh {name}.</span></div><div class="step"><span class="step-number">3</span><span>Selesaikan verifikasi Meta; koneksi akan disimpan otomatis.</span></div></div><button id="connect" type="button">Lanjutkan dengan Meta <span aria-hidden="true">→</span></button><p id="status" role="status" aria-live="polite"></p><section id="handoff" aria-label="Pilih nomor WhatsApp"><p>Pilih nomor WhatsApp yang baru dikonfirmasi Meta.</p><select id="handoff-select" aria-label="Nomor WhatsApp"></select><button id="handoff-complete" type="button">Lanjutkan ke aktivasi</button></section><section id="activation" aria-label="Aktivasi nomor WhatsApp"><p>Nomor telah dipilih. Untuk mengaktifkannya di WhatsApp Cloud API, buat atau masukkan PIN verifikasi dua langkah WhatsApp 6 digit. PIN tidak disimpan.</p><div id="activation-row"><input id="activation-pin" type="password" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" autocomplete="one-time-code" placeholder="PIN 6 digit" aria-label="PIN WhatsApp 6 digit"><button id="activate" type="button">Aktifkan nomor</button></div></section></div><div class="security"><span class="security-icon">●</span><span>Anda akan melanjutkan ke flow resmi Meta. Kami tidak pernah meminta password Facebook atau kode verifikasi Anda di halaman ini.</span></div></section></main>
<script>
const state={state!r};
const storageKey=`meta-embedded-signup:${{state}}`;
const fragmentTtlMs={settings.meta_signup_state_ttl_seconds * 1000!r};
const statusEl=document.getElementById('status');
const connectButton=document.getElementById('connect');
const activationEl=document.getElementById('activation');
const activationPin=document.getElementById('activation-pin');
const activateButton=document.getElementById('activate');
const handoffEl=document.getElementById('handoff');
const handoffSelect=document.getElementById('handoff-select');
const handoffButton=document.getElementById('handoff-complete');
let completionPromise=null;
let fragments=readFragments();
const mobileContext=Boolean(window.matchMedia&&window.matchMedia('(max-width: 768px)').matches);
function report(event){{fetch('/v1/meta/signup/telemetry',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state:state,event:event,mobile:mobileContext}}),keepalive:true}}).catch(()=>{{}});}}
function readStore(store){{try{{const stored=JSON.parse(store.getItem(storageKey)||'{{}}');if(!stored||typeof stored!=='object')return {{}};if(stored.expires_at&&stored.expires_at<Date.now()){{store.removeItem(storageKey);return {{}};}}return stored;}}catch(_err){{return {{}};}}}}
function readFragments(){{return Object.assign({{}},readStore(localStorage),readStore(sessionStorage));}}
function saveFragments(){{fragments.expires_at=Date.now()+fragmentTtlMs;const encoded=JSON.stringify(fragments);sessionStorage.setItem(storageKey,encoded);localStorage.setItem(storageKey,encoded);}}
function clearFragments(){{sessionStorage.removeItem(storageKey);localStorage.removeItem(storageKey);}}
function ready(){{return Boolean(fragments.code&&fragments.waba_id&&fragments.phone_number_id);}}
function setStatus(message,type=''){{statusEl.textContent=message;statusEl.dataset.state=type;}}
function setFragment(name,value){{if(typeof value==='string'&&value){{fragments[name]=value;fragments.updated_at=Date.now();saveFragments();}}}}
function showActivation(){{activationEl.style.display='block';connectButton.disabled=true;connectButton.textContent='Nomor dipilih';setStatus('Koneksi disimpan. Selesaikan aktivasi nomor di Meta.');}}
function showPending(){{if(fragments.activation_completed){{activationEl.style.display='none';setStatus('Aktivasi nomor dikirim ke Meta. Periksa status sampai Active.','success');connectButton.disabled=true;connectButton.textContent='WhatsApp terhubung';return;}}if(fragments.activation_required){{showActivation();return;}}if(fragments.completion_requested){{setStatus('Koneksi WhatsApp sedang disimpan. Tunggu sebentar…');return;}}if(fragments.code||fragments.waba_id||fragments.phone_number_id){{setStatus('Menunggu konfirmasi Meta melengkapi koneksi…');}}}}
async function attemptCompletion(){{if(!ready()||fragments.activation_required||fragments.completion_requested||completionPromise)return;fragments.completion_requested=true;saveFragments();setStatus('Menyimpan koneksi WhatsApp…');completionPromise=fetch('/v1/meta/signup/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state:state,code:fragments.code,waba_id:fragments.waba_id,phone_number_id:fragments.phone_number_id}})}}).then(async response=>{{const data=await response.json();if(!response.ok)throw new Error(data.detail||'Koneksi belum berhasil. Silakan coba lagi.');fragments={{activation_required:true,expires_at:Date.now()+fragmentTtlMs}};saveFragments();showActivation();}}).catch(error=>{{fragments.completion_requested=false;saveFragments();setStatus(error.message,'error');connectButton.disabled=false;connectButton.innerHTML='Coba lagi dengan Meta <span aria-hidden="true">→</span>';}}).finally(()=>{{completionPromise=null;}});return completionPromise;}}
activateButton.onclick=async()=>{{const pin=activationPin.value.trim();if(!/^\d{{6}}$/.test(pin)){{setStatus('Masukkan PIN WhatsApp 6 digit.','error');return;}}activateButton.disabled=true;setStatus('Mengaktifkan nomor di Meta…');try{{const response=await fetch('/v1/meta/signup/activate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state:state,pin:pin}})}});const data=await response.json();if(!response.ok)throw new Error(data.detail||'Aktivasi nomor belum berhasil.');activationPin.value='';fragments={{activation_completed:true,expires_at:Date.now()+fragmentTtlMs}};saveFragments();showPending();}}catch(error){{activationPin.value='';setStatus(error.message,'error');activateButton.disabled=false;}}}};
async function completeHandoff(candidate){{setStatus('Menyimpan pilihan nomor WhatsApp…');const response=await fetch('/v1/meta/signup/handoff/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state:state,waba_id:candidate.waba_id,phone_number_id:candidate.phone_number_id}})}});const data=await response.json();if(!response.ok)throw new Error(data.detail||'Koneksi belum berhasil.');fragments={{activation_required:true,expires_at:Date.now()+fragmentTtlMs}};saveFragments();handoffEl.style.display='none';showActivation();}}
async function loadServerHandoff(){{if(fragments.activation_required||fragments.activation_completed)return;try{{const response=await fetch('/v1/meta/signup/handoff/status?state='+encodeURIComponent(state));const data=await response.json();if(!response.ok||!data.ready)return;const candidates=data.candidates||[];if(candidates.length===1){{await completeHandoff(candidates[0]);return;}}handoffSelect.innerHTML='';candidates.forEach((candidate,index)=>{{const option=document.createElement('option');option.value=String(index);option.textContent=candidate.display_phone||candidate.business_name||'Nomor WhatsApp';handoffSelect.appendChild(option);}});handoffButton.onclick=()=>completeHandoff(candidates[Number(handoffSelect.value)]).catch(error=>setStatus(error.message,'error'));handoffEl.style.display='block';connectButton.disabled=true;setStatus('Meta selesai. Pilih nomor untuk dilanjutkan.');}}catch(_error){{/* A normal desktop flow has no server handoff. */}}}}
function resumePending(){{showPending();attemptCompletion();loadServerHandoff();}}
window.addEventListener('pageshow',event=>{{if(event.persisted){{window.location.reload();return;}}resumePending();}});
document.addEventListener('visibilitychange',()=>{{if(document.visibilityState==='visible')resumePending();}});
connectButton.onclick=()=>{{report('launch_clicked');if(fragments.completed)return;if(ready()){{attemptCompletion();return;}}connectButton.disabled=true;setStatus('Membuka Meta…');window.location.assign('/v1/meta/signup/identity/start?state='+encodeURIComponent(state));}};
report('page_loaded');
resumePending();
</script></body></html>'''


@router.get("/l/{state}", response_class=HTMLResponse)
async def launch_short(state: str) -> RedirectResponse:
    """Compact, WhatsApp-friendly alias for the Embedded Signup launch URL."""
    _agent_for_state(state)
    response = RedirectResponse(url=f"/v1/meta/signup/launch?state={state}", status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@router.post("/complete")
async def complete(payload: EmbeddedSignupCompleteRequest, db: AsyncSession = Depends(get_db)) -> dict:
    agent_id = _agent_for_state(payload.state)
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    try:
        token = await exchange_code_for_token(payload.code)
        numbers = await get_waba_phone_numbers(payload.waba_id, token)
        selected = next((item for item in numbers if str(item.get("id")) == payload.phone_number_id), None) if payload.phone_number_id else None
        if payload.connection_mode == "coexistence" and selected is None and len(numbers) == 1:
            selected = numbers[0]
        if selected is None:
            if payload.connection_mode != "coexistence":
                raise ValueError("Selected phone number does not belong to the shared WABA")
            encrypted = encrypt_value(token)
            if not encrypted.startswith("enc:"):
                raise RuntimeError("CHANNEL_SECRET_KEY must be configured before Cloud API credentials can be stored")
            candidates = [
                {
                    "waba_id": payload.waba_id,
                    "phone_number_id": str(item["id"]),
                    "display_phone": str(item.get("display_phone_number") or ""),
                    "business_name": str(item.get("verified_name") or ""),
                }
                for item in numbers if item.get("id")
            ]
            if not candidates:
                raise ValueError("Meta did not return a WhatsApp Business App number")
            await _save_handoff(payload.state, {"token": encrypted, "candidates": candidates, "connection_mode": "coexistence"})
            return {"ok": True, "selection_required": True, "candidates": candidates}
        if payload.connection_mode == "coexistence" and not (
            selected.get("is_on_biz_app") is True and str(selected.get("platform_type") or "").upper() == "CLOUD_API"
        ):
            raise ValueError("Meta has not completed WhatsApp Business App Coexistence for this number")
        encrypted = encrypt_value(token)
        if not encrypted.startswith("enc:"):
            raise RuntimeError("CHANNEL_SECRET_KEY must be configured before Cloud API credentials can be stored")
        await subscribe_waba_to_webhooks(payload.waba_id, token)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    phone_number_id = str(selected["id"])
    existing = (await db.execute(select(Agent.id).where(Agent.wa_phone_number_id == phone_number_id, Agent.id != agent.id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This WhatsApp number is already connected to another assistant")
    agent.wa_phone_number_id = phone_number_id
    agent.wa_waba_id = payload.waba_id
    agent.wa_access_token_encrypted = encrypted
    agent.wa_display_phone = str(selected.get("display_phone_number") or "") or None
    agent.wa_business_name = str(selected.get("verified_name") or "") or None
    agent.wa_connection_type = "cloud_api"
    agent.wa_cloud_api_mode = "coexistence" if payload.connection_mode == "coexistence" else "cloud_api_new"
    agent.channel_type = "whatsapp"
    agent.version += 1
    await db.commit()
    return {
        "ok": True,
        "agent_id": str(agent.id),
        "connection_type": "cloud_api",
        "display_phone": agent.wa_display_phone,
        "activation_required": payload.connection_mode != "coexistence",
        "cloud_api_mode": agent.wa_cloud_api_mode,
    }


@router.post("/handoff/complete")
async def complete_server_handoff(
    payload: EmbeddedSignupHandoffCompleteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Persist a phone selected from the server-side mobile OAuth handoff."""
    agent_id = _agent_for_state(payload.state)
    handoff = await _get_handoff(payload.state)
    if not handoff:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mobile signup result is missing or expired")
    selected = next(
        (
            item for item in handoff.get("candidates", [])
            if item.get("waba_id") == payload.waba_id and item.get("phone_number_id") == payload.phone_number_id
        ),
        None,
    )
    if selected is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Selected phone number was not returned by Meta")
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    existing = (await db.execute(select(Agent.id).where(Agent.wa_phone_number_id == payload.phone_number_id, Agent.id != agent.id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This WhatsApp number is already connected to another assistant")
    agent.wa_phone_number_id = payload.phone_number_id
    agent.wa_waba_id = payload.waba_id
    agent.wa_access_token_encrypted = handoff["token"]
    agent.wa_display_phone = selected.get("display_phone") or None
    agent.wa_business_name = selected.get("business_name") or None
    agent.wa_connection_type = "cloud_api"
    agent.wa_cloud_api_mode = handoff.get("connection_mode") or "cloud_api_new"
    agent.channel_type = "whatsapp"
    agent.version += 1
    await subscribe_waba_to_webhooks(payload.waba_id, decrypt_value(handoff["token"]))
    await db.commit()
    await _clear_handoff(payload.state)
    return {"ok": True, "connection_type": "cloud_api", "activation_required": agent.wa_cloud_api_mode != "coexistence"}


@router.post("/activate")
async def activate_phone_number(
    payload: EmbeddedSignupActivationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Register the selected number with Meta after a valid signed signup.

    The PIN is supplied by the operator for this request only.  It is never
    stored, returned, or logged; the short-lived signed state binds the action
    to the same agent selected by the Embedded Signup link.
    """
    agent_id = _agent_for_state(payload.state)
    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found")
    if not (
        agent.wa_connection_type == "cloud_api"
        and agent.wa_phone_number_id
        and agent.wa_waba_id
        and agent.wa_access_token_encrypted
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Complete Meta Embedded Signup before activating this phone number",
        )
    if getattr(agent, "wa_cloud_api_mode", None) == "coexistence":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This number is already registered through WhatsApp Business App Coexistence; no PIN activation is required",
        )
    try:
        from app.core.infra.wa_cloud_client import register_phone_number

        access_token = decrypt_value(agent.wa_access_token_encrypted)
        await register_phone_number(agent.wa_phone_number_id, access_token, payload.pin)
    except Exception as exc:
        # Deliberately omit PIN, access token, and Meta response body from logs.
        logger.warning(
            "cloud_api.signup_activation_failed",
            agent_id=str(agent.id),
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Meta rejected phone activation. Verify the six-digit WhatsApp two-step "
                "verification PIN and complete any pending phone verification in Meta."
            ),
        ) from exc
    return {"ok": True, "registration": "requested"}

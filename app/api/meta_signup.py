"""Secure Meta Embedded Signup endpoint for a caller-owned assistant."""
from __future__ import annotations

import hashlib
import html
import json
import uuid
from typing import Literal
from urllib.parse import urlencode, quote_plus

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


def _hosted_signup_url(mode: str | None = None) -> str:
    from app.config import get_settings
    settings = get_settings()
    extras_dict = {"setup": {}, "sessionInfoVersion": "3", "version": "v4"}
    if mode == "coexistence":
        extras_dict["featureType"] = "whatsapp_business_app_onboarding"
    extras = json.dumps(extras_dict, separators=(",", ":"))
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
    # Meta sends the exchangeable code and asset IDs through separate browser
    # callbacks. The code remains valid when the session event is delayed.
    waba_id: str | None = Field(None, min_length=1, max_length=64)
    phone_number_id: str | None = Field(None, min_length=1, max_length=64)
    connection_mode: Literal["cloud_api_new", "coexistence"] = "cloud_api_new"
    redirect_uri: str | None = None


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
async def identity_start(request: Request, state: str = Query(..., min_length=32), mode: str | None = Query(None)) -> RedirectResponse:
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
    payload = json.dumps({"state": state, "mode": mode})
    await redis.set(_identity_key(nonce), payload, ex=settings.meta_signup_state_ttl_seconds)
    extras_dict = {"setup": {}, "sessionInfoVersion": "3", "version": "v4"}
    if mode == "coexistence":
        extras_dict["featureType"] = "whatsapp_business_app_onboarding"
    extras = json.dumps(extras_dict, separators=(",", ":"))

    target = "https://www.facebook.com/" + settings.meta_graph_api_version + "/dialog/oauth?" + urlencode({
        "client_id": settings.meta_app_id,
        "redirect_uri": _identity_callback_url(),
        "state": nonce,
        "config_id": settings.meta_embedded_signup_config_id,
        "response_type": "code",
        "override_default_response_type": "true",
        "extras": extras,
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
    raw_payload = await redis.getdel(_identity_key(state))
    if not raw_payload:
        raise HTTPException(status_code=403, detail="Meta business identification expired or is invalid")
    try:
        payload = json.loads(raw_payload)
        signed_state = payload["state"]
        mode = payload.get("mode")
    except Exception:
        signed_state = raw_payload
        mode = None

    if signed_state != request.cookies.get("meta_signup_state"):
        raise HTTPException(status_code=403, detail="Meta business identification expired or is invalid")
    _agent_for_state(signed_state)
    target_url = f"/v1/meta/signup/launch?state={signed_state}#code={code}&redirect_uri={quote_plus(_identity_callback_url())}"
    if mode:
        target_url += f"&mode={mode}"
    return RedirectResponse(target_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/identity/select")
async def identity_select(request: Request, choice: int = Query(..., ge=0, le=100), mode: str | None = Query(None)) -> RedirectResponse:
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
    return RedirectResponse(_hosted_signup_url(mode), status_code=status.HTTP_303_SEE_OTHER)


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

    On desktop browsers the Meta JS SDK popup flow is used.  On mobile browsers
    (where the popup becomes a full-page redirect that destroys JS context) the
    page redirects to ``/identity/start`` which uses a server-side OAuth
    callback and Redis handoff instead.  When the mobile browser returns to this
    page the client polls ``/handoff/status`` to discover the result.

    Fragments are persisted in both ``sessionStorage`` (same-tab) and
    ``localStorage`` (cross-tab / page-navigation) so they survive the mobile
    redirect round-trip.
    """
    return rf'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hubungkan WhatsApp · {name}</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif}}body{{margin:0;min-height:100vh;background:#08111f;color:#edf3ff}}main{{max-width:560px;margin:auto;padding:32px 20px}}section{{padding:28px;border:1px solid #29466e;border-radius:20px;background:#0f1b30}}h1{{font-size:32px;margin:12px 0}}p{{color:#c6d3e6;line-height:1.5}}button,a.button{{display:block;width:100%;box-sizing:border-box;margin-top:12px;padding:15px;border:0;border-radius:12px;background:#1877f2;color:#fff;font:700 16px inherit;text-align:center;text-decoration:none;cursor:pointer}}button.secondary{{background:#243956;border:1px solid #4c6485}}button:disabled{{opacity:.6;cursor:wait}}#status{{min-height:24px;text-align:center}}#status[data-state=error]{{color:#ff9ca6}}#status[data-state=success]{{color:#68e3ac}}#in-app,#selection{{display:none;padding:12px;margin-top:16px;border-radius:10px;background:#3b2b12;color:#ffe0a4;font-size:14px}}#selection{{background:#10243f;color:#d9e8ff}}#selection button{{background:#243956;border:1px solid #4c6485}}</style></head><body><main><section><small>◉ META EMBEDDED SIGNUP</small><h1>Hubungkan WhatsApp Business</h1><p>Sambungkan nomor resmi untuk <strong>{name}</strong>.</p><p><strong>Pakai WhatsApp Business yang sudah ada</strong> membuat AI Arthur dan tim Anda tetap dapat membalas dari nomor yang sama. <strong>Nomor baru</strong> memakai WhatsApp Cloud API khusus.</p><button id="coexist" type="button">Pakai WhatsApp Business yang sudah ada</button><button id="new-number" class="secondary" type="button">Gunakan nomor baru khusus Cloud API</button><div id="in-app"><strong>Buka di browser eksternal.</strong> Browser bawaan aplikasi seperti WhatsApp, Instagram, atau Facebook sering memblokir halaman Login for Business Meta.<a id="external-browser" class="button" target="_blank" rel="noopener">Buka di Chrome / Safari</a></div><section id="selection" aria-live="polite"><strong>Pilih nomor WhatsApp yang akan dihubungkan</strong><div id="selection-options"></div></section><p id="status" role="status" aria-live="polite"></p></section></main><script>
const state={state!r},appId={app_id!r},configId={config_id!r},ttlMs={ttl_ms!r};
const key=`meta-es:${{state}}`,launchUrl=`/v1/meta/signup/l/${{encodeURIComponent(state)}}`,statusEl=document.querySelector('#status'),coexist=document.querySelector('#coexist'),newNumber=document.querySelector('#new-number'),selection=document.querySelector('#selection'),selectionOptions=document.querySelector('#selection-options');
const isMobile=Boolean(window.matchMedia&&window.matchMedia('(max-width:768px)').matches)||/Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const inApp=/FBAN|FBAV|Instagram|WhatsApp/i.test(navigator.userAgent);
let sdkReady=false,submitting=false,fallbackTimer=null,handoffPollTimer=null,handoffPollCount=0;
/* --- Storage: read from both localStorage and sessionStorage, write to both --- */
function readStore(store){{try{{const raw=store.getItem(key);if(!raw)return {{}};const x=JSON.parse(raw);return x&&x.expires_at>Date.now()?x:{{}}}}catch(_e){{return {{}}}}}}
function read(){{return Object.assign({{}},readStore(localStorage),readStore(sessionStorage))}}
let fragments=read();
function save(){{fragments.expires_at=Date.now()+ttlMs;const encoded=JSON.stringify(fragments);sessionStorage.setItem(key,encoded);localStorage.setItem(key,encoded)}}
function clear(){{sessionStorage.removeItem(key);localStorage.removeItem(key)}}
function status(message,type=''){{statusEl.textContent=message;statusEl.dataset.state=type}}
function setButtons(disabled){{coexist.disabled=disabled;newNumber.disabled=disabled}}
function telemetry(event){{fetch('/v1/meta/signup/telemetry',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state,event,mobile:isMobile}}),keepalive:true}}).catch(()=>{{}})}}
function isMetaOrigin(origin){{return origin==='https://www.facebook.com'||origin==='https://web.facebook.com'||origin==='https://business.facebook.com'}}
function applySession(value){{let data=value;try{{if(typeof value==='string')data=JSON.parse(value)}}catch(_e){{return}}if(!data||data.type!=='WA_EMBEDDED_SIGNUP')return;const d=data.data||{{}};if(d.waba_id)fragments.waba_id=String(d.waba_id);if(d.phone_number_id)fragments.phone_number_id=String(d.phone_number_id);if(String(data.event||'').toUpperCase()==='FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING')fragments.connection_mode='coexistence';save();telemetry('session_event_received');finish()}}
window.addEventListener('message',event=>{{if(isMetaOrigin(event.origin))applySession(event.data)}});
async function selectNumber(candidate){{setButtons(true);status('Menyimpan nomor WhatsApp…');try{{const r=await fetch('/v1/meta/signup/handoff/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state,waba_id:candidate.waba_id,phone_number_id:candidate.phone_number_id}})}});const d=await r.json();if(!r.ok)throw Error(d.detail||'Koneksi belum berhasil.');selection.style.display='none';clear();stopHandoffPoll();status('Terhubung via Meta Cloud API dan WhatsApp Business App.','success')}}catch(e){{setButtons(false);status(e.message,'error')}}}}
function chooseNumber(candidates){{selectionOptions.replaceChildren();candidates.forEach(candidate=>{{const button=document.createElement('button');button.type='button';button.textContent=candidate.display_phone||candidate.business_name||'Nomor WhatsApp';button.onclick=()=>selectNumber(candidate);selectionOptions.appendChild(button)}});selection.style.display='block';status('Meta selesai. Pilih nomor WhatsApp untuk dilanjutkan.')}}
function finish(allowServerDiscovery=false){{if(submitting||!fragments.code||(!fragments.waba_id&&!allowServerDiscovery)){{if(fragments.code||fragments.waba_id)status('Menunggu konfirmasi Meta melengkapi koneksi…');return}}submitting=true;setButtons(true);status('Menyimpan koneksi WhatsApp…');stopHandoffPoll();fetch('/v1/meta/signup/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state,code:fragments.code,waba_id:fragments.waba_id||null,phone_number_id:fragments.phone_number_id,connection_mode:fragments.connection_mode||'cloud_api_new',redirect_uri:fragments.redirect_uri}})}}).then(async r=>{{const d=await r.json();if(!r.ok)throw Error(d.detail||'Koneksi belum berhasil.');if(d.selection_required){{submitting=false;chooseNumber(d.candidates||[]);return}}clear();status(d.activation_required?'Koneksi tersimpan. Selesaikan PIN enam digit untuk mengaktifkan nomor baru di Meta.':'Terhubung via Meta Cloud API dan WhatsApp Business App.','success')}}).catch(e=>{{submitting=false;setButtons(false);status(e.message,'error')}})}}
/* --- Server-side handoff polling (for mobile redirect flow) --- */
function stopHandoffPoll(){{if(handoffPollTimer){{clearInterval(handoffPollTimer);handoffPollTimer=null}}}}
async function checkHandoff(){{if(submitting)return;try{{const r=await fetch('/v1/meta/signup/handoff/status?state='+encodeURIComponent(state));const d=await r.json();if(!r.ok||!d.ready)return;stopHandoffPoll();const candidates=d.candidates||[];if(candidates.length===1){{await selectNumber(candidates[0]);return}}chooseNumber(candidates)}}catch(_e){{}}}}
function startHandoffPoll(){{if(handoffPollTimer)return;handoffPollCount=0;checkHandoff();handoffPollTimer=setInterval(()=>{{handoffPollCount++;if(handoffPollCount>=40){{stopHandoffPoll();return}}checkHandoff()}},3000)}}
/* --- Desktop: FB SDK popup flow --- */
function login(mode){{if(!sdkReady){{status('Halaman Login for Business Meta belum siap. Coba lagi atau buka di Chrome / Safari.','error');return}}if(fallbackTimer)clearTimeout(fallbackTimer);fragments={{connection_mode:mode,expires_at:Date.now()+ttlMs}};save();telemetry('sdk_launch_requested');setButtons(true);status('Membuka Login for Business Meta…');const extras=mode==='coexistence'?{{setup:{{}},featureType:'whatsapp_business_app_onboarding',sessionInfoVersion:'3'}}:{{setup:{{}}}};FB.login(response=>{{setButtons(false);if(response&&response.authResponse&&response.authResponse.code){{fragments.code=response.authResponse.code;save();telemetry('sdk_callback_with_code');finish();fallbackTimer=window.setTimeout(()=>finish(true),1500)}}else{{telemetry('sdk_callback_without_code');status('Login Meta belum selesai atau diblokir. Buka tautan ini di Chrome / Safari lalu coba lagi.','error')}}}},{{config_id:configId,response_type:'code',override_default_response_type:true,extras}})}}
/* --- Mobile: full-page redirect to /identity/start --- */
function mobileRedirect(mode){{fragments={{connection_mode:mode,expires_at:Date.now()+ttlMs}};save();telemetry('sdk_launch_requested');setButtons(true);status('Membuka Meta…');window.location.assign('/v1/meta/signup/identity/start?state='+encodeURIComponent(state)+'&mode='+encodeURIComponent(mode))}}
/* --- Button handlers: branch on mobile vs desktop --- */
coexist.addEventListener('click',()=>{{if(inApp)return;if(isMobile){{mobileRedirect('coexistence')}}else{{login('coexistence')}}}});
newNumber.addEventListener('click',()=>{{if(inApp)return;if(isMobile){{mobileRedirect('cloud_api_new')}}else{{login('cloud_api_new')}}}});
/* --- FB SDK init (desktop only, but harmless on mobile) --- */
window.fbAsyncInit=()=>{{FB.init({{appId,cookie:true,xfbml:false,version:'v26.0'}});sdkReady=true;telemetry('sdk_ready')}};(function(d,s,id){{const js=d.createElement(s);js.id=id;js.src='https://connect.facebook.net/en_US/sdk.js';js.onerror=()=>{{telemetry('sdk_load_failed');if(!isMobile)status('Halaman Login for Business Meta gagal dimuat. Periksa koneksi atau buka di Chrome / Safari.','error')}};d.head.appendChild(js)}})(document,'script','facebook-jssdk');
/* --- Lifecycle: resume on pageshow / visibilitychange --- */
function resume(){{const hash=new URLSearchParams(window.location.hash.substring(1));const code=hash.get('code');if(code){{fragments.code=code;const mode=hash.get('mode');if(mode)fragments.connection_mode=mode;const redir=hash.get('redirect_uri');if(redir)fragments.redirect_uri=redir;save();window.history.replaceState(null,'',window.location.pathname+window.location.search);finish(true);return}}fragments=read();if(submitting)return;finish();checkHandoff()}}
document.querySelector('#external-browser').href=launchUrl;
if(inApp){{document.querySelector('#in-app').style.display='block';telemetry('external_browser_requested')}}
window.addEventListener('pageshow',event=>{{if(event.persisted){{window.location.reload();return}}resume();if(isMobile)startHandoffPoll()}});
document.addEventListener('visibilitychange',()=>{{if(document.visibilityState==='visible'){{resume();if(isMobile)startHandoffPoll()}}}});
window.addEventListener('storage',event=>{{if(event.key===key){{fragments=read();finish()}}}});
telemetry('page_loaded');
resume();
if(isMobile)startHandoffPoll();
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
        token = await exchange_code_for_token(payload.code, redirect_uri=payload.redirect_uri)
        # The documented JS SDK returns the token code and session data through
        # separate callbacks. If a mobile browser drops the session message,
        # use the valid code to discover the assets on Meta's server instead.
        if not payload.waba_id:
            candidates: list[dict[str, str]] = []
            for waba_id in await get_shared_waba_ids(token):
                for number in await get_waba_phone_numbers(waba_id, token):
                    phone_number_id = str(number.get("id") or "")
                    if phone_number_id:
                        candidates.append({
                            "waba_id": str(waba_id),
                            "phone_number_id": phone_number_id,
                            "display_phone": str(number.get("display_phone_number") or ""),
                            "business_name": str(number.get("verified_name") or ""),
                        })
            if not candidates:
                raise ValueError("Meta did not return a shared WhatsApp phone number")
            encrypted = encrypt_value(token)
            if not encrypted.startswith("enc:"):
                raise RuntimeError("CHANNEL_SECRET_KEY must be configured before Cloud API credentials can be stored")
            await _save_handoff(payload.state, {
                "token": encrypted,
                "candidates": candidates,
                "connection_mode": payload.connection_mode,
            })
            return {"ok": True, "selection_required": True, "candidates": candidates}
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

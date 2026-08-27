"""Secure Meta Embedded Signup endpoint for a caller-owned assistant."""
from __future__ import annotations

import html
import hashlib
import json
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.infra.channel_service import decrypt_value, encrypt_value
from app.core.infra.meta_embedded_signup import (
    build_signup_state,
    exchange_code_for_token,
    get_shared_waba_ids,
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


class EmbeddedSignupCompleteRequest(BaseModel):
    state: str = Field(..., min_length=32)
    code: str = Field(..., min_length=3)
    waba_id: str = Field(..., min_length=1, max_length=64)
    phone_number_id: str = Field(..., min_length=1, max_length=64)


class EmbeddedSignupActivationRequest(BaseModel):
    """One-time WhatsApp registration request for a signed signup link."""

    state: str = Field(..., min_length=32)
    pin: str = Field(..., pattern=r"^\d{6}$")


class EmbeddedSignupHandoffCompleteRequest(BaseModel):
    state: str = Field(..., min_length=32)
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


@router.get("/callback", response_class=HTMLResponse)
async def oauth_callback(
    state: str = Query(..., min_length=32),
    code: str | None = Query(None, min_length=3),
    error: str | None = Query(None),
) -> RedirectResponse:
    """Receive mobile OAuth return and retain only encrypted server-side data."""
    _agent_for_state(state)
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
    return RedirectResponse(url=f"/v1/meta/signup/l/{state}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/handoff/status")
async def handoff_status(state: str = Query(..., min_length=32)) -> dict:
    _agent_for_state(state)
    handoff = await _get_handoff(state)
    return {"ready": bool(handoff), "candidates": (handoff or {}).get("candidates", [])}


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
    mobile_redirect_uri = _callback_url()
    # The code comes from FB.login; WABA and phone-number IDs arrive via the
    # official WA_EMBEDDED_SIGNUP postMessage event in either order.
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
const mobileRedirectUri={mobile_redirect_uri!r};
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
function receiveCode(value){{setFragment('code',value);resumePending();}}
function receiveSignupMessage(data){{if(!data||data.type!=='WA_EMBEDDED_SIGNUP')return;setFragment('waba_id',data.data?.waba_id);setFragment('phone_number_id',data.data?.phone_number_id);resumePending();}}
window.addEventListener('message',event=>{{if(event.origin!=='https://www.facebook.com'&&event.origin!=='https://web.facebook.com')return;let data=event.data;try{{if(typeof data==='string')data=JSON.parse(data);}}catch(_err){{return;}}receiveSignupMessage(data);}});
window.addEventListener('pageshow',resumePending);
document.addEventListener('visibilitychange',()=>{{if(document.visibilityState==='visible')resumePending();}});
window.fbAsyncInit=()=>FB.init({{appId:{settings.meta_app_id!r},cookie:true,xfbml:true,version:{settings.meta_graph_api_version!r}}});
connectButton.onclick=()=>{{if(fragments.completed)return;if(ready()){{attemptCompletion();return;}}if(!window.FB||typeof FB.login!=='function'){{setStatus('Komponen Meta masih dimuat. Tunggu sebentar lalu coba lagi.','error');return;}}const mobile=/Android|iPhone|iPad|iPod/i.test(navigator.userAgent);const loginOptions={{config_id:{settings.meta_embedded_signup_config_id!r},response_type:'code',override_default_response_type:true,extras:{{setup:{{}}}}}};if(mobile)loginOptions.redirect_uri=mobileRedirectUri;connectButton.disabled=true;setStatus('Membuka Meta…');FB.login(response=>{{const returnedCode=response&&response.authResponse&&response.authResponse.code;if(returnedCode)receiveCode(returnedCode);else{{connectButton.disabled=false;setStatus('Menunggu konfirmasi Meta. Kembali ke halaman ini setelah setup selesai.');resumePending();}}}},loginOptions);}};
resumePending();
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
    return {
        "ok": True,
        "agent_id": str(agent.id),
        "connection_type": "cloud_api",
        "display_phone": agent.wa_display_phone,
        "activation_required": True,
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
    agent.channel_type = "whatsapp"
    agent.version += 1
    await subscribe_waba_to_webhooks(payload.waba_id, decrypt_value(handoff["token"]))
    await db.commit()
    await _clear_handoff(payload.state)
    return {"ok": True, "connection_type": "cloud_api", "activation_required": True}


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

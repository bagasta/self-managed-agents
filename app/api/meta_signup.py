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
    return f'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Hubungkan WhatsApp · {name}</title><style>
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
.security {{ display:flex; gap:10px; padding:18px 35px; border-top:1px solid rgba(173,199,255,.12); background:rgba(5,13,27,.32); color:#91a3bd; font-size:12px; line-height:1.5; }}
.security-icon {{ color:#70dba4; }}
@media (max-width:520px) {{ .signup-shell {{ width:min(100% - 24px,570px); padding-top:24px; }} .card-main {{ padding:28px 23px; }} .security {{ padding:17px 23px; }} }}
</style></head>
<body><main class="signup-shell"><div class="brand"><span class="brand-mark">✦</span><span>Chief AI Officer</span></div><section class="signup-card" aria-labelledby="page-title"><div class="card-main"><span class="eyebrow">◉ Meta Embedded Signup</span><h1 id="page-title">Hubungkan WhatsApp Business</h1><p class="lead">Sambungkan nomor WhatsApp Business resmi untuk <span class="agent">{name}</span> lewat proses aman dari Meta.</p><div class="steps" aria-label="Langkah koneksi"><div class="step"><span class="step-number">1</span><span>Lanjutkan ke akun Facebook/Meta yang mengelola WhatsApp Business Anda.</span></div><div class="step"><span class="step-number">2</span><span>Pilih atau daftarkan nomor yang ingin digunakan oleh {name}.</span></div><div class="step"><span class="step-number">3</span><span>Selesaikan verifikasi Meta; koneksi akan disimpan otomatis.</span></div></div><button id="connect" type="button">Lanjutkan dengan Meta <span aria-hidden="true">→</span></button><p id="status" role="status" aria-live="polite"></p></div><div class="security"><span class="security-icon">●</span><span>Anda akan melanjutkan ke flow resmi Meta. Kami tidak pernah meminta password Facebook atau kode verifikasi Anda di halaman ini.</span></div></section></main>
<script>
const state={state!r};
const storageKey=`meta-embedded-signup:${{state}}`;
const statusEl=document.getElementById('status');
const connectButton=document.getElementById('connect');
let completionPromise=null;
let fragments=readFragments();
function readFragments(){{try{{const stored=JSON.parse(sessionStorage.getItem(storageKey)||'{{}}');return stored&&typeof stored==='object'?stored:{{}};}}catch(_err){{return {{}};}}}}
function saveFragments(){{sessionStorage.setItem(storageKey,JSON.stringify(fragments));}}
function ready(){{return Boolean(fragments.code&&fragments.waba_id&&fragments.phone_number_id);}}
function setStatus(message,type=''){{statusEl.textContent=message;statusEl.dataset.state=type;}}
function setFragment(name,value){{if(typeof value==='string'&&value){{fragments[name]=value;fragments.updated_at=Date.now();saveFragments();}}}}
function showPending(){{if(fragments.completed){{setStatus('WhatsApp berhasil terhubung. Anda bisa kembali ke WhatsApp.','success');connectButton.disabled=true;connectButton.textContent='WhatsApp terhubung';return;}}if(fragments.completion_requested){{setStatus('Koneksi WhatsApp sedang disimpan. Tunggu sebentar…');return;}}if(fragments.code||fragments.waba_id||fragments.phone_number_id){{setStatus('Menunggu konfirmasi Meta melengkapi koneksi…');}}}}
async function attemptCompletion(){{if(!ready()||fragments.completed||fragments.completion_requested||completionPromise)return;fragments.completion_requested=true;saveFragments();setStatus('Menyimpan koneksi WhatsApp…');completionPromise=fetch('/v1/meta/signup/complete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{state:state,code:fragments.code,waba_id:fragments.waba_id,phone_number_id:fragments.phone_number_id}})}}).then(async response=>{{const data=await response.json();if(!response.ok)throw new Error(data.detail||'Koneksi belum berhasil. Silakan coba lagi.');fragments.completed=true;delete fragments.code;saveFragments();setStatus('WhatsApp berhasil terhubung. Anda bisa kembali ke WhatsApp.','success');connectButton.disabled=true;connectButton.textContent='WhatsApp terhubung';}}).catch(error=>{{fragments.completion_requested=false;saveFragments();setStatus(error.message,'error');connectButton.disabled=false;connectButton.innerHTML='Coba lagi dengan Meta <span aria-hidden="true">→</span>';}}).finally(()=>{{completionPromise=null;}});return completionPromise;}}
function resumePending(){{showPending();attemptCompletion();}}
function receiveCode(value){{setFragment('code',value);resumePending();}}
function receiveSignupMessage(data){{if(!data||data.type!=='WA_EMBEDDED_SIGNUP')return;setFragment('waba_id',data.data?.waba_id);setFragment('phone_number_id',data.data?.phone_number_id);resumePending();}}
window.addEventListener('message',event=>{{if(event.origin!=='https://www.facebook.com'&&event.origin!=='https://web.facebook.com')return;let data=event.data;try{{if(typeof data==='string')data=JSON.parse(data);}}catch(_err){{return;}}receiveSignupMessage(data);}});
window.addEventListener('pageshow',resumePending);
document.addEventListener('visibilitychange',()=>{{if(document.visibilityState==='visible')resumePending();}});
window.fbAsyncInit=()=>FB.init({{appId:{settings.meta_app_id!r},cookie:true,xfbml:true,version:{settings.meta_graph_api_version!r}}});
connectButton.onclick=()=>{{if(fragments.completed)return;if(ready()){{attemptCompletion();return;}}connectButton.disabled=true;setStatus('Membuka Meta…');FB.login(response=>{{const returnedCode=response&&response.authResponse&&response.authResponse.code;if(returnedCode)receiveCode(returnedCode);else{{connectButton.disabled=false;setStatus('Menunggu konfirmasi Meta. Kembali ke halaman ini setelah setup selesai.');resumePending();}}}},{{config_id:{settings.meta_embedded_signup_config_id!r},response_type:'code',override_default_response_type:true,extras:{{setup:{{}}}}}});}};
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
    return {"ok": True, "agent_id": str(agent.id), "connection_type": "cloud_api", "display_phone": agent.wa_display_phone}

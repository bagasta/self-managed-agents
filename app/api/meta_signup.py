"""Endpoints for Meta Embedded Signup integration.

Endpoints:
  POST /v1/meta/signup/complete — Callback endpoint after user completes Facebook Login
  GET  /v1/meta/signup/status/{agent_id} — Check connection status for an agent
  DELETE /v1/meta/signup/{agent_id} — Disconnect WABA from an agent
"""
from __future__ import annotations

import uuid
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import verify_api_key
from app.core.infra.channel_service import encrypt_value
from app.core.infra.meta_embedded_signup import (
    exchange_code_for_token,
    get_waba_phone_numbers,
    subscribe_waba_to_webhooks,
)
from app.database import get_db
from app.models.agent import Agent

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/meta/signup", tags=["meta-signup"])


class EmbeddedSignupCompleteRequest(BaseModel):
    agent_id: uuid.UUID
    code: str = Field(..., description="Short-lived code from Facebook Login Embedded Signup")
    waba_id: str = Field(..., description="WhatsApp Business Account ID from Embedded Signup")
    phone_number_id: str = Field(..., description="Meta Phone Number ID selected by user")
    business_name: str | None = Field(None, description="Business display name")


class EmbeddedSignupStatusResponse(BaseModel):
    agent_id: uuid.UUID
    connected: bool
    connection_type: str | None = None
    waba_id: str | None = None
    phone_number_id: str | None = None
    display_phone: str | None = None
    business_name: str | None = None


@router.get("/launch")
async def launch_meta_embedded_signup_page(
    agent_id: uuid.UUID = Query(..., description="Agent ID to connect"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Render official Meta Embedded Signup page using Meta's Facebook JavaScript SDK."""
    from app.config import get_settings
    settings = get_settings()

    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    app_id = settings.meta_app_id or "1267421042076588"
    config_id = settings.meta_embedded_signup_config_id or "2310023142810963"
    api_version = settings.meta_graph_api_version or "v26.0"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Meta WhatsApp Business Signup — {agent.name}</title>
        <style>
            * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }}
            body {{ background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }}
            .container {{ background: #1e293b; border-radius: 20px; padding: 40px 30px; width: 100%; max-width: 440px; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.5); border: 1px solid #334155; }}
            .logo {{ width: 64px; height: 64px; background: #2563eb; border-radius: 16px; display: inline-flex; align-items: center; justify-content: center; font-size: 32px; margin-bottom: 20px; }}
            h1 {{ font-size: 22px; font-weight: 700; color: #ffffff; margin: 0 0 8px 0; }}
            .subtitle {{ font-size: 14px; color: #94a3b8; margin-bottom: 24px; line-height: 1.5; }}
            .agent-box {{ background: #0f172a; border-radius: 12px; padding: 14px; margin-bottom: 24px; text-align: left; border: 1px solid #334155; }}
            .agent-label {{ font-size: 11px; text-transform: uppercase; color: #64748b; letter-spacing: 0.5px; margin-bottom: 4px; }}
            .agent-name {{ font-size: 16px; font-weight: 600; color: #38bdf8; }}
            .fb-btn {{ background: #1877f2; color: #ffffff; border: none; border-radius: 12px; padding: 14px 24px; font-size: 16px; font-weight: 600; width: 100%; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 10px; transition: all 0.2s ease; box-shadow: 0 4px 12px rgba(24,119,242,0.3); }}
            .fb-btn:hover {{ background: #166fe5; transform: translateY(-1px); }}
            .fb-btn:active {{ transform: translateY(0); }}
            .status-box {{ margin-top: 20px; padding: 14px; border-radius: 12px; font-size: 14px; display: none; }}
            .status-box.loading {{ display: block; background: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6; }}
            .status-box.success {{ display: block; background: #064e3b; color: #6ee7b7; border: 1px solid #10b981; }}
            .status-box.error {{ display: block; background: #7f1d1d; color: #fca5a5; border: 1px solid #ef4444; }}
            .footer {{ margin-top: 24px; font-size: 12px; color: #64748b; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="logo">📱</div>
            <h1>Meta WhatsApp Business</h1>
            <div class="subtitle">Hubungkan WhatsApp Business Account ke Assistant AI kamu secara resmi.</div>

            <div class="agent-box">
                <div class="agent-label">Assistant ID</div>
                <div class="agent-name">{agent.name}</div>
            </div>

            <button id="signup-btn" class="fb-btn" onclick="launchMetaSignup()">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>
                Hubungkan via Meta Embedded Signup
            </button>

            <button id="signup-alt-btn" class="fb-btn" style="background: #334155; margin-top: 10px; font-size: 14px;" onclick="launchMetaSignupStandard()">
                Hubungkan via Standard Facebook OAuth
            </button>

            <div id="status-box" class="status-box"></div>

            <div class="footer">
                Ditenagai oleh Meta WhatsApp Cloud API Official<br>
                <small style="color: #64748b; display: block; margin-top: 8px;">
                    Catatan: Jika muncul "App isn't available", pastikan akun Facebook kamu sudah ditambahkan sebagai <strong>Tester/Developer</strong> di Meta App Dashboard ({app_id}).
                </small>
            </div>
        </div>

        <script>
            window.fbAsyncInit = function() {{
                FB.init({{
                    appId            : '{app_id}',
                    autoLogAppEvents : true,
                    xfbml            : true,
                    version          : '{api_version}'
                }});
            }};
        </script>
        <script async defer crossorigin="anonymous" src="https://connect.facebook.net/en_US/sdk.js"></script>
        <script>

            let sessionCode = null;
            let sessionWabaId = null;
            let sessionPhoneId = null;

            window.addEventListener('message', (event) => {{
                if (event.origin.includes('facebook.com')) {{
                    try {{
                        const data = JSON.parse(event.data);
                        if (data.type === 'WA_EMBEDDED_SIGNUP') {{
                            if (data.data) {{
                                sessionWabaId = data.data.waba_id;
                                sessionPhoneId = data.data.phone_number_id;
                                checkAndComplete();
                            }}
                        }}
                    }} catch (e) {{}}
                }}
            }});

            function launchMetaSignup() {{
                showStatus('loading', 'Membuka Meta Embedded Signup popup...');
                FB.login(function(response) {{
                    if (response.authResponse && response.authResponse.code) {{
                        sessionCode = response.authResponse.code;
                        checkAndComplete();
                    }} else {{
                        showStatus('error', 'Login Meta dibatalkan / gagal. Jika muncul "App isn\'t available", tambahkan akun FB kamu ke App Roles Tester di Meta Dashboard.');
                    }}
                }}, {{
                    config_id: '{config_id}',
                    response_type: 'code',
                    override_default_response_type: true,
                    extras: {{
                        setup: {{ version: 2 }}
                    }}
                }});
            }}

            function launchMetaSignupStandard() {{
                showStatus('loading', 'Membuka Facebook Login (Izin Standar)...');
                FB.login(function(response) {{
                    if (response.authResponse && response.authResponse.code) {{
                        sessionCode = response.authResponse.code;
                        checkAndComplete();
                    }} else {{
                        showStatus('error', 'Login Facebook dibatalkan / gagal.');
                    }}
                }}, {{
                    scope: 'whatsapp_business_management,whatsapp_business_messaging',
                    response_type: 'code',
                    override_default_response_type: true
                }});
            }}

            function checkAndComplete() {{
                if (sessionCode) {{
                    showStatus('loading', 'Menghubungkan WhatsApp Business ke Assistant...');
                    fetch('/v1/meta/signup/complete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            agent_id: '{agent_id}',
                            code: sessionCode,
                            waba_id: sessionWabaId || '',
                            phone_number_id: sessionPhoneId || '',
                            business_name: '{agent.name}'
                        }})
                    }})
                    .then(r => r.json())
                    .then(data => {{
                        if (data.connected) {{
                            showStatus('success', '✅ WhatsApp Business Berhasil Dihubungkan! Silakan kembali ke chat Arthur dan ketik "sudah".');
                            document.getElementById('signup-btn').style.display = 'none';
                        }} else {{
                            showStatus('error', 'Gagal menghubungkan: ' + (data.error || 'Unknown error'));
                        }}
                    }})
                    .catch(err => {{
                        showStatus('error', 'Terjadi kesalahan koneksi ke server: ' + err.message);
                    }});
                }}
            }}

            function showStatus(type, msg) {{
                const box = document.getElementById('status-box');
                box.className = 'status-box ' + type;
                box.innerHTML = msg;
            }}
        </script>
    </body>
    </html>
    """
    return Response(content=html_content, media_type="text/html")


@router.get("/callback")
async def meta_signup_oauth_callback(
    code: str = Query(..., description="Authorization code from Facebook OAuth"),
    state: str = Query(..., description="Agent ID passed as OAuth state parameter"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """OAuth callback endpoint invoked by Meta/Facebook after user authorizes WhatsApp access."""
    try:
        agent_id = uuid.UUID(state)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid agent state ID")

    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    try:
        # Step 1: Exchange authorization code for token
        token_data = await exchange_code_for_token(code)
        access_token = token_data["access_token"]

        # Step 2: Retrieve WABA and phone number from Meta
        waba_id = token_data.get("waba_id", "")
        phone_number_id = token_data.get("phone_number_id", "")
        display_phone = ""
        business_name = ""

        if not waba_id:
            from app.core.infra.meta_embedded_signup import get_shared_waba_id
            waba_id = await get_shared_waba_id(access_token) or ""

        if waba_id:
            try:
                await subscribe_waba_to_webhooks(waba_id, access_token)
                phone_numbers = await get_waba_phone_numbers(waba_id, access_token)
                if phone_numbers:
                    phone_number_id = phone_numbers[0].get("id", phone_number_id)
                    display_phone = phone_numbers[0].get("display_phone_number", "")
                    business_name = phone_numbers[0].get("verified_name", "")
            except Exception as exc:
                logger.warning("meta_callback.waba_details_warning", error=str(exc))

        # Step 3: Save credentials on Agent model
        agent.wa_waba_id = waba_id
        agent.wa_phone_number_id = phone_number_id
        agent.wa_access_token_encrypted = encrypt_value(access_token)
        agent.wa_display_phone = display_phone
        agent.wa_business_name = business_name
        agent.wa_connection_type = "cloud_api"
        agent.channel_type = "whatsapp"
        agent.version += 1

        db.add(agent)
        await db.commit()

        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>WhatsApp Connected</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                .card { background: #1e293b; border-radius: 16px; padding: 40px; text-align: center; max-width: 420px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
                .icon { font-size: 64px; margin-bottom: 16px; }
                h1 { font-size: 24px; color: #22c55e; margin-bottom: 12px; }
                p { font-size: 15px; color: #94a3b8; line-height: 1.6; }
                .btn { display: inline-block; margin-top: 24px; background: #2563eb; color: #fff; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-weight: 600; }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="icon">✅</div>
                <h1>WhatsApp Business Connected!</h1>
                <p>Akun WhatsApp Business kamu berhasil dihubungkan ke Assistant.</p>
                <p>Silakan tutup halaman ini dan kembali ke chat WhatsApp Arthur. Ketik <strong>"sudah"</strong> atau <strong>"cek status"</strong>.</p>
            </div>
        </body>
        </html>
        """
        return Response(content=html_content, media_type="text/html")
    except Exception as exc:
        logger.error("meta_callback.failed", error=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Meta OAuth error: {exc}")


@router.post("/complete", response_model=EmbeddedSignupStatusResponse)
async def complete_embedded_signup(
    payload: EmbeddedSignupCompleteRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> EmbeddedSignupStatusResponse:
    """Complete Meta Embedded Signup by exchanging code for token and binding WABA to Agent."""
    result = await db.execute(
        select(Agent).where(Agent.id == payload.agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Step 1: Exchange code for long-lived access token
    try:
        token_data = await exchange_code_for_token(payload.code)
        access_token = token_data["access_token"]
    except Exception as exc:
        logger.error("meta_signup.token_exchange_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to exchange code for token: {exc}",
        )

    # Step 2: Subscribe WABA to webhooks
    try:
        await subscribe_waba_to_webhooks(payload.waba_id, access_token)
    except Exception as exc:
        logger.warning("meta_signup.webhook_subscribe_warning", error=str(exc))

    # Step 3: Get display phone number
    display_phone = ""
    try:
        phone_numbers = await get_waba_phone_numbers(payload.waba_id, access_token)
        for phone in phone_numbers:
            if phone.get("id") == payload.phone_number_id:
                display_phone = phone.get("display_phone_number", "")
                break
    except Exception as exc:
        logger.warning("meta_signup.get_phone_warning", error=str(exc))

    # Step 4: Encrypt access token and update Agent model
    agent.wa_waba_id = payload.waba_id
    agent.wa_phone_number_id = payload.phone_number_id
    agent.wa_access_token_encrypted = encrypt_value(access_token)
    agent.wa_display_phone = display_phone
    agent.wa_business_name = payload.business_name or ""
    agent.wa_connection_type = "cloud_api"
    agent.channel_type = "whatsapp"
    agent.version += 1

    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    logger.info(
        "meta_signup.completed",
        agent_id=str(agent.id),
        waba_id=payload.waba_id,
        phone_number_id=payload.phone_number_id,
    )

    return EmbeddedSignupStatusResponse(
        agent_id=agent.id,
        connected=True,
        connection_type="cloud_api",
        waba_id=agent.wa_waba_id,
        phone_number_id=agent.wa_phone_number_id,
        display_phone=agent.wa_display_phone,
        business_name=agent.wa_business_name,
    )


@router.get("/status/{agent_id}", response_model=EmbeddedSignupStatusResponse)
async def get_signup_status(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> EmbeddedSignupStatusResponse:
    """Check Embedded Signup status for an agent."""
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    is_connected = bool(agent.wa_phone_number_id and agent.wa_connection_type == "cloud_api")

    return EmbeddedSignupStatusResponse(
        agent_id=agent.id,
        connected=is_connected,
        connection_type=agent.wa_connection_type,
        waba_id=agent.wa_waba_id,
        phone_number_id=agent.wa_phone_number_id,
        display_phone=agent.wa_display_phone,
        business_name=agent.wa_business_name,
    )


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_embedded_signup(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> Response:
    """Disconnect Cloud API WABA connection from an agent."""
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    agent.wa_phone_number_id = None
    agent.wa_waba_id = None
    agent.wa_access_token_encrypted = None
    agent.wa_display_phone = None
    agent.wa_business_name = None
    agent.wa_connection_type = None
    agent.version += 1

    db.add(agent)
    await db.commit()

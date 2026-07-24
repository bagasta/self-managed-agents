"""
Integrations API — proxy endpoint untuk Google Workspace OAuth.
Arthur memanggil GET /v1/integrations/google/auth-link untuk generate auth URL.
"""
from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.engine.agent_identity import _session_sender_phone
from app.core.engine.agent_policy import build_agent_runtime_policy
from app.core.utils.phone_utils import normalize_phone
from app.database import get_db
from app.deps import verify_api_key as require_api_key
from app.models.agent import Agent
from app.models.session import Session

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/integrations", tags=["integrations"])

settings = get_settings()


class GoogleOAuthSuccessEvent(BaseModel):
    external_user_id: str
    agent_id: str | None = None
    google_email: str | None = None


def _oauth_identity_candidates(external_user_id: str) -> list[str]:
    raw = str(external_user_id or "").strip()
    if not raw:
        return []

    without_jid = raw.split("@", 1)[0]
    normalized = normalize_phone(without_jid)
    values = [raw, without_jid, normalized]
    if normalized:
        values.extend(
            [
                f"+{normalized}",
                f"{normalized}@s.whatsapp.net",
            ]
        )
        if normalized.startswith("62"):
            values.append("0" + normalized[2:])

    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = str(value or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _google_oauth_success_message(google_email: str | None) -> str:
    account_line = (
        f"Akun *{google_email.strip()}* sudah terhubung."
        if google_email and google_email.strip()
        else "Akun Google kamu sudah terhubung."
    )
    return (
        "✅ *Autentikasi Google berhasil*\n\n"
        f"{account_line} Sekarang kamu bisa kembali ke chat ini dan menggunakan "
        "fitur Google Workspace melalui agent sesuai izin yang kamu berikan."
    )


async def _deliver_google_oauth_success_whatsapp(
    *,
    db: AsyncSession,
    event: GoogleOAuthSuccessEvent,
) -> tuple[dict, int]:
    candidates = _oauth_identity_candidates(event.external_user_id)
    if not candidates:
        return {"notified": False, "reason": "invalid_external_user_id"}, 400

    result = await db.execute(
        select(Session, Agent)
        .join(Agent, Session.agent_id == Agent.id)
        .where(Session.channel_type == "whatsapp")
        .where(
            or_(
                Session.external_user_id.in_(candidates),
                Agent.owner_external_id.in_(candidates),
            )
        )
        .order_by(Session.updated_at.desc(), Session.created_at.desc())
        .limit(50)
    )
    rows = list(result.all())
    if not rows:
        logger.warning(
            "integrations.google.oauth_success_session_not_found",
            external_user_id=event.external_user_id,
            agent_id=event.agent_id or "",
        )
        return {"notified": False, "reason": "whatsapp_session_not_found"}, 404

    target_agent_id = str(event.agent_id or "").strip()
    normalized_candidates = {normalize_phone(value.split("@", 1)[0]) for value in candidates}
    normalized_candidates.discard("")

    def _score(row: tuple[Session, Agent]) -> tuple[int, int, int]:
        session, agent = row
        sender = _session_sender_phone(session)
        direct_match = int(bool(sender and sender in normalized_candidates))
        policy = build_agent_runtime_policy(
            agent,
            agent.tools_config if isinstance(agent.tools_config, dict) else {},
        )
        builder_match = int(policy.is_builder)
        target_match = int(bool(target_agent_id and str(agent.id) == target_agent_id))
        return direct_match, builder_match, target_match

    session, sending_agent = max(rows, key=_score)
    channel_config = dict(session.channel_config or {})
    if not channel_config.get("device_id") and sending_agent.wa_device_id:
        channel_config["device_id"] = sending_agent.wa_device_id

    target_phone = _session_sender_phone(session) or normalize_phone(event.external_user_id)
    if not channel_config.get("device_id") or not target_phone:
        logger.warning(
            "integrations.google.oauth_success_delivery_route_missing",
            session_id=str(session.id),
            has_device=bool(channel_config.get("device_id")),
            has_target=bool(target_phone),
        )
        return {"notified": False, "reason": "whatsapp_delivery_route_missing"}, 404

    from app.core.infra.channel_service import send_message

    send_result = await send_message(
        channel_type="whatsapp",
        channel_config=channel_config,
        text=_google_oauth_success_message(event.google_email),
        to_override=target_phone,
    )
    if send_result is None:
        logger.error(
            "integrations.google.oauth_success_send_failed",
            session_id=str(session.id),
            agent_id=str(sending_agent.id),
        )
        return {"notified": False, "reason": "whatsapp_send_failed"}, 502

    logger.info(
        "integrations.google.oauth_success_notified",
        session_id=str(session.id),
        agent_id=str(sending_agent.id),
        google_agent_id=target_agent_id,
    )
    return {"notified": True}, 200


def _integration_service_url() -> str:
    url = str(settings.google_integration_service_url).rstrip("/")
    if not url:
        raise RuntimeError("GOOGLE_INTEGRATION_SERVICE_URL is not configured")
    return url


@router.get("/google/auth-link", dependencies=[Depends(require_api_key)])
async def get_google_auth_link(
    external_user_id: str = Query(...),
    agent_id: str = Query(...),
    scopes: str | None = Query(None, description="Comma-separated OAuth scopes. If omitted, integration service uses its defaults."),
) -> JSONResponse:
    """
    Generate Google OAuth auth URL untuk user tertentu.
    Arthur memanggil endpoint ini via http_get untuk dapat link auth.
    """
    try:
        body: dict = {"external_user_id": external_user_id, "agent_id": agent_id}
        if scopes:
            body["scopes"] = [s.strip() for s in scopes.split(",") if s.strip()]
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_integration_service_url()}/v1/integrations/google/connect",
                json=body,
                headers={"X-API-Key": settings.api_key},
            )
        if resp.status_code == 200:
            data = resp.json()
            auth_url = data.get("auth_url") or data.get("authorization_url", "")
            return JSONResponse({"auth_url": auth_url})
        logger.warning("integrations.google.auth_link_failed", status=resp.status_code)
        return JSONResponse({"error": resp.text[:200]}, status_code=resp.status_code)
    except Exception as exc:
        logger.error("integrations.google.auth_link_error", error=str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/google/status", dependencies=[Depends(require_api_key)])
async def get_google_status(
    external_user_id: str = Query(...),
    agent_id: str = Query(None),
) -> JSONResponse:
    """Cek apakah user sudah connect Google."""
    try:
        params: dict = {"external_user_id": external_user_id}
        if agent_id:
            params["agent_id"] = agent_id
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_integration_service_url()}/v1/integrations/google/status",
                params=params,
                headers={"X-API-Key": settings.api_key},
            )
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/google/oauth-success", dependencies=[Depends(require_api_key)])
async def notify_google_oauth_success(
    event: GoogleOAuthSuccessEvent,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Receive a post-commit OAuth event and notify the matching WhatsApp user."""
    payload, status_code = await _deliver_google_oauth_success_whatsapp(db=db, event=event)
    return JSONResponse(payload, status_code=status_code)

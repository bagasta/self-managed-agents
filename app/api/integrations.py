"""
Integrations API — proxy endpoint untuk Google Workspace OAuth.
Arthur memanggil GET /v1/integrations/google/auth-link untuk generate auth URL.
"""
from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.deps import verify_api_key as require_api_key

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/integrations", tags=["integrations"])

settings = get_settings()


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

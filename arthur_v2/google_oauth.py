"""Google Workspace MCP OAuth client owned by Arthur V2."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


@dataclass(frozen=True)
class GoogleOAuthStartResult:
    """Outcome of asking the integration service to connect Google.

    ``connected`` is deliberately distinct from ``auth_url``: the service
    returns an empty URL when an active connection already exists.  Treating
    that idempotent response as an error left the target assistant without its
    MCP configuration.
    """

    connected: bool
    auth_url: str | None
    email: str | None = None


def google_mcp_url() -> str:
    url = str(get_settings().workspace_mcp_url or "").strip()
    if not url:
        raise RuntimeError("WORKSPACE_MCP_URL belum dikonfigurasi")
    return url


async def start_google_oauth(
    *, external_user_id: str, agent_id: str, scopes: list[str]
) -> GoogleOAuthStartResult:
    settings = get_settings()
    base_url = str(settings.google_integration_service_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("GOOGLE_INTEGRATION_SERVICE_URL belum dikonfigurasi")
    body: dict[str, Any] = {"external_user_id": external_user_id, "agent_id": agent_id}
    if scopes:
        body["scopes"] = scopes
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{base_url}/v1/integrations/google/connect",
            json=body,
            headers={"X-API-Key": settings.api_key},
        )
    response.raise_for_status()
    data = response.json()
    url = str(data.get("auth_url") or data.get("authorization_url") or "").strip()
    email = str(data.get("email") or "").strip() or None
    if bool(data.get("connected")):
        return GoogleOAuthStartResult(connected=True, auth_url=None, email=email)
    if not url:
        raise RuntimeError("integration service tidak mengembalikan auth_url")
    return GoogleOAuthStartResult(connected=False, auth_url=url, email=email)


async def get_google_oauth_status(*, external_user_id: str, agent_id: str) -> dict[str, Any]:
    settings = get_settings()
    base_url = str(settings.google_integration_service_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("GOOGLE_INTEGRATION_SERVICE_URL belum dikonfigurasi")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{base_url}/v1/integrations/google/status",
            params={"external_user_id": external_user_id, "agent_id": agent_id},
            headers={"X-API-Key": settings.api_key},
        )
    response.raise_for_status()
    return response.json() if response.content else {}

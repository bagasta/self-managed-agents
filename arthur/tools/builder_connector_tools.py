"""Workspace/app connector tools for Arthur builder."""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import httpx
import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.engine.google_mcp_support import _candidate_external_user_ids
from app.core.domain.agent_ownership import owner_filter
from app.core.utils.wa_identity import is_probable_whatsapp_lid
from app.models.agent import Agent

logger = structlog.get_logger(__name__)

SettingsProvider = Callable[[], Any]
LoggerProvider = Callable[[], Any]


def build_builder_connector_tools(
    *,
    db_factory: async_sessionmaker,
    owner_phone: str | None,
    get_settings: SettingsProvider,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _get_settings = get_settings
    _get_logger = get_logger or (lambda: logger)

    async def _agent_oauth_scopes(agent_id: str) -> list[str]:
        """Read the persisted, server-owned scope policy; never accept scopes from the LLM."""
        try:
            target_id = uuid.UUID(str(agent_id))
        except (TypeError, ValueError):
            return []
        async with db_factory() as db:
            statement = select(Agent).where(Agent.id == target_id)
            if owner_phone:
                statement = statement.where(owner_filter(owner_phone))
            result = await db.execute(statement)
            agent = result.scalar_one_or_none()
        if agent is None:
            return []
        config = getattr(agent, "tools_config", None)
        mcp = config.get("mcp") if isinstance(config, dict) and isinstance(config.get("mcp"), dict) else {}
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else mcp
        google = servers.get("google_workspace") if isinstance(servers, dict) else {}
        return list(google.get("oauth_scopes") or []) if isinstance(google, dict) else []

    @tool
    async def generate_google_auth_link(
        agent_id: str,
        external_user_id: str,
    ) -> str:
        """
        Generate link untuk user connect akun Google mereka ke agent tertentu.
        Gunakan tool ini setiap kali user minta link auth Google, atau setelah
        create/update agent yang punya integrasi Google Workspace.

        Setelah dapat auth_url, kirimkan HANYA link-nya ke user — jangan tampilkan
        endpoint, parameter teknis, atau istilah internal/protokol tool.

        Args:
            agent_id: ID agent yang akan dihubungkan ke Google
            external_user_id: ID user saat ini (dari session yang sedang berjalan)
        """
        settings = _get_settings()
        integration_url = str(settings.google_integration_service_url).rstrip("/")
        if not integration_url:
            return "[error] GOOGLE_INTEGRATION_SERVICE_URL belum dikonfigurasi; auth Google Workspace harus memakai URL dev tunnel."

        if is_probable_whatsapp_lid(external_user_id):
            return (
                "[error] Nomor WhatsApp asli user belum tersedia, jadi link login Google belum bisa dibuat. "
                "Minta user chat dari nomor WhatsApp biasa atau pastikan wa-service mengirim phone_from, bukan LID."
            )
        candidate_user_ids = [
            candidate
            for candidate in _candidate_external_user_ids(external_user_id, external_user_id)
            if not is_probable_whatsapp_lid(candidate)
        ]
        if not candidate_user_ids:
            return (
                "[error] Nomor WhatsApp asli user belum tersedia, jadi link login Google belum bisa dibuat. "
                "Minta user chat dari nomor WhatsApp biasa atau pastikan wa-service mengirim phone_from, bukan LID."
            )

        scopes = await _agent_oauth_scopes(agent_id)
        if not scopes:
            return (
                "[error] Policy OAuth Google agent belum dikonfigurasi. "
                "Konfigurasikan ulang layanan Google agent ini sebelum membuat link; "
                "default permission luas tidak diizinkan."
            )

        last_status = ""
        last_body = ""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                for candidate in candidate_user_ids:
                    resp = await client.post(
                        f"{integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": candidate, "agent_id": agent_id, "scopes": scopes},
                        headers={"X-API-Key": settings.api_key},
                    )
                    last_status = str(resp.status_code)
                    last_body = resp.text[:300]
                    if resp.status_code != 200:
                        continue
                    data = resp.json() if resp.text else {}
                    auth_url = data.get("auth_url") or data.get("authorization_url", "")
                    if auth_url:
                        return json.dumps(
                            {
                                "auth_url": auth_url,
                                "external_user_id": candidate,
                                "integration_url_used": integration_url,
                            },
                            ensure_ascii=False,
                        )
                    last_body = resp.text[:300] or "response JSON tidak mengandung auth_url"
            return (
                f"[error] Gagal generate link Google. status={last_status or 'no_response'} "
                f"body={last_body or '-'}"
            )
        except httpx.TimeoutException as exc:
            _get_logger().error(
                "builder_tools.generate_google_auth_link.error",
                error_type=type(exc).__name__,
                error=repr(exc),
                integration_url=integration_url,
                candidates=candidate_user_ids,
            )
            return (
                f"[error] Timeout saat menghubungi Google integration service di {integration_url}. "
                "Pastikan service integration jalan dan GOOGLE_INTEGRATION_SERVICE_URL/WORKSPACE_MCP_PREFER_LOCAL benar."
            )
        except httpx.HTTPError as exc:
            _get_logger().error(
                "builder_tools.generate_google_auth_link.error",
                error_type=type(exc).__name__,
                error=repr(exc),
                integration_url=integration_url,
                candidates=candidate_user_ids,
            )
            return f"[error] Gagal menghubungi Google integration service ({type(exc).__name__}): {exc!r}"
        except Exception as exc:
            _get_logger().error(
                "builder_tools.generate_google_auth_link.error",
                error_type=type(exc).__name__,
                error=repr(exc),
                integration_url=integration_url,
                candidates=candidate_user_ids,
            )
            return f"[error] Gagal generate link Google ({type(exc).__name__}): {exc!r}"

    return {"generate_google_auth_link": generate_google_auth_link}

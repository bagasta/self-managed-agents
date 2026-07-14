"""Workspace/app connector tools for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import structlog
from langchain_core.tools import tool

from app.core.engine.google_mcp_support import _candidate_external_user_ids
from app.core.utils.wa_identity import is_probable_whatsapp_lid

logger = structlog.get_logger(__name__)

SettingsProvider = Callable[[], Any]
LoggerProvider = Callable[[], Any]


def build_builder_connector_tools(
    *,
    get_settings: SettingsProvider,
    get_logger: LoggerProvider | None = None,
    owner_external_id: str | None = None,
) -> dict[str, Any]:
    _get_settings = get_settings
    _get_logger = get_logger or (lambda: logger)

    @tool
    async def generate_google_auth_link(
        agent_id: str,
        external_user_id: str = "",
    ) -> str:
        """
        Generate link untuk user connect akun Google mereka ke agent tertentu.
        Gunakan tool ini setiap kali user minta link auth Google, atau setelah
        create/update agent yang punya integrasi Google Workspace.

        Setelah dapat auth_url, kirimkan HANYA link-nya ke user — jangan tampilkan
        endpoint, parameter teknis, atau istilah internal/protokol tool.

        Args:
            agent_id: ID agent yang akan dihubungkan ke Google
            external_user_id: Fallback legacy. Runtime memakai owner agent yang
                sudah terikat oleh backend; model tidak perlu mengisi field ini.
        """
        settings = _get_settings()
        integration_url = str(settings.google_integration_service_url).rstrip("/")
        if not integration_url:
            return "[error] GOOGLE_INTEGRATION_SERVICE_URL belum dikonfigurasi; auth Google Workspace harus memakai URL dev tunnel."

        stable_owner_id = str(owner_external_id or "").strip()
        requested_user_id = str(external_user_id or "").strip()
        identity_source = stable_owner_id or requested_user_id
        if is_probable_whatsapp_lid(identity_source):
            return (
                "[error] Nomor WhatsApp asli user belum tersedia, jadi link login Google belum bisa dibuat. "
                "Minta user chat dari nomor WhatsApp biasa atau pastikan wa-service mengirim phone_from, bukan LID."
            )
        candidate_user_ids = [
            candidate
            for candidate in _candidate_external_user_ids(identity_source, identity_source)
            if not is_probable_whatsapp_lid(candidate)
        ]
        if not candidate_user_ids:
            return (
                "[error] Nomor WhatsApp asli user belum tersedia, jadi link login Google belum bisa dibuat. "
                "Minta user chat dari nomor WhatsApp biasa atau pastikan wa-service mengirim phone_from, bukan LID."
            )

        last_status = ""
        last_body = ""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                for candidate in candidate_user_ids:
                    for status_agent_id in (agent_id, None):
                        params = {"external_user_id": candidate}
                        if status_agent_id:
                            params["agent_id"] = status_agent_id
                        status_resp = await client.get(
                            f"{integration_url}/v1/integrations/google/status",
                            params=params,
                            headers={"X-API-Key": settings.api_key},
                        )
                        if status_resp.status_code != 200:
                            continue
                        status_data = status_resp.json() if status_resp.text else {}
                        if bool(status_data.get("connected")):
                            return json.dumps(
                                {
                                    "connected": True,
                                    "auth_url": "",
                                    "already_connected": True,
                                    "connection_scope": "owner",
                                    "google_email": status_data.get("email"),
                                },
                                ensure_ascii=False,
                            )

                    # Arthur-created agents share one owner-level Google
                    # connection. Per-agent access remains controlled by the
                    # agent tools_config, without forcing another OAuth login.
                    resp = await client.post(
                        f"{integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": candidate, "agent_id": None},
                        headers={"X-API-Key": settings.api_key},
                    )
                    last_status = str(resp.status_code)
                    last_body = resp.text[:300]
                    if resp.status_code != 200:
                        continue
                    data = resp.json() if resp.text else {}
                    if bool(data.get("connected")):
                        return json.dumps(
                            {
                                "connected": True,
                                "auth_url": "",
                                "already_connected": True,
                                "connection_scope": "owner",
                                "google_email": data.get("email"),
                            },
                            ensure_ascii=False,
                        )
                    auth_url = data.get("auth_url") or data.get("authorization_url", "")
                    if auth_url:
                        return json.dumps(
                            {
                                "connected": False,
                                "auth_url": auth_url,
                                "already_connected": False,
                                "connection_scope": "owner",
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

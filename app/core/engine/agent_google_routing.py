"""Google Workspace routing helpers.

Extracted from agent_runner — pure refactor, zero behaviour change.
Handles auth-URL extraction, MCP server filtering, and customer/owner routing
for Google Workspace blockers.
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any

import structlog

from app.core.engine.agent_identity import (
    _is_customer_whatsapp_session,
    _normalized_agent_operator_ids,
    _owner_notification_target,
    _session_real_phone,
    _session_sender_phone,
)
from app.core.engine.agent_step_utils import (
    _operator_message_payload,
    _parse_step_result_json,
)
from app.core.engine.google_mcp_support import (
    _candidate_external_user_ids,
    _fetch_google_auth_link,
)
from app.core.utils.phone_utils import normalize_phone
from app.models.session import Session

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exported helpers
# ---------------------------------------------------------------------------

def _extract_auth_url_from_builder_steps(steps: list[dict[str, Any]]) -> str | None:
    for step in reversed(steps or []):
        if step.get("tool") != "generate_google_auth_link":
            continue
        data = _parse_step_result_json(step.get("result"))
        if data:
            auth_url = data.get("auth_url") or data.get("authorization_url")
            if auth_url:
                return str(auth_url)
        result_text = str(step.get("result") or "")
        match = re.search(r"https?://[^\s\"'<>]+", result_text)
        if match:
            return match.group(0).rstrip(".,)")
    return None


def _builder_google_auth_agent_id(steps: list[dict[str, Any]]) -> str | None:
    if any((step or {}).get("tool") == "generate_google_auth_link" for step in steps or []):
        return None
    for step in reversed(steps or []):
        if step.get("tool") not in {"create_agent", "update_agent"}:
            continue
        data = _parse_step_result_json(step.get("result"))
        if not data or data.get("success") is not True:
            continue
        readback = data.get("readback") if isinstance(data.get("readback"), dict) else {}
        needs_auth = (
            data.get("needs_google_auth") is True
            or data.get("google_workspace_enabled") is True
            or readback.get("tools_config_has_google_workspace") is True
        )
        if needs_auth:
            agent_id = str(data.get("agent_id") or "").strip()
            if agent_id:
                return agent_id
    return None


async def _append_builder_google_auth_link_if_needed(
    final_reply: str,
    *,
    steps: list[dict[str, Any]],
    session: Session,
    settings_obj: Any,
    log: Any,
) -> str:
    auth_url = _extract_auth_url_from_builder_steps(steps)
    agent_id = _builder_google_auth_agent_id(steps)
    if not auth_url and not agent_id:
        return final_reply

    if not auth_url and agent_id:
        try:
            auth_url = await _fetch_google_auth_link(
                integration_url=str(settings_obj.google_integration_service_url).rstrip("/"),
                api_key=settings_obj.api_key,
                agent_id=uuid.UUID(agent_id),
                candidate_user_ids=_candidate_external_user_ids(
                    session.external_user_id,
                    _session_real_phone(session),
                ),
            )
        except Exception as exc:
            log.warning("agent_run.builder_google_auth_link_fetch_failed", agent_id=agent_id, error=str(exc))
            auth_url = None

    if not auth_url:
        if "link login google" in (final_reply or "").lower():
            return final_reply
        return (
            (final_reply or "").strip()
            + "\n\nIntegrasi Google sudah aktif, tapi link login Google belum berhasil dibuat otomatis. "
            "Kirim 'buatkan link Google' dan saya akan coba generate ulang."
        ).strip()

    if auth_url in (final_reply or ""):
        return final_reply

    log.info(
        "agent_run.builder_google_auth_link_appended",
        agent_id=agent_id or "",
        auth_url_host=auth_url.split("/", 3)[2] if "://" in auth_url else "",
    )
    return (
        (final_reply or "").strip()
        + f"\n\nLink login Google: {auth_url}\nBuka link ini dulu supaya agent bisa akses Google Workspace."
    ).strip()


def _google_workspace_server_has_auth(runtime: Any) -> bool:
    server = getattr(runtime, "workspace_server", None)
    if not isinstance(server, dict):
        return False
    headers = server.get("headers", {})
    if not isinstance(headers, dict):
        return False
    auth = headers.get("Authorization") or headers.get("authorization")
    return bool(str(auth or "").strip())


def _remove_google_workspace_mcp_server(tools_config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with google_workspace MCP removed.

    Google Workspace MCP runs in external OAuth mode. Opening the MCP client
    before a per-user bearer token exists only produces a transport-level 401,
    so the run should surface the dev-tunnel auth link instead.
    """
    copied = copy.deepcopy(tools_config)
    mcp_cfg = copied.get("mcp", {})
    if not isinstance(mcp_cfg, dict):
        return copied

    if "servers" in mcp_cfg or "enabled" in mcp_cfg:
        servers = mcp_cfg.get("servers", {})
        if isinstance(servers, dict):
            servers.pop("google_workspace", None)
            if not servers:
                mcp_cfg["enabled"] = False
        return copied

    mcp_cfg.pop("google_workspace", None)
    return copied


def _google_workspace_customer_blocker_reply(*, notified_owner: bool) -> str:
    if notified_owner:
        return (
            "Maaf, jadwalnya belum bisa saya finalkan otomatis sekarang. "
            "Data pesanan Anda sudah saya catat dan sudah saya teruskan ke Owner untuk dicek. "
            "Nanti akan dikonfirmasi kembali."
        )
    return (
        "Maaf, jadwalnya belum bisa saya finalkan otomatis sekarang. "
        "Data pesanan Anda sudah saya catat, tapi saya perlu Owner mengecek sistem penjadwalan dulu. "
        "Nanti akan dikonfirmasi kembali."
    )


def _is_google_workspace_mcp_authorized_for_session(session: Session, agent_model: Any) -> bool:
    """Only owner/admin/operator WhatsApp senders may access Google MCP tools."""
    if getattr(session, "channel_type", None) != "whatsapp":
        return True
    sender = _session_sender_phone(session)
    if not sender:
        return False
    return sender in _normalized_agent_operator_ids(agent_model)


def _google_workspace_mcp_unauthorized_reply() -> str:
    return (
        "Maaf, aksi yang terhubung ke Google Workspace hanya bisa dijalankan "
        "oleh Admin/operator agent ini."
    )


async def _route_google_workspace_blocker_to_owner_if_customer(
    *,
    reply: str,
    session: Session,
    agent_model: Any,
    user_message: str,
    error_text: str,
    auth_url: str | None,
    log: Any,
) -> str:
    """For operational WA customers, Google blockers are internal owner incidents."""
    from app.core.engine.agent_policy import build_agent_runtime_policy

    # Builder sessions (Arthur) talk directly to their own owner; the operational
    # customer-blocker persona ("pesanan"/"jadwal") never applies there.
    tools_config = getattr(agent_model, "tools_config", None)
    policy = build_agent_runtime_policy(
        agent_model, tools_config if isinstance(tools_config, dict) else {}
    )
    if policy.is_builder:
        return reply

    if not _is_customer_whatsapp_session(session, agent_model):
        return reply

    cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    device_id = str(cfg.get("device_id") or getattr(agent_model, "wa_device_id", "") or "").strip()
    owner_target = _owner_notification_target(agent_model)
    notified_owner = False

    if device_id and owner_target:
        agent_name = str(getattr(agent_model, "name", "") or "agent").strip()
        sender = _session_sender_phone(session)
        owner_text = (
            f"Perlu tindakan Owner untuk {agent_name}.\n\n"
            "Ada customer yang sedang dibantu, tapi aksi Google/Calendar belum bisa dijalankan karena koneksi akun perlu dicek.\n\n"
            f"Customer: {sender or '-'}\n"
            f"Pesan terakhir: {user_message.strip()[:500] or '-'}\n"
            f"Error ringkas: {str(error_text or '').strip()[:500] or '-'}"
        )
        if auth_url:
            owner_text += (
                "\n\nBuka link ini untuk hubungkan ulang Google:\n"
                f"{auth_url}\n\n"
                "Setelah selesai, balas customer atau minta agent melanjutkan jadwalnya."
            )
        else:
            owner_text += (
                "\n\nLink reconnect belum berhasil dibuat otomatis. "
                "Cek pengaturan integrasi Google agent ini, lalu lanjutkan konfirmasi ke customer."
            )
        try:
            from app.core.infra.channel_service import send_message

            await send_message(
                channel_type="whatsapp",
                channel_config={**cfg, "user_phone": owner_target, "device_id": device_id},
                text=owner_text,
            )
            notified_owner = True
            log.info(
                "agent_run.google_workspace_blocker_notified_owner",
                owner=normalize_phone(owner_target),
                auth_url_present=bool(auth_url),
            )
        except Exception as exc:
            log.warning(
                "agent_run.google_workspace_blocker_owner_notify_failed",
                owner=normalize_phone(owner_target),
                error=str(exc)[:200],
            )
    else:
        log.warning(
            "agent_run.google_workspace_blocker_owner_notify_missing_target",
            device_id_present=bool(device_id),
            owner_target_present=bool(owner_target),
        )

    return _google_workspace_customer_blocker_reply(notified_owner=notified_owner)


def _is_google_chat_intent(user_message: str) -> bool:
    text = _operator_message_payload(user_message).lower()
    return any(
        marker in text
        for marker in (
            "google chat",
            "gchat",
            "google space",
            "space google",
            "spaces/",
            "chat space",
        )
    )

"""Main agent orchestration entry point.

`run_agent()` coordinates run records, LLM setup, tools, prompt/context,
graph execution, result persistence, and post-run memory extraction. Detailed
tool setup, HITL handling, MCP support, callbacks, and input preparation live
in sibling modules.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypedDict

import structlog
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.engine.context_service import count_user_messages, db_messages_to_lc, load_history
from app.core.domain.agent_sop_service import get_latest_agent_operating_manual
from app.core.domain.memory_service import build_memory_context, extract_long_term_memory, load_layered_memory
from app.core.engine.prompt_builder import (
    build_mcp_tool_priority_notice,
    build_rag_context,
    build_system_prompt,
    maybe_summarize_context,
)
from app.core.engine.tool_builder import _is_enabled
from app.core.engine.agent_callbacks import AgentStepLogger
from app.core.engine.agent_hitl import handle_graph_interrupt, handle_pending_interrupt
from app.core.engine.agent_input import build_input_messages
from app.core.engine.agent_llm import build_agent_llms
from app.core.engine.agent_recovery import send_agent_recovery_message
from app.core.engine.agent_identity import (
    _is_customer_whatsapp_session,
    _normalized_agent_operator_ids,
    _owner_notification_target,
    _session_real_phone,
    _session_sender_phone,
)
from app.core.engine.agent_tool_setup import build_agent_tool_setup
from app.core.engine.agent_policy import (
    AgentRuntimePolicy,
    build_agent_runtime_policy,
    should_block_external_service_fallback_tool,
    should_use_google_workspace_parent_only,
)
from app.models.agent import Agent as AgentModel
from app.models.message import Message
from app.models.run import Run
from app.models.session import Session
from sqlalchemy import select
from app.core.engine.result_parser import (
    ParsedResult,
    sanitize_input_messages as _sanitize_input_messages,
    parse_agent_result,
)
from app.core.engine.reply_guard import ensure_non_empty_reply
from app.core.utils.phone_utils import normalize_phone
from app.core.domain.agent_quota_service import get_owner_subscription, is_quota_exempt_builder_agent
from app.core.domain.subscription_service import QuotaExceeded, assert_token_quota_available
from app.core.engine.google_mcp_support import (
    _build_google_mcp_auth_failure_reply,
    _build_google_mcp_unavailable_reply,
    _build_google_mcp_validation_reply,
    _candidate_external_user_ids,
    _contains_google_workspace_artifact,
    _extract_google_mcp_step_error,
    _extract_requested_slide_count,
    _fetch_google_auth_link,
    _has_google_mcp_step,
    _has_google_workspace_artifact_step,
    _is_google_auth_or_scope_error,
    _is_google_forms_authoring_intent,
    _is_google_mcp_intent,
    _is_google_sheets_authoring_intent,
    _is_google_slides_relayout_intent,
    _looks_like_progress_claim,
    _needs_google_forms_followup,
    _needs_google_sheets_followup,
    _needs_google_slides_followup,
    apply_google_mcp_reply_overrides,
    apply_mcp_error_notice,
    google_forms_create_retry_directive,
    google_forms_followup_directive,
    google_forms_followup_retry_directive,
    google_forms_request_kind_retry_directive,
    google_sheets_followup_directive,
    find_last_google_workspace_user_request,
    is_google_auth_recovery_followup,
    prepare_google_mcp_runtime,
    sanitize_google_forms_tools,
    google_slides_dimension_retry_directive,
    google_slides_followup_directive,
    google_slides_shape_retry_directive,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


_URL_RE = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")
_SHARED_WORKSPACE_FILE_RE = re.compile(r"(/workspace/shared/[^\s`'\"),]+)")


def _parse_step_result_json(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


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


async def _graph_result_from_output(
    *,
    graph: Any,
    graph_config: dict[str, Any],
    graph_output: Any,
    log: Any,
) -> dict[str, Any]:
    """Return graph state when a checkpointer exists, otherwise use ainvoke output."""
    try:
        state = await graph.aget_state(graph_config)
        if state is not None and isinstance(getattr(state, "values", None), dict):
            return dict(state.values)
    except ValueError as exc:
        if "No checkpointer set" not in str(exc):
            raise
        log.warning("agent_run.graph_state_unavailable_no_checkpointer")

    if isinstance(graph_output, dict):
        return graph_output
    values = getattr(graph_output, "values", None)
    if isinstance(values, dict):
        return dict(values)
    output = getattr(graph_output, "output", None)
    if isinstance(output, dict):
        return output
    return {}


def _task_result_guard_reply(final_reply: str, steps: list[dict[str, Any]], user_message: str) -> str:
    """Prevent parent agents from claiming subagent work succeeded when it did not."""
    if not steps:
        return final_reply

    task_results = [
        str(step.get("result") or "")
        for step in steps
        if step.get("tool") == "task" and step.get("result")
    ]
    if not task_results:
        return final_reply

    task_payload_text = "\n".join(
        json.dumps(step.get("args") or {}, ensure_ascii=False)
        for step in steps
        if step.get("tool") == "task"
    ).lower()
    user_lower = (user_message or "").lower()
    artifact_required = any(
        marker in f"{task_payload_text}\n{user_lower}"
        for marker in (
            "deploy",
            "trycloudflare",
            "website",
            "web app",
            "landing page",
            "html",
            "css",
            "javascript",
            "prototype",
            "portfolio",
            "portofolio",
            "aplikasi",
            "app web",
            "url",
            "link website",
            "file final",
            "dokumen final",
            "kirim file",
            "cv ats",
            "buat cv",
        )
    )

    combined = "\n".join(task_results)
    combined_lower = combined.lower()
    final_lower = (final_reply or "").lower()

    has_success_artifact = bool(
        _URL_RE.search(combined)
        or "[document_sent]" in combined_lower
        or "[image_sent]" in combined_lower
        or _has_whatsapp_media_send_step(steps)
        or " terkirim" in combined_lower
        or "deployment berhasil" in combined_lower
    )
    blocker_markers = (
        "belum menemukan",
        "belum menerima",
        "mohon bagikan",
        "tolong kirim",
        "perlu informasi",
        "butuh informasi",
        "tidak menemukan",
        "file cv",
        "isi cv",
    )
    has_blocker = any(marker in combined_lower for marker in blocker_markers)
    promise_markers = (
        "nanti",
        "sedang",
        "saya mulai",
        "saya langsung",
        "langsung buatkan",
        "akan saya",
        "hasilnya saya kirim",
        "lagi saya",
    )
    final_is_promise = any(marker in final_lower for marker in promise_markers)
    user_asks_status = any(k in user_lower for k in ("mana", "belum jadi", "udah jadi", "sudah jadi", "url", "link"))

    if not artifact_required:
        return final_reply
    if has_success_artifact:
        return final_reply
    if has_blocker:
        return (
            "Belum bisa saya lanjutkan karena bahan yang dibutuhkan belum tersedia di workspace agent. "
            "Subagent minta isi/file CV dikirim ulang atau ditempel di chat dulu, baru saya bisa buat web HTML/CSS/JS-nya."
        )
    if final_is_promise or user_asks_status:
        return (
            "Belum selesai. Subagent belum mengembalikan URL, file terkirim, atau hasil final yang bisa saya serahkan. "
            "Saya tidak akan klaim selesai sebelum ada output yang valid."
        )
    return final_reply


def _operator_escalation_reply_guard(
    final_reply: str,
    steps: list[dict[str, Any]],
    user_message: str,
    escalation_user_jid: str | None,
) -> str:
    """Block operator turns from hallucinating completed customer deliverables."""
    if not escalation_user_jid or not _is_operator_envelope(user_message):
        return final_reply
    if _has_reply_to_user_step(steps) or _has_send_to_number_step(steps):
        return final_reply

    text = (final_reply or "").strip()
    lowered = text.lower()
    if "draft" in lowered and ("ketik" in lowered or "sudah ok" in lowered):
        return final_reply

    deliverable_markers = (
        "cv",
        "file",
        "pdf",
        "dokumen",
        "document",
        "website",
        "web",
    )
    unsafe_completion_markers = (
        "sudah selesai",
        "selesai dibuat",
        "siap dikirim",
        "siap saya kirim",
        "berhasil dibuat",
        "akan saya kirim",
        "harus dilakukan secara manual",
    )
    if not (
        any(marker in lowered for marker in deliverable_markers)
        and any(marker in lowered for marker in unsafe_completion_markers)
    ):
        return final_reply

    operator_text = _operator_message_payload(user_message).lower()
    if any(marker in operator_text for marker in ("pembayaran", "transfer", "bayar", "payment", "paid", "valid", "approve")):
        return (
            "Draft pesan untuk customer:\n"
            "----\n"
            "Halo, pembayaran Anda sudah kami terima. Proses pembuatan CV akan kami lanjutkan, "
            "dan hasilnya akan kami kirimkan setelah siap.\n"
            "----\n"
            "Sudah OK? Ketik 'kirim' untuk saya teruskan ke customer."
        )

    return (
        "Saya belum mengirim atau membuat ulang deliverable dari sesi operator ini. "
        "Silakan tulis pesan yang ingin diteruskan ke customer, lalu ketik 'kirim' setelah draft-nya sudah OK."
    )


_DIRECT_WA_SEND_RE = re.compile(
    r"\b(kirim|send|wa|whatsapp)\b.{0,80}\b(pesan|message|wa|whatsapp)?\b.{0,120}(?:\+?62|08)\d{7,15}",
    re.IGNORECASE,
)
_DIRECT_WA_CONFIRM_WORDS = {
    "kirim",
    "kirim pesan",
    "kirim pesan wa",
    "kirim wa",
    "yes",
    "yes kirim",
    "ya",
    "ya kirim",
    "iya",
    "iya kirim",
    "ok",
    "ok kirim",
    "oke",
    "oke kirim",
    "lanjut",
    "lanjut kirim",
}
_WA_MEDIA_REQUEST_MARKERS = (
    "gambar",
    "foto",
    "image",
    "img",
    "dokumen",
    "document",
    "file",
    "pdf",
    "excel",
    "xlsx",
    "chart",
    "grafik",
)
_DIRECT_WA_META_REQUEST_MARKERS = (
    "agent",
    "arthur",
    "bas",
    "bug",
    "error",
    "log",
    "perbaiki",
    "benerin",
    "fix",
    "debug",
    "cek",
    "lihat",
    "kenapa",
    "masalah",
    "kendala",
    "gabisa",
    "gak bisa",
    "ga bisa",
    "tidak bisa",
    "konfigurasi",
    "config",
    "setting",
    "tools_config",
    "kemampuan",
    "disuruh",
)
_DIRECT_WA_TEXT_WRONG_TOOLS = {
    "send_message",  # Google Chat MCP; needs spaces/... and is not WhatsApp.
    "send_whatsapp_image",
    "send_whatsapp_document",
    "notify_user",
}


def _operator_message_payload(message: str) -> str:
    """Return the actual operator text from WA/API operator envelopes."""
    text = message or ""
    if text.startswith("[OPERATOR] "):
        return text.removeprefix("[OPERATOR] ").strip()
    if text.startswith("<OPERATOR>"):
        marker = "\nPesan:"
        idx = text.find(marker)
        if idx != -1:
            return text[idx + len(marker):].strip()
    return text


def _is_operator_envelope(message: str) -> bool:
    text = message or ""
    return text.startswith("[OPERATOR] ") or text.startswith("<OPERATOR>")


def _is_direct_whatsapp_send_confirmation(user_message: str) -> bool:
    text = _operator_message_payload(user_message).strip().lower()
    return text in _DIRECT_WA_CONFIRM_WORDS


def _is_direct_whatsapp_send_request(user_message: str) -> bool:
    text = _operator_message_payload(user_message).lower()
    if not text:
        return False
    if _is_direct_whatsapp_meta_request(user_message):
        return False
    if _DIRECT_WA_SEND_RE.search(text):
        return True
    has_send_word = any(word in text for word in ("kirim", "send"))
    has_message_word = any(word in text for word in ("pesan", "message", "wa", "whatsapp"))
    has_phone = bool(re.search(r"(?:\+?62|08)\d{7,15}", text))
    return has_send_word and has_message_word and has_phone


def _is_direct_whatsapp_meta_request(user_message: str) -> bool:
    """True when user discusses/fixes WA sending capability, not asks to send now."""
    if _is_operator_envelope(user_message):
        return False
    text = _operator_message_payload(user_message).lower()
    if not text:
        return False
    has_wa_send_topic = any(marker in text for marker in ("kirim wa", "kirim pesan", "whatsapp", "wa ke", "pesan wa"))
    if not has_wa_send_topic:
        return False
    return any(marker in text for marker in _DIRECT_WA_META_REQUEST_MARKERS)


def _is_direct_whatsapp_text_send_context(user_message: str, history_rows: list[Any] | None = None) -> bool:
    """Detect text-message-to-number turns so WhatsApp routing can prefer send_to_number."""
    text = _operator_message_payload(user_message).strip().lower()
    if _is_direct_whatsapp_meta_request(user_message):
        return False
    if any(marker in text for marker in _WA_MEDIA_REQUEST_MARKERS):
        return False
    if _is_direct_whatsapp_send_request(user_message):
        return True
    if text not in _DIRECT_WA_CONFIRM_WORDS:
        return False

    recent_contents = []
    for row in (history_rows or [])[-10:]:
        content = _operator_message_payload(getattr(row, "content", "") or "")
        if content:
            recent_contents.append(str(content).lower())
    recent_text = "\n".join(recent_contents)
    if _is_direct_whatsapp_meta_request(recent_text):
        return False
    if any(marker in recent_text for marker in _WA_MEDIA_REQUEST_MARKERS):
        return False
    has_recent_phone = bool(re.search(r"(?:\+?62|08)\d{7,15}", recent_text))
    has_recent_direct_send = any(
        marker in recent_text
        for marker in (
            "kirim pesan",
            "kirim wa",
            "whatsapp ke",
            "pesan whatsapp ke",
            "pesan wa ke",
            "nomor",
            "draft",
        )
    )
    return has_recent_phone and has_recent_direct_send


def _extract_direct_whatsapp_confirmation_payload(
    user_message: str,
    history_rows: list[Any] | None,
) -> tuple[str, str] | None:
    """Extract target phone and last confirmed text draft for deterministic WA send."""
    if not _is_direct_whatsapp_send_confirmation(user_message):
        return None

    rows = list(history_rows or [])[-12:]
    target_phone = ""
    for row in reversed(rows):
        content = _operator_message_payload(getattr(row, "content", "") or "")
        phones = re.findall(r"(?:\+?62|08)\d{7,15}", content)
        if phones:
            target_phone = phones[-1]
            break
    if not target_phone:
        return None

    draft = ""
    for row in reversed(rows):
        if getattr(row, "role", "") not in {"agent", "assistant"}:
            continue
        content = str(getattr(row, "content", "") or "").strip()
        if not content or content.lower().startswith("belum saya kirim"):
            continue

        quoted = re.findall(r'"([^"\n]{6,1000})"|“([^”\n]{6,1000})”', content)
        quote_candidates = [a or b for a, b in quoted if (a or b)]
        if quote_candidates:
            draft = quote_candidates[-1].strip()
            break

        marker_match = re.search(
            r"(?:draft(?:\s+untuk\s+[^:]+)?|pesan(?:\s+sopan)?(?:\s+untuk\s+[^:]+)?|isi pesan)\s*:\s*(.+)",
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if marker_match:
            candidate = marker_match.group(1).strip()
            candidate = re.split(
                r"\b(?:ketik|balas|sudah ok|sudah oke|konfirmasi)\b",
                candidate,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" \n\t\"'")
            if len(candidate) >= 6:
                draft = candidate
                break

    if not draft:
        return None
    return target_phone, draft


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


def _filter_whatsapp_unsafe_mcp_tools(mcp_tools: list[Any], *, user_message: str, log: Any) -> list[Any]:
    """Remove MCP tools whose names collide with WhatsApp intents in WhatsApp sessions."""
    if _is_google_chat_intent(user_message):
        return mcp_tools

    filtered: list[Any] = []
    removed: list[str] = []
    for tool in mcp_tools:
        name = getattr(tool, "name", "")
        if name == "send_message":
            removed.append(name)
            continue
        filtered.append(tool)
    if removed:
        log.info(
            "agent_run.whatsapp_mcp_tool_collision_filtered",
            removed=removed,
            reason="send_message_is_google_chat_not_whatsapp",
        )
    return filtered


def _prioritize_direct_whatsapp_text_send_tools(tools: list[Any], log: Any) -> list[Any]:
    """Remove ambiguous non-text/direct-WA tools and place send_to_number first."""
    send_tools = [tool for tool in tools if getattr(tool, "name", "") == "send_to_number"]
    if not send_tools:
        log.warning("agent_run.direct_wa_send_to_number_unavailable")
        return tools

    filtered: list[Any] = []
    removed: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", "")
        if name in _DIRECT_WA_TEXT_WRONG_TOOLS:
            removed.append(name)
            continue
        if name == "send_to_number":
            continue
        filtered.append(tool)
    log.info(
        "agent_run.direct_wa_text_tool_filter_applied",
        removed=removed,
        send_to_number_count=len(send_tools),
    )
    return send_tools + filtered


def _has_send_to_number_step(steps: list[dict[str, Any]]) -> bool:
    return any((step or {}).get("tool") == "send_to_number" for step in steps or [])


def _looks_like_direct_send_success_claim(final_reply: str) -> bool:
    text = (final_reply or "").lower()
    if not text:
        return False
    has_whatsapp_or_phone_context = bool(
        "whatsapp" in text
        or " wa " in f" {text} "
        or "nomor" in text
        or re.search(r"(?:\+?62|08)\d{7,15}", text)
    )
    if not has_whatsapp_or_phone_context:
        return False
    success_markers = (
        "sudah saya kirim",
        "sudah dikirim",
        "sudah terkirim",
        "berhasil dikirim",
        "telah saya kirim",
        "pesan whatsapp ke",
        "pesan wa ke",
        "terkirim ke",
    )
    return any(marker in text for marker in success_markers)


def _has_prior_send_to_number_evidence(messages: list[BaseMessage] | None) -> bool:
    for msg in messages or []:
        name = getattr(msg, "name", None)
        if name == "send_to_number":
            return True
        content = getattr(msg, "content", "")
        if "[SENT_TO_NUMBER" in str(content or ""):
            return True
        tool_calls = getattr(msg, "tool_calls", None) or []
        if any((tc or {}).get("name") == "send_to_number" for tc in tool_calls):
            return True
    return False


def _has_reply_to_user_step(steps: list[dict[str, Any]]) -> bool:
    return any((step or {}).get("tool") == "reply_to_user" for step in steps or [])


def _has_prior_reply_to_user_evidence(messages: list[BaseMessage] | None) -> bool:
    for msg in messages or []:
        name = getattr(msg, "name", None)
        if name == "reply_to_user":
            return True
        content = getattr(msg, "content", "")
        text = str(content or "")
        if "[SENT_TO_USER]" in text or "[TO_USER_MEDIA]" in text:
            return True
        tool_calls = getattr(msg, "tool_calls", None) or []
        if any((tc or {}).get("name") == "reply_to_user" for tc in tool_calls):
            return True
    return False


def _direct_whatsapp_send_guard_reply(
    final_reply: str,
    steps: list[dict[str, Any]],
    user_message: str,
    history_messages: list[BaseMessage] | None = None,
) -> str:
    """Prevent false WhatsApp-send success claims when send_to_number did not run."""
    if not _looks_like_direct_send_success_claim(final_reply):
        return final_reply
    if _is_operator_envelope(user_message):
        return final_reply
    if _has_reply_to_user_step(steps) or _has_prior_reply_to_user_evidence(history_messages):
        return final_reply
    if _has_send_to_number_step(steps) or _has_prior_send_to_number_evidence(history_messages):
        return final_reply
    if _is_direct_whatsapp_meta_request(user_message):
        return final_reply
    if not (
        _is_direct_whatsapp_send_request(user_message)
        or _is_direct_whatsapp_send_confirmation(user_message)
    ):
        return final_reply
    return (
        "Belum saya kirim. Saya tidak menemukan eksekusi tool kirim WhatsApp ke nomor tujuan, "
        "jadi saya tidak akan mengklaim pesan sudah terkirim. Ketik `kirim` jika draftnya sudah benar."
    )


def _has_external_service_fallback_blocked_step(steps: list[dict[str, Any]]) -> bool:
    marker = "This is a Google Workspace external-service action"
    return any(marker in str((step or {}).get("result", "") or "") for step in steps or [])


def _step_text(step: dict[str, Any]) -> str:
    return "\n".join(
        str(step.get(key) or "")
        for key in ("tool", "args", "result", "content")
        if step.get(key) is not None
    )


def _has_public_url_in_text(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


def _has_public_url_in_steps(steps: list[dict[str, Any]]) -> bool:
    return any(_has_public_url_in_text(_step_text(step)) for step in steps or [])


def _extract_shared_workspace_file_path(*values: Any) -> str | None:
    for value in values:
        text = str(value or "")
        for match in _SHARED_WORKSPACE_FILE_RE.findall(text):
            path = match.rstrip(".,;:)")
            name = path.rsplit("/", 1)[-1]
            if name and "." in name:
                return path
    return None


def _extract_shared_workspace_file_from_steps(
    steps: list[dict[str, Any]],
    final_reply: str = "",
) -> str | None:
    values: list[Any] = [final_reply]
    values.extend(_step_text(step) for step in reversed(steps or []))
    return _extract_shared_workspace_file_path(*values)


def _has_whatsapp_media_send_step(steps: list[dict[str, Any]]) -> bool:
    for step in steps or []:
        tool_name = str((step or {}).get("tool") or "")
        if tool_name not in {"send_whatsapp_document", "send_whatsapp_image"}:
            continue
        result = str((step or {}).get("result") or "")
        lower = result.lower()
        if "[error]" in lower or "gagal" in lower:
            continue
        if "[document_sent]" in lower or "[image_sent]" in lower or "terkirim" in lower or " dikirim " in lower:
            return True
    return False


def _is_whatsapp_file_delivery_request(user_message: str, steps: list[dict[str, Any]], final_reply: str) -> bool:
    text = "\n".join([user_message or "", final_reply or ""] + [_step_text(step) for step in steps or []]).lower()
    markers = (
        "siap_dikirim_parent",
        "kirim file",
        "kirim filenya",
        "file-nya",
        "filenya",
        "kirim dokumen",
        "kirim gambar",
        "kirim foto",
        "pdf",
        "docx",
        "xlsx",
        "csv",
        "zip",
        "dokumen",
        "attachment",
        "lampiran",
    )
    return any(marker in text for marker in markers)


def _needs_whatsapp_file_delivery_followup(
    user_message: str,
    tools_config: dict[str, Any],
    steps: list[dict[str, Any]],
    final_reply: str,
) -> tuple[bool, str | None]:
    """Detect subagent-created shared files that still need parent WA delivery."""
    if not _is_enabled(tools_config, "whatsapp_media", default=True):
        return False, None
    if _has_whatsapp_media_send_step(steps):
        return False, None
    path = _extract_shared_workspace_file_from_steps(steps, final_reply)
    if not path:
        return False, None
    if not _is_whatsapp_file_delivery_request(user_message, steps, final_reply):
        return False, None
    return True, path


def _whatsapp_file_delivery_followup_message(
    final_reply: str,
    steps: list[dict[str, Any]],
    shared_path: str,
) -> str:
    filename = shared_path.rsplit("/", 1)[-1] or "file"
    tool_names = ", ".join(
        str(step.get("tool") or "?")
        for step in (steps or [])[-8:]
        if step.get("tool")
    )
    return (
        "LANJUTKAN TASK SEBELUMNYA: subagent sudah membuat file final di shared workspace, "
        "tetapi parent belum mengirim file ke WhatsApp.\n\n"
        f"Path file final: {shared_path}\n"
        f"Filename: {filename}\n"
        f"Ringkasan jawaban sebelumnya: {(final_reply or '').strip()[:1200]}\n"
        f"Tool terakhir: {tool_names or '-'}\n\n"
        "Wajib sekarang panggil tool WhatsApp parent, bukan task/subagent. "
        "Untuk PDF/DOCX/XLSX/CSV/ZIP gunakan send_whatsapp_document(file_path_or_base64=path, filename=filename, caption=...). "
        "Untuk PNG/JPG/JPEG/WEBP gunakan send_whatsapp_image(image_path_or_base64=path, caption=...). "
        "Setelah tool mengembalikan sukses, jawab final singkat bahwa file sudah dikirim. "
        "Jika tool error, sampaikan error nyatanya tanpa mengklaim terkirim."
    )


def _is_website_or_app_request(user_message: str) -> bool:
    text = (user_message or "").lower()
    markers = (
        "website",
        "web site",
        "webapp",
        "web app",
        "landing page",
        "portfolio",
        "company profile",
        "profile page",
        "homepage",
        "frontend",
        "react",
        "next.js",
        "nextjs",
        "vue",
        "svelte",
        "astro",
        "html",
        "css",
        "dashboard",
        "situs",
        "halaman web",
        "aplikasi web",
        "buatkan web",
        "bikin web",
    )
    if any(marker in text for marker in markers):
        return True
    return bool(re.search(r"\bweb\b", text))


def _has_code_creation_evidence(steps: list[dict[str, Any]]) -> bool:
    direct_code_tools = {
        "write_file",
        "edit_file",
        "execute",
        "sandbox_write_binary_file",
    }
    code_markers = (
        "/workspace/src",
        "index.html",
        ".html",
        ".css",
        ".js",
        ".jsx",
        ".tsx",
        "package.json",
        "vite",
        "next",
        "react",
        "tailwind",
        "npm run build",
        "build berhasil",
        "file dibuat",
        "file berhasil",
        "berhasil dibuat",
        "sudah dibuat",
        "telah dibuat",
        "ditulis",
        "menulis file",
        "created",
        "wrote",
        "generated",
        "source code",
        "kode",
    )
    failure_markers = (
        "error",
        "failed",
        "gagal",
        "exception",
        "traceback",
        "not found",
    )
    for step in steps or []:
        tool_name = str(step.get("tool") or "")
        text = _step_text(step)
        lower = text.lower()
        if tool_name in direct_code_tools and not any(marker in lower for marker in failure_markers):
            return True
        if tool_name == "task" and any(marker in lower for marker in code_markers):
            return True
    return False


_BUILD_PROGRESS_TOOLS = frozenset(
    {
        "plan_agent",
        "compose_agent_blueprint",
        "compose_agent_instructions",
        "compose_agent_soul",
    }
)


def _needs_builder_create_completion(
    steps: list[dict[str, Any]],
    *,
    is_builder: bool,
) -> bool:
    """Detect a build that planned/composed an agent but never reached create_agent.

    Arthur (on a small model) often stops after plan_agent — e.g. to ask about
    Google — and never chains through to create_agent, leaving the user with a
    confusing "belum berhasil" loop. When that happens with no real plan/
    entitlement block, the runtime continues the build once internally instead
    of bouncing it back to the user.
    """
    if not is_builder:
        return False
    tool_names = {str(step.get("tool", "")).strip() for step in (steps or [])}
    # Only the create flow (which always starts with plan_agent) is in scope.
    if "plan_agent" not in tool_names:
        return False
    if not (tool_names & _BUILD_PROGRESS_TOOLS):
        return False
    if "create_agent" in tool_names or "update_agent" in tool_names:
        return False
    # A real plan/entitlement limit is not something to silently retry.
    for step in steps or []:
        result_text = str(step.get("result", "")).lower()
        if "entitlement" in result_text or "melebihi entitlement" in result_text:
            return False
    return True


def _builder_create_completion_directive() -> str:
    """Directive that pushes Arthur to finish the build through create_agent."""
    return (
        "LANJUTKAN PEMBUATAN AGENT SEKARANG SAMPAI SELESAI — JANGAN BERHENTI.\n"
        "Kamu sudah merencanakan/menyusun agent tapi belum memanggil create_agent. "
        "JANGAN bertanya konfirmasi lagi, JANGAN menawarkan Google lagi, JANGAN mengulang plan_agent. "
        "Langsung jalankan berurutan: compose_agent_blueprint (jika belum) -> compose_agent_instructions -> "
        "validate_agent_config -> create_agent, memakai konteks bisnis yang sudah ada. "
        "Kalau ada detail yang belum lengkap, pakai asumsi wajar dan tandai untuk direview nanti — "
        "jangan berhenti untuk bertanya. Setelah create_agent sukses, balas singkat dan natural bahwa agennya sudah jadi."
    )


def _needs_deploy_followup(
    user_message: str,
    tools_config: dict[str, Any],
    steps: list[dict[str, Any]],
    final_reply: str,
) -> bool:
    """Detect website/app work that stopped after coding without public deploy URL."""
    if not _is_enabled(tools_config, "deploy", default=False):
        return False
    if not _is_website_or_app_request(user_message):
        return False
    if _has_public_url_in_text(final_reply) or _has_public_url_in_steps(steps):
        return False
    return _has_code_creation_evidence(steps)


def _deploy_followup_message(final_reply: str, steps: list[dict[str, Any]], *, has_subagents: bool) -> str:
    tool_names = ", ".join(
        str(step.get("tool") or "?")
        for step in (steps or [])[-8:]
        if step.get("tool")
    )
    subagent_instruction = (
        "Jika file website dibuat di workspace sys_coder/subagent, panggil task() ke sys_coder dan instruksikan "
        "sys_coder untuk memanggil deploy_app() dari workspace-nya sendiri. Parent tidak boleh mencoba deploy "
        "workspace kosong yang berbeda."
        if has_subagents
        else "Panggil deploy_app() dari workspace sandbox yang berisi file website."
    )
    return (
        "LANJUTKAN TASK SEBELUMNYA: user meminta website/app dan agent ini memiliki deploy=true, "
        "tetapi percobaan sebelumnya belum mengembalikan URL public.\n\n"
        f"Ringkasan jawaban sebelumnya: {(final_reply or '').strip()[:1200]}\n"
        f"Tool terakhir: {tool_names or '-'}\n\n"
        "Wajib sekarang deploy hasil website/app dengan Cloudflare tunnel.\n"
        f"{subagent_instruction}\n"
        "Gunakan get_deployment_status() jika perlu, lalu deploy_app(command, port), lalu verifikasi status. "
        "Jangan berhenti pada menulis file/build. Jawaban akhir harus menyertakan URL https public dari deploy_app."
    )


class AgentRunResult(TypedDict):
    reply: str
    steps: list[dict]
    run_id: uuid.UUID
    tokens_used: int
    usage: dict[str, Any]


class BlockTaskToolMiddleware(AgentMiddleware):
    """Block Deep Agents task delegation for flows that must execute in parent."""

    name = "BlockTaskToolMiddleware"

    def _blocked_message(self, request: Any) -> ToolMessage | None:
        tool_name = getattr(getattr(request, "tool", None), "name", None)
        if tool_name != "task":
            return None
        tool_call = getattr(request, "tool_call", {}) or {}
        return ToolMessage(
            content=(
                "The task tool is disabled for this run. Execute the requested "
                "Google Workspace action directly with the connected Google Workspace tools."
            ),
            tool_call_id=tool_call.get("id", ""),
            name="task",
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        blocked = self._blocked_message(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        blocked = self._blocked_message(request)
        if blocked is not None:
            return blocked
        return await handler(request)


class ExternalServiceFallbackGuardMiddleware(AgentMiddleware):
    """Reject local/delegated fallback tools for external-service side effects."""

    name = "ExternalServiceFallbackGuardMiddleware"

    def __init__(
        self,
        *,
        policy: AgentRuntimePolicy,
        google_workspace_mcp_available: bool,
        user_message: str,
    ) -> None:
        self._policy = policy
        self._google_workspace_mcp_available = google_workspace_mcp_available
        self._user_message = user_message

    def _blocked_message(self, request: Any) -> ToolMessage | None:
        tool_name = getattr(getattr(request, "tool", None), "name", None) or ""
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_payload = tool_call.get("args", tool_call)
        if not should_block_external_service_fallback_tool(
            policy=self._policy,
            tool_name=tool_name,
            tool_payload=tool_payload,
            user_message=self._user_message,
            google_workspace_mcp_available=self._google_workspace_mcp_available,
        ):
            return None
        return ToolMessage(
            content=(
                "This is a Google Workspace external-service action. Do not use "
                "sandbox, filesystem, or task delegation as a fallback. Call the "
                "relevant Google Workspace tool directly, or return the Google "
                "auth/unavailable blocker if the integration cannot run."
            ),
            tool_call_id=tool_call.get("id", ""),
            name=tool_name,
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        blocked = self._blocked_message(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        blocked = self._blocked_message(request)
        if blocked is not None:
            return blocked
        return await handler(request)


async def run_agent(
    *,
    agent_model: AgentModel,
    session: Session,
    user_message: str,
    db: AsyncSession,
    escalation_user_jid: str | None = None,
    escalation_context: str | None = None,
    media_image_b64: str | None = None,
    media_image_mime: str | None = None,
    sender_name: str | None = None,
    prior_run_was_interrupted: bool = False,
) -> AgentRunResult:
    """
    Jalankan agent end-to-end:
    1. Setup LLM + sandbox
    2. Build tools berdasarkan tools_config
    3. Build sub-agents (jika enabled)
    4. Inject RAG + context summary + memory ke system prompt
    5. Load history, run graph
    6. Persist messages ke DB
    7. Auto-extract long-term memory (jika triggered)
    """
    run_id = uuid.uuid4()
    agent_id: uuid.UUID = session.agent_id
    _raw_tools_cfg = agent_model.tools_config
    tools_config: dict[str, Any] = _raw_tools_cfg if isinstance(_raw_tools_cfg, dict) else {}
    temperature: float = getattr(agent_model, "temperature", 0.7)

    log = logger.bind(
        run_id=str(run_id),
        session_id=str(session.id),
        agent_id=str(agent_id),
        model=agent_model.model,
    )
    log.info("agent_run.start")

    abandoned_before_current = (
        await db.execute(
            select(Run)
            .where(
                Run.session_id == session.id,
                Run.status == "abandoned",
            )
            .order_by(Run.completed_at.desc().nullslast(), Run.started_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()

    # --- Create Run record (status: running) ---
    _now = datetime.now(timezone.utc)
    run_record = Run(
        id=run_id,
        session_id=session.id,
        status="running",
        started_at=_now,
    )
    db.add(run_record)

    await db.flush()

    # HITL action_requests use .get("name", ...) and .get("args", ...);
    # compatibility tests also assert .get("name", ...) remains documented here.
    resumed_result = await handle_pending_interrupt(
        session=session,
        user_message=user_message,
        db=db,
        run_record=run_record,
        run_id=run_id,
        log=log,
    )
    if resumed_result is not None:
        return AgentRunResult(**resumed_result)

    # --- Token quota pre-run gate ---
    # Block before LLM is built or invoked when subscription quota is exhausted.
    # Builder/system agents are exempt (platform infrastructure).
    # NOTE: overlaps check_agent_quota in agent_quota_service.py; kept as explicit pre-LLM gate.
    if not is_quota_exempt_builder_agent(agent_model):
        try:
            _, _owner_sub = await get_owner_subscription(agent_model, db)
            if _owner_sub is not None:
                try:
                    assert_token_quota_available(_owner_sub)
                except QuotaExceeded as _qe:
                    log.warning("agent_run.quota_exceeded_pre_run", detail=str(_qe))
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.tokens_used = 0
                    await db.flush()
                    _quota_reply = (
                        "Maaf, kuota token subscription pemilik agent ini sudah habis. "
                        "Silakan upgrade plan atau tunggu reset kuota berikutnya."
                    )
                    return AgentRunResult(
                        reply=_quota_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=0,
                        usage={},
                    )
        except Exception as _quota_exc:
            log.warning(
                "agent_run.quota_lookup_failed_allow_run",
                error=str(_quota_exc),
            )

    llm_raw, llm = build_agent_llms(agent_model, settings, temperature)

    # Fetch operating manual early so it can be passed to tool setup for SOP gating.
    _early_operating_manual = await get_latest_agent_operating_manual(
        agent_id,
        db,
        fallback_tools_config=tools_config,
    )

    # Tool setup lives in agent_tool_setup.py; it still gates builder tools via
    # capabilities and build_builder_tools.
    tool_setup = await build_agent_tool_setup(
        agent_model=agent_model,
        session=session,
        tools_config=tools_config,
        raw_tools_config=_raw_tools_cfg,
        db=db,
        log=log,
        escalation_user_jid=escalation_user_jid,
        sender_name=sender_name,
        user_message=user_message,
        operating_manual=_early_operating_manual,
    )
    tools = tool_setup.tools
    active_groups = tool_setup.active_groups
    saved_custom_tools = tool_setup.saved_custom_tools
    sandbox = tool_setup.sandbox
    subagent_list = tool_setup.subagent_list
    sub_sandboxes = tool_setup.sub_sandboxes
    _memory_scope = tool_setup.memory_scope

    runtime_policy = build_agent_runtime_policy(agent_model, tools_config)
    google_mcp_parent_only = should_use_google_workspace_parent_only(
        policy=runtime_policy,
        user_message=user_message,
        tools_config=tools_config,
    )
    if google_mcp_parent_only and subagent_list:
        log.info(
            "agent_run.google_mcp_subagents_removed_before_prompt",
            subagents=len(subagent_list),
            reason="google_workspace_mcp_parent_only",
        )
        subagent_list = []
        active_groups = [
            group for group in active_groups if not str(group).startswith("subagents(")
        ]

    log.debug("agent_run.tools_ready (pre-mcp)", groups=active_groups, count=len(tools))

    # ------------------------------------------------------------------ #
    # 5. Context enrichment                                               #
    # ------------------------------------------------------------------ #
    rag_context = ""
    if _is_enabled(tools_config, "rag", default=False):
        rag_context = await build_rag_context(agent_id, user_message, db, tools_config, log)

    context_summary = await maybe_summarize_context(session, db, llm, log)

    memory_block = await build_memory_context(agent_id, db, scope=_memory_scope)
    layered_memory = await load_layered_memory(agent_id, db, scope=_memory_scope)
    # Reuse the operating manual already fetched before tool setup (avoids duplicate DB query).
    operating_manual = _early_operating_manual
    setattr(agent_model, "_runtime_operating_manual", operating_manual)

    # When a context summary is already injected into the system prompt (triggered
    # after context_summary_trigger messages), loading the full short_term_memory_turns
    # is redundant — the summary covers older turns.  Reduce to half the configured
    # limit so the LLM context stays manageable on long sessions.
    _history_turns = (
        max(settings.short_term_memory_turns // 2, 5)
        if context_summary
        else settings.short_term_memory_turns
    )
    history_rows = await load_history(session.id, db, max_turns=_history_turns)
    prior_messages = db_messages_to_lc(history_rows)
    google_auth_recovery_followup = is_google_auth_recovery_followup(
        user_message,
        history_rows,
    )
    google_auth_recovery_request = (
        find_last_google_workspace_user_request(history_rows)
        if google_auth_recovery_followup
        else None
    )
    execution_user_message = google_auth_recovery_request or user_message
    log.debug("agent_run.history_loaded", turns=len(prior_messages) // 2)
    direct_wa_text_send_context = (
        getattr(session, "channel_type", None) == "whatsapp"
        and _is_direct_whatsapp_text_send_context(user_message, history_rows)
    )
    if direct_wa_text_send_context:
        log.info("agent_run.direct_wa_text_send_context_detected")
    if google_auth_recovery_followup:
        log.info(
            "agent_run.google_mcp_auth_recovery_followup_detected",
            has_prior_request=bool(google_auth_recovery_request),
        )

    is_op_msg = _is_operator_envelope(user_message)

    # ------------------------------------------------------------------ #
    # 6. System prompt                                                    #
    # ------------------------------------------------------------------ #
    system_prompt = build_system_prompt(
        agent_model=agent_model,
        session=session,
        active_groups=active_groups,
        saved_custom_tools=saved_custom_tools,
        subagent_list=subagent_list,
        sender_name=sender_name,
        context_summary=context_summary,
        memory_block=memory_block,
        layered_memory=layered_memory,
        rag_context=rag_context,
        escalation_user_jid=escalation_user_jid,
        escalation_context=escalation_context,
        is_operator_message=is_op_msg,
        user_message=user_message,
    )
    if abandoned_before_current is not None:
        system_prompt += (
            "\n\n## Restart Recovery\n"
            "Run sebelumnya terhenti karena service restart sebelum selesai. "
            "JANGAN lanjutkan atau ulangi task lama secara otomatis. "
            "Fokus pada pesan user terbaru. Jika user bertanya status task lama, jelaskan singkat bahwa proses sebelumnya terhenti saat restart dan minta konfirmasi sebelum menjalankan ulang."
        )
    if google_auth_recovery_followup:
        _prior_google_request = (
            f"\nRequest Google Workspace yang harus dilanjutkan: {google_auth_recovery_request[:800]}"
            if google_auth_recovery_request
            else ""
        )
        system_prompt += (
            "\n\n## Google Workspace Auth Recovery\n"
            "Pesan user terbaru adalah konfirmasi bahwa OAuth Google sudah dicoba/diselesaikan. "
            "Lanjutkan request Google Workspace terakhir dari percakapan sebelumnya memakai tool Google Workspace langsung. "
            "Jika integrasi Google masih belum authorized, jangan gunakan task, sandbox, atau filesystem sebagai fallback; kirim blocker auth yang berisi link reconnect baru. "
            "Jangan menyebut istilah teknis internal/protokol tool kepada user."
            f"{_prior_google_request}"
        )
    if direct_wa_text_send_context:
        system_prompt += (
            "\n\n## Direct WhatsApp Text Send — Tool Lock\n"
            "Turn ini adalah konteks kirim pesan teks WhatsApp ke nomor tertentu. "
            "Satu-satunya tool pengiriman yang boleh dipakai untuk aksi ini adalah `send_to_number(phone_or_target, message)`.\n"
            "- Jangan gunakan `send_message` karena itu Google Chat/space, bukan WhatsApp.\n"
            "- Jangan gunakan `send_whatsapp_image` atau `send_whatsapp_document` karena user meminta pesan teks, bukan media.\n"
            "- Jangan gunakan `notify_user` untuk mengklaim sukses. Setelah `send_to_number` sukses, baru tulis final singkat ke operator/user.\n"
        )

    # ------------------------------------------------------------------ #
    # 7. Persist user message                                             #
    # ------------------------------------------------------------------ #
    step_base = max((m.step_index for m in history_rows), default=-1) + 1
    db.add(Message(
        session_id=session.id,
        role="user",
        content=user_message,
        step_index=step_base,
        run_id=run_id,
    ))
    await db.flush()

    # ------------------------------------------------------------------ #
    # 8. Run agent graph (with MCP tools)                                 #
    # ------------------------------------------------------------------ #
    from app.core.tools.mcp_tool import mcp_client_context

    # WA progress notify — only for WhatsApp sessions
    _ch_cfg: dict = session.channel_config if isinstance(session.channel_config, dict) else {}
    _wa_device_id: str = _ch_cfg.get("device_id", "")
    _wa_target: str = _ch_cfg.get("user_phone", "")
    _is_wa_session: bool = getattr(session, "channel_type", None) == "whatsapp" and bool(_wa_device_id and _wa_target)
    _wa_typing_started: bool = False

    async def _start_wa_run_typing() -> None:
        nonlocal _wa_typing_started
        if not _is_wa_session:
            return
        try:
            from app.core.infra.wa_client import start_wa_typing

            await start_wa_typing(_wa_device_id, _wa_target)
            _wa_typing_started = True
        except Exception as exc:
            log.warning("agent_run.wa_typing_start_failed", error=str(exc)[:200])

    async def _stop_wa_run_typing() -> None:
        if not _is_wa_session or not _wa_typing_started:
            return
        try:
            from app.core.infra.wa_client import stop_wa_typing

            await stop_wa_typing(_wa_device_id, _wa_target)
        except Exception as exc:
            log.warning("agent_run.wa_typing_stop_failed", error=str(exc)[:200])

    await _start_wa_run_typing()
    _google_fallback_external_user_id = getattr(agent_model, "owner_external_id", None)
    if not _google_fallback_external_user_id:
        _operator_ids = getattr(agent_model, "operator_ids", None)
        if isinstance(_operator_ids, list) and _operator_ids:
            _google_fallback_external_user_id = str(_operator_ids[0])

    google_mcp = await prepare_google_mcp_runtime(
        tools_config=tools_config,
        tools=tools,
        active_groups=active_groups,
        session=session,
        agent_id=agent_id,
        memory_scope=_memory_scope,
        api_key=settings.api_key,
        user_message=execution_user_message,
        system_prompt=system_prompt,
        log=log,
        fallback_external_user_id=_google_fallback_external_user_id,
    )
    system_prompt = google_mcp.system_prompt
    _google_mcp_auth_url = google_mcp.auth_url
    mcp_tools_config = tools_config
    if (
        google_mcp.enabled
        and google_mcp.workspace_server
        and not _google_workspace_server_has_auth(google_mcp)
    ):
        mcp_tools_config = _remove_google_workspace_mcp_server(tools_config)
        log.info(
            "agent_run.google_mcp_client_skipped_until_auth",
            reason="missing_per_user_bearer",
            auth_url_present=bool(_google_mcp_auth_url),
            integration_url=google_mcp.integration_url or None,
        )

    async with mcp_client_context(mcp_tools_config) as (mcp_tools, mcp_errors):
        if google_mcp.preflight_error and "google_workspace" not in mcp_errors:
            mcp_errors["google_workspace"] = google_mcp.preflight_error
        if mcp_tools:
            mcp_tools = sanitize_google_forms_tools(mcp_tools, log)
            if getattr(session, "channel_type", None) == "whatsapp":
                mcp_tools = _filter_whatsapp_unsafe_mcp_tools(
                    mcp_tools,
                    user_message=user_message,
                    log=log,
                )
            mcp_tool_names = [getattr(tool, "name", "") for tool in mcp_tools]
        if mcp_tools:
            if google_mcp_parent_only and subagent_list:
                log.info(
                    "agent_run.google_mcp_subagents_disabled",
                    subagents=len(subagent_list),
                    reason="google_workspace_mcp_must_run_in_parent",
                )
                subagent_list = []
            # Put MCP tools first so model/tool-router bias favors the connected
            # external service over sandbox helpers when both could appear useful.
            tools = mcp_tools + tools
            active_groups.append(f"mcp({len(mcp_tools)} tools)")
            if isinstance(system_prompt, str):
                system_prompt += build_mcp_tool_priority_notice(
                    mcp_tool_names=mcp_tool_names,
                    sandbox_active=sandbox is not None,
                )
            log.debug("agent_run.mcp_tools_added", count=len(mcp_tools), names=mcp_tool_names)
            if google_mcp_parent_only:
                task_tools = [
                    getattr(tool, "name", "")
                    for tool in tools
                    if getattr(tool, "name", "") == "task"
                ]
                if task_tools:
                    log.warning(
                        "agent_run.google_mcp_parent_only_task_tool_present",
                        count=len(task_tools),
                    )
        if direct_wa_text_send_context:
            tools = _prioritize_direct_whatsapp_text_send_tools(tools, log)
        if mcp_errors:
            log.warning("agent_run.mcp_errors", errors=mcp_errors)
            _google_mcp_auth_url, system_prompt = await apply_mcp_error_notice(
                mcp_errors=mcp_errors,
                runtime=google_mcp,
                agent_id=agent_id,
                memory_scope=_memory_scope,
                api_key=settings.api_key,
                system_prompt=system_prompt,
                log=log,
            )
            google_mcp_err = str(mcp_errors.get("google_workspace") or "")
            should_block_google_before_graph = (
                bool(google_mcp_err)
                and not mcp_tools
                and (
                    google_mcp_parent_only
                    or _is_google_mcp_intent(user_message)
                    or google_auth_recovery_followup
                )
            )
            if should_block_google_before_graph:
                if _is_google_auth_or_scope_error(google_mcp_err):
                    if not _google_mcp_auth_url:
                        _google_mcp_auth_url = await _fetch_google_auth_link(
                            integration_url=google_mcp.integration_url,
                            api_key=settings.api_key,
                            agent_id=agent_id,
                            candidate_user_ids=google_mcp.candidate_user_ids,
                        )
                    final_reply = await _build_google_mcp_auth_failure_reply(
                        llm=llm_raw,
                        user_message=execution_user_message,
                        error_text=google_mcp_err,
                        auth_url=_google_mcp_auth_url,
                    )
                else:
                    final_reply = _build_google_mcp_unavailable_reply(google_mcp_err)
                final_reply = await _route_google_workspace_blocker_to_owner_if_customer(
                    reply=final_reply,
                    session=session,
                    agent_model=agent_model,
                    user_message=execution_user_message,
                    error_text=google_mcp_err,
                    auth_url=_google_mcp_auth_url,
                    log=log,
                )
                log.warning(
                    "agent_run.google_mcp_blocked_before_graph",
                    error=google_mcp_err[:200],
                    auth_url_present=bool(_google_mcp_auth_url),
                )
                run_record.status = "completed"
                run_record.completed_at = datetime.now(timezone.utc)
                run_record.error_message = google_mcp_err[:2000]
                run_record.tokens_used = 0
                run_record.prompt_tokens = 0
                run_record.completion_tokens = 0
                run_record.reasoning_tokens = 0
                run_record.cached_tokens = 0
                run_record.openrouter_cost_usd = Decimal("0")
                run_record.usage_details = None
                db.add(Message(
                    session_id=session.id,
                    role="assistant",
                    content=final_reply,
                    step_index=step_base + 1,
                    run_id=run_id,
                ))
                await db.flush()
                await _stop_wa_run_typing()
                if sandbox:
                    await sandbox.aclose()
                for _ssb in sub_sandboxes:
                    await _ssb.aclose()
                return AgentRunResult(
                    reply=final_reply,
                    steps=[],
                    run_id=run_id,
                    tokens_used=0,
                    usage={
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "total_tokens": 0,
                        "openrouter_cost_usd": 0,
                        "details": None,
                    },
                )

        backend = None
        _checkpointer = None
        try:
            from langgraph.checkpoint.memory import MemorySaver
            from app.core.engine.deep_agent_backend import DockerBackend

            backend = DockerBackend(sandbox) if sandbox is not None else None
            _checkpointer = MemorySaver()
            # interrupt_on: tools_config["interrupt_on"] may be:
            #   - dict: {"tool_name": true}  → pass directly
            #   - list: ["tool_name"]         → convert to {name: True}
            _raw_interrupt_on = tools_config.get("interrupt_on") if isinstance(tools_config, dict) else None
            if isinstance(_raw_interrupt_on, dict) and _raw_interrupt_on:
                _interrupt_on: dict[str, bool] = _raw_interrupt_on
            elif isinstance(_raw_interrupt_on, list) and _raw_interrupt_on:
                _interrupt_on = {name: True for name in _raw_interrupt_on}
            else:
                _interrupt_on = {}
            if runtime_policy.is_builder:
                # Arthur is a control-plane agent. DeepAgents adds a generic
                # task tool even without explicit subagents, which lets Arthur
                # invent "updates" instead of calling builder tools.
                from langgraph.prebuilt import create_react_agent

                log.info(
                    "agent_run.builder_react_agent_mode",
                    reason="builder_must_not_receive_task_or_filesystem_tools",
                )
                graph = create_react_agent(
                    llm,
                    tools=tools,
                    prompt=system_prompt,
                    checkpointer=_checkpointer,
                )
            else:
                from deepagents import create_deep_agent

                # PENTING: gunakan llm_raw (bukan llm yang sudah .bind()) —
                # DeepAgents SDK memanggil .count() pada model untuk parse nama provider,
                # yang gagal pada RunnableBinding dan menyebabkan AttributeError ditangkap
                # sebagai TypeError → fallback ke create_react_agent tanpa backend.
                _dag_kwargs: dict[str, Any] = dict(
                    model=llm_raw,
                    tools=tools,
                    system_prompt=system_prompt,
                    backend=backend,
                    subagents=subagent_list or None,
                    checkpointer=_checkpointer,
                )
                _middleware: list[AgentMiddleware] = []
                if (
                    runtime_policy.policy_class == "operational"
                    and google_mcp.enabled
                    and google_mcp.workspace_server
                ):
                    _middleware.append(
                        ExternalServiceFallbackGuardMiddleware(
                            policy=runtime_policy,
                            google_workspace_mcp_available=True,
                            user_message=execution_user_message,
                        )
                    )
                if google_mcp_parent_only:
                    _middleware.append(BlockTaskToolMiddleware())
                    log.info(
                        "agent_run.google_mcp_deepagent_parent_only_mode",
                        reason="block_task_tool_keep_deepagents_runtime",
                    )
                if _middleware:
                    _dag_kwargs["middleware"] = _middleware
                if _interrupt_on:
                    _dag_kwargs["interrupt_on"] = _interrupt_on
                graph = create_deep_agent(**_dag_kwargs)
        except (ImportError, TypeError, AttributeError) as _dag_err:
            if sandbox is not None or backend is not None or subagent_list:
                log.error(
                    "agent_run.deepagent_required_failed",
                    error=str(_dag_err)[:300],
                    has_backend=backend is not None,
                    has_sandbox=sandbox is not None,
                    subagents=len(subagent_list or []),
                )
                raise
            log.warning(
                "agent_run.deepagent_fallback",
                error=str(_dag_err)[:300],
                has_sandbox=sandbox is not None,
            )
            from langgraph.prebuilt import create_react_agent
            graph = create_react_agent(
                llm,
                tools=tools,
                prompt=system_prompt,
                checkpointer=_checkpointer,
            )

        if media_image_b64 and media_image_mime:
            human_content: Any = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": f"data:{media_image_mime};base64,{media_image_b64}"}},
            ]
        else:
            human_content = user_message
            if google_auth_recovery_followup and google_auth_recovery_request:
                human_content = (
                    "Saya sudah menyelesaikan OAuth/reconnect Google. "
                    "Lanjutkan request Google Workspace sebelumnya sekarang dengan tool Google Workspace langsung:\n"
                    f"{google_auth_recovery_request}"
                )

        input_messages: list[BaseMessage] = build_input_messages(
            prior_messages=prior_messages,
            history_rows=history_rows,
            human_content=human_content,
            log=log,
        )
        step_counter = step_base + 1

        _progress_last_sent_at: float = 0.0
        _progress_sent_count: int = 0
        _progress_important_tools = {
            "task",
            "deploy_app",
            "execute",
            "send_whatsapp_document",
            "send_whatsapp_image",
            "plan_agent",
            "compose_agent_blueprint",
            "compose_agent_instructions",
            "compose_agent_soul",
            "validate_agent_config",
            "create_agent",
            "update_agent",
        }
        _progress_notice_task: asyncio.Task | None = None
        _progress_notice_sent: bool = False
        _progress_finished: bool = False
        _long_progress_notice_seconds = max(
            5.0,
            float(getattr(settings, "wa_long_progress_notice_seconds", 25.0) or 25.0),
        )

        async def _schedule_wa_long_progress_notice(reason: str) -> None:
            nonlocal _progress_last_sent_at, _progress_sent_count, _progress_notice_task, _progress_notice_sent
            if not _is_wa_session:
                return

            import time as _time
            now_ts = _time.monotonic()
            if _progress_notice_task is not None or _progress_notice_sent:
                return
            _progress_last_sent_at = now_ts

            async def _send_delayed_notice() -> None:
                nonlocal _progress_sent_count, _progress_notice_sent
                try:
                    await asyncio.sleep(_long_progress_notice_seconds)
                    if _progress_finished or _progress_notice_sent:
                        return
                    from app.core.infra.wa_client import send_wa_message, start_wa_typing

                    message = "Masih saya proses ya. Saya akan kirim hasilnya begitu selesai."
                    await send_wa_message(_wa_device_id, _wa_target, message)
                    with contextlib.suppress(Exception):
                        await start_wa_typing(_wa_device_id, _wa_target)
                    _progress_notice_sent = True
                    _progress_sent_count += 1
                    log.info("agent_run.wa_long_progress_notice_sent", reason=reason)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("agent_run.wa_long_progress_notice_failed", reason=reason, error=str(exc)[:200])

            _progress_notice_task = asyncio.create_task(_send_delayed_notice())

        async def _wa_progress_callback(tool_name: str, input_payload: Any, phase: str, output: Any | None) -> None:
            if tool_name == "notify_user":
                return
            if tool_name not in _progress_important_tools:
                return
            if phase != "start":
                return
            await _schedule_wa_long_progress_notice(tool_name)

        async def _cancel_wa_long_progress_notice() -> None:
            nonlocal _progress_finished, _progress_notice_task
            _progress_finished = True
            if _progress_notice_task is None:
                return
            _progress_notice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _progress_notice_task
            _progress_notice_task = None

        if _is_wa_session:
            await _schedule_wa_long_progress_notice("run")

        _agent_logger = AgentStepLogger(log,
            progress_callback=_wa_progress_callback if _is_wa_session else None,
        )

        def _usage_summary() -> dict[str, Any]:
            return {
                "prompt_tokens": _agent_logger.prompt_tokens_from_callbacks,
                "completion_tokens": _agent_logger.completion_tokens_from_callbacks,
                "reasoning_tokens": _agent_logger.reasoning_tokens_from_callbacks,
                "cached_tokens": _agent_logger.cached_tokens_from_callbacks,
                "total_tokens": _agent_logger.total_tokens_from_callbacks,
                "openrouter_cost_usd": round(_agent_logger.openrouter_cost_usd_from_callbacks, 8),
                "details": _agent_logger.usage_details,
            }

        def _apply_run_usage(total_tokens: int) -> None:
            summary = _usage_summary()
            run_record.tokens_used = int(total_tokens or summary["total_tokens"] or 0)
            run_record.prompt_tokens = int(summary["prompt_tokens"] or 0)
            run_record.completion_tokens = int(summary["completion_tokens"] or 0)
            run_record.reasoning_tokens = int(summary["reasoning_tokens"] or 0)
            run_record.cached_tokens = int(summary["cached_tokens"] or 0)
            run_record.openrouter_cost_usd = Decimal(str(summary["openrouter_cost_usd"] or 0))
            run_record.usage_details = summary["details"] or None

        _thread_id = str(session.id)
        _graph_config = {
            "recursion_limit": settings.agent_max_steps * 8,
            "callbacks": [_agent_logger],
            "configurable": {"thread_id": _thread_id},
        }

        async def _cleanup_sandboxes() -> None:
            await _stop_wa_run_typing()
            if sandbox:
                await sandbox.aclose()
            for _ssb in sub_sandboxes:
                await _ssb.aclose()

        direct_wa_confirmation_payload = (
            _extract_direct_whatsapp_confirmation_payload(user_message, history_rows)
            if direct_wa_text_send_context
            else None
        )
        if direct_wa_confirmation_payload:
            target_phone, draft_message = direct_wa_confirmation_payload
            steps = [
                {
                    "step": 1,
                    "tool": "send_to_number",
                    "args": {"phone_or_target": target_phone, "message": draft_message},
                    "result": "",
                }
            ]
            try:
                from app.core.infra.channel_service import send_message as _channel_send_message

                _raw_cfg = session.channel_config
                _channel_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
                await _channel_send_message(
                    channel_type=session.channel_type,
                    channel_config=_channel_cfg,
                    text=draft_message,
                    to_override=target_phone,
                )
                tool_result = f"[SENT_TO_NUMBER:{target_phone}] {draft_message}"
                steps[0]["result"] = tool_result
                final_reply = f"Pesan WhatsApp ke {target_phone} sudah saya kirim."
                log.info("agent_run.direct_wa_confirmation_sent", target=target_phone)
            except Exception as exc:
                tool_result = f"[error] Gagal kirim WhatsApp ke {target_phone}: {exc}"
                steps[0]["result"] = tool_result
                final_reply = f"Gagal mengirim pesan WhatsApp ke {target_phone}: {exc}"
                log.warning("agent_run.direct_wa_confirmation_send_failed", target=target_phone, error=str(exc)[:300])

            db.add(Message(
                session_id=session.id,
                role="tool",
                tool_name="send_to_number",
                tool_args={"phone_or_target": target_phone, "message": draft_message},
                tool_result=tool_result[:2000],
                step_index=step_counter,
                run_id=run_id,
            ))
            db.add(Message(
                session_id=session.id,
                role="agent",
                content=final_reply,
                step_index=step_counter + 1,
                run_id=run_id,
            ))
            run_record.status = "completed"
            run_record.completed_at = datetime.now(timezone.utc)
            _apply_run_usage(0)
            await db.flush()
            await _cleanup_sandboxes()
            return AgentRunResult(
                reply=final_reply,
                steps=steps,
                run_id=run_id,
                tokens_used=0,
                usage=_usage_summary(),
            )

        # Initialize so UnboundLocalError can't happen if an exception bypasses parse_agent_result
        final_reply: str = ""
        steps: list = []
        total_tokens_used: int = 0
        _graph_output: Any | None = None
        parsed: dict = {"final_reply": "", "steps": [], "total_tokens_used": 0, "has_output": False, "db_messages": []}

        _agent_caps = getattr(agent_model, "capabilities", []) or []
        _has_subagents = bool(
            isinstance(tools_config, dict)
            and tools_config.get("subagents", {})
            and tools_config["subagents"].get("enabled")
        )
        _is_builder_or_system_agent = "builder" in _agent_caps or "system" in _agent_caps
        if _is_builder_or_system_agent and not _has_subagents:
            _graph_config["recursion_limit"] = max(settings.agent_max_steps * 3, 24)
        # Agents that delegate to sys_coder may build framework projects (npm install,
        # next build, pip install) — these can take 5-10 min on cold sandboxes.
        # Keep that extended ceiling for subagent-enabled flows only. Arthur
        # builder runs without subagents should fail fast enough for WhatsApp.
        if _has_subagents:
            _timeout = settings.agent_timeout_seconds * 8
        elif _is_builder_or_system_agent:
            _timeout = min(settings.agent_timeout_seconds * 4, 540)
        else:
            _timeout = settings.agent_timeout_seconds
        try:
            async with asyncio.timeout(_timeout):
                _graph_output = await graph.ainvoke(
                    {"messages": input_messages},
                    config=_graph_config,
                    version="v2",
                )
                # GraphOutput (version="v2") may only carry .interrupts; when
                # a checkpointer exists, get messages from graph state.
                result = await _graph_result_from_output(
                    graph=graph,
                    graph_config=_graph_config,
                    graph_output=_graph_output,
                    log=log,
                )
        except asyncio.CancelledError:
            # Human interrupt — user sent a new message while this run was active.
            log.info("agent_run.cancelled_by_interrupt", session_id=str(session.id))
            run_record.status = "cancelled"
            run_record.completed_at = datetime.now(timezone.utc)
            run_record.error_message = "Cancelled because a newer user message interrupted this run."
            _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
            await db.flush()
            await _cancel_wa_long_progress_notice()
            await _cleanup_sandboxes()
            raise  # propagate so the task is properly marked cancelled
        except asyncio.TimeoutError:
            log.error(
                "agent_run.timeout",
                timeout_seconds=_timeout,
                session_id=str(session.id),
            )
            run_record.status = "timed_out"
            run_record.completed_at = datetime.now(timezone.utc)
            run_record.error_message = f"Timeout after {_timeout}s"
            await db.flush()
            await _cancel_wa_long_progress_notice()
            await _cleanup_sandboxes()
            await send_agent_recovery_message(
                is_wa_session=_is_wa_session,
                wa_device_id=_wa_device_id,
                wa_target=_wa_target,
                llm_raw=llm_raw,
                system_prompt=system_prompt,
                reason="memakan waktu terlalu lama dan terpaksa dihentikan",
                log=log,
            )
            raise
        except Exception as exc:
            err_str = str(exc)
            # Log sub-exceptions from ExceptionGroup for debugging
            if hasattr(exc, "exceptions"):
                for _sub in exc.exceptions:
                    log.error("agent_run.exception_group_sub", sub_type=type(_sub).__name__, sub_error=str(_sub))

            # JSONDecodeError inside a subagent = OpenRouter returned a truncated
            # HTTP response (network hiccup). Retry the same graph once with a brief
            # delay — no need to rebuild or sanitize messages.
            import json as _json_mod
            if isinstance(exc.__cause__, _json_mod.JSONDecodeError) or isinstance(exc, _json_mod.JSONDecodeError) or "JSONDecodeError" in type(exc).__name__ or (exc.__context__ and isinstance(exc.__context__, _json_mod.JSONDecodeError)):
                log.warning("agent_run.subagent_json_error_retry", error=err_str[:200])
                try:
                    await asyncio.sleep(1)
                    async with asyncio.timeout(_timeout):
                        _graph_output = await graph.ainvoke(
                            {"messages": input_messages},
                            config=_graph_config,
                            version="v2",
                        )
                        result = await _graph_result_from_output(
                            graph=graph,
                            graph_config=_graph_config,
                            graph_output=_graph_output,
                            log=log,
                        )
                    log.info("agent_run.subagent_json_error_retry_ok")
                except Exception as _retry_json_exc:
                    log.error("agent_run.subagent_json_error_retry_failed", error=str(_retry_json_exc)[:300])
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = str(_retry_json_exc)[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cancel_wa_long_progress_notice()
                    await _cleanup_sandboxes()
                    await send_agent_recovery_message(
                        is_wa_session=_is_wa_session,
                        wa_device_id=_wa_device_id,
                        wa_target=_wa_target,
                        llm_raw=llm_raw,
                        system_prompt=system_prompt,
                        reason="mengalami gangguan koneksi ke model",
                        log=log,
                    )
                    return AgentRunResult(
                        reply="Maaf, terjadi gangguan koneksi ke model. Silakan coba lagi.",
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

            # "No tool output found for function call" means the provider received
            # an AIMessage with tool_calls but no matching ToolMessage. This can
            # happen when the Deep Agents SDK drops a tool result mid-graph (e.g.
            # tool exception before ToolMessage is written to state).
            #
            # Retry strategy: rebuild graph using LangGraph's built-in
            # create_react_agent (more reliable tool execution than Deep Agents SDK)
            # with sanitized input so history is clean.
            elif "No tool output found for function call" in err_str:
                log.warning(
                    "agent_run.dangling_tool_call_retry",
                    error=err_str[:300],
                    input_msg_count=len(input_messages),
                )
                # Rebuild with create_react_agent as the fallback executor —
                # the Deep Agents SDK may have been the source of the dropped
                # tool result; LangGraph's ToolNode is the safer path here.
                try:
                    from langgraph.prebuilt import create_react_agent as _cra
                    _fallback_graph = _cra(llm, tools=tools, prompt=system_prompt)
                except Exception as _ge:
                    log.warning("agent_run.fallback_graph_build_failed", error=str(_ge))
                    _fallback_graph = graph

                clean_input = _sanitize_input_messages(input_messages)
                log.info(
                    "agent_run.dangling_tool_call_retry_with_fallback",
                    clean_msg_count=len(clean_input),
                )
                try:
                    async with asyncio.timeout(settings.agent_timeout_seconds):
                        result = await _fallback_graph.ainvoke(
                            {"messages": clean_input}, config=_graph_config
                        )
                    log.info("agent_run.dangling_tool_call_retry_ok")
                except Exception as retry_exc:
                    retry_err = str(retry_exc)
                    if "No tool output found for function call" in retry_err:
                        log.error(
                            "agent_run.dangling_tool_call_retry_failed",
                            error=retry_err[:300],
                        )
                        # Update Run → failed (dangling tool call)
                        run_record.status = "failed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        run_record.error_message = "Dangling tool call after retry"
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.commit()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply="Maaf, terjadi kesalahan internal. Silakan coba lagi.",
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )
                    log.error("agent_run.retry_error", error=retry_err)
                    # Update Run → failed
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = retry_err[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    raise retry_exc
            else:
                _recovered_via_retry = False
                _slides_invalid_page_target = (
                    "error calling tool 'batch_update_presentation'" in err_str.lower()
                    and "invalid slides batch update request" in err_str.lower()
                    and "targets a slide/page object" in err_str.lower()
                )

                _slides_invalid_dimension = (
                    "error calling tool 'batch_update_presentation'" in err_str.lower()
                    and (
                        "invalid value" in err_str.lower()
                        or "unknown dimension unit" in err_str.lower()
                        or "unit_unspecified" in err_str.lower()
                    )
                    and "dimension" in err_str.lower()
                    and ("create_shape" in err_str.lower() or "createshape" in err_str.lower())
                )

                if _slides_invalid_dimension:
                    log.warning(
                        "agent_run.slides_dimension_retry",
                        error=err_str[:300],
                    )
                    _slides_dim_retry_directive = google_slides_dimension_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _slides_prompt = (
                            (system_prompt + "\n\n" + _slides_dim_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                        _slides_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _slides_graph.ainvoke(
                                {"messages": _slides_input}, config=_graph_config
                            )
                        log.info("agent_run.slides_dimension_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _slides_retry_exc:
                        log.warning(
                            "agent_run.slides_dimension_retry_failed",
                            error=str(_slides_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _slides_invalid_page_target:
                    log.warning(
                        "agent_run.slides_shape_retry",
                        error=err_str[:300],
                    )
                    _slides_retry_directive = google_slides_shape_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _slides_prompt = (
                            (system_prompt + "\n\n" + _slides_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                        _slides_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _slides_graph.ainvoke(
                                {"messages": _slides_input}, config=_graph_config
                            )
                        log.info("agent_run.slides_shape_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _slides_retry_exc:
                        log.warning(
                            "agent_run.slides_shape_retry_failed",
                            error=str(_slides_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                _forms_create_title_only_error = (
                    "error calling tool 'create_form'" in err_str.lower()
                    and "only info.title can be set when creating a form" in err_str.lower()
                )
                _forms_request_kind_error = (
                    "error calling tool 'batch_update_form'" in err_str.lower()
                    and "request kind was not provided" in err_str.lower()
                )

                if _forms_create_title_only_error:
                    log.warning(
                        "agent_run.forms_create_retry",
                        error=err_str[:300],
                    )
                    _forms_retry_directive = google_forms_create_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_prompt = (
                            (system_prompt + "\n\n" + _forms_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_graph = _cra(llm, tools=tools, prompt=_forms_prompt)
                        _forms_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_graph.ainvoke(
                                {"messages": _forms_input}, config=_graph_config
                            )
                        log.info("agent_run.forms_create_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _forms_retry_exc:
                        log.warning(
                            "agent_run.forms_create_retry_failed",
                            error=str(_forms_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _forms_request_kind_error:
                    log.warning(
                        "agent_run.forms_request_kind_retry",
                        error=err_str[:300],
                    )
                    _forms_kind_retry_directive = google_forms_request_kind_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_kind_prompt = (
                            (system_prompt + "\n\n" + _forms_kind_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_kind_graph = _cra(llm, tools=tools, prompt=_forms_kind_prompt)
                        _forms_kind_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_kind_graph.ainvoke(
                                {"messages": _forms_kind_input}, config=_graph_config
                            )
                        log.info("agent_run.forms_request_kind_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _forms_kind_retry_exc:
                        log.warning(
                            "agent_run.forms_request_kind_retry_failed",
                            error=str(_forms_kind_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _recovered_via_retry:
                    log.info("agent_run.retry_recovered_continue")
                elif (
                    "validation error for call[batch_update_presentation]" in err_str.lower()
                    or ("missing required argument" in err_str.lower() and "requests" in err_str.lower())
                    or (
                        "error calling tool 'batch_update_presentation'" in err_str.lower()
                        and "invalid slides batch update request" in err_str.lower()
                    )
                    or (
                        "error calling tool 'create_form'" in err_str.lower()
                        and "only info.title can be set when creating a form" in err_str.lower()
                    )
                    or (
                        "error calling tool 'batch_update_form'" in err_str.lower()
                        and "request kind was not provided" in err_str.lower()
                    )
                ):
                    _reply = _build_google_mcp_validation_reply(err_str)
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    return AgentRunResult(
                        reply=_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

                if (not _recovered_via_retry) and _is_google_auth_or_scope_error(err_str):
                    if not _google_mcp_auth_url:
                        _google_mcp_auth_url = await _fetch_google_auth_link(
                            integration_url=google_mcp.integration_url,
                            api_key=settings.api_key,
                            agent_id=agent_id,
                            candidate_user_ids=google_mcp.candidate_user_ids,
                        )
                    _reply = await _build_google_mcp_auth_failure_reply(
                        llm=llm_raw,
                        user_message=execution_user_message,
                        error_text=err_str,
                        auth_url=_google_mcp_auth_url,
                    )
                    _reply = await _route_google_workspace_blocker_to_owner_if_customer(
                        reply=_reply,
                        session=session,
                        agent_model=agent_model,
                        user_message=execution_user_message,
                        error_text=err_str,
                        auth_url=_google_mcp_auth_url,
                        log=log,
                    )
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    return AgentRunResult(
                        reply=_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

                if not _recovered_via_retry:
                    log.error("agent_run.error", error=err_str)
                    # Update Run → failed
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = err_str[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    raise


        interrupt_result = await handle_graph_interrupt(
            graph_output=_graph_output,
            graph=graph,
            checkpointer=_checkpointer,
            thread_id=_thread_id,
            session=session,
            db=db,
            run_record=run_record,
            run_id=run_id,
            prior_messages=prior_messages,
            user_message=execution_user_message,
            cleanup_sandboxes=_cleanup_sandboxes,
            log=log,
        )
        if interrupt_result is not None:
            return AgentRunResult(**interrupt_result)

        # ------------------------------------------------------------------ #
        # 9. Parse & persist result messages                                  #
        # ------------------------------------------------------------------ #
        parsed: ParsedResult = parse_agent_result(
            result=result,
            input_messages=input_messages,
            session_id=session.id,
            run_id=run_id,
            step_start=step_counter,
            log=log,
        )
        final_reply = parsed["final_reply"]
        steps = parsed["steps"]
        # Prefer callback-based counter — it captures sub-agent LLM calls too.
        # Fall back to result_parser count if callback produced nothing (e.g. mocked LLM).
        _cb_tokens = _agent_logger.total_tokens_from_callbacks
        total_tokens_used = _cb_tokens if _cb_tokens > 0 else parsed["total_tokens_used"]
        if (
            _is_google_mcp_intent(execution_user_message)
            and mcp_tools
            and not _has_google_mcp_step(steps)
            and _has_external_service_fallback_blocked_step(steps)
        ):
            log.warning(
                "agent_run.google_mcp_retry_after_blocked_fallback",
                mcp_tools=len(mcp_tools),
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _mcp_only_prompt = (
                    (system_prompt if isinstance(system_prompt, str) else "")
                    + "\n\n## Google Workspace Tool Retry\n"
                    "The previous attempt incorrectly used a delegated/sandbox fallback for a Google Workspace action. "
                    "Retry now using only the Google Workspace tools. "
                    "Do not call task, filesystem, sandbox, or non-Google tools. "
                    "Return the Google Workspace URL only after the Google tool output contains it. "
                    "Do not mention internal tool protocol terms to the user."
                )
                _mcp_only_graph = _cra(llm, tools=mcp_tools, prompt=_mcp_only_prompt)
                _mcp_retry_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _mcp_only_graph.ainvoke(
                        {"messages": _mcp_retry_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                log.info("agent_run.google_mcp_retry_after_blocked_fallback_ok")
            except Exception as _mcp_retry_exc:
                log.warning(
                    "agent_run.google_mcp_retry_after_blocked_fallback_failed",
                    error=str(_mcp_retry_exc)[:300],
                )

        if _needs_builder_create_completion(steps, is_builder=runtime_policy.is_builder):
            log.warning(
                "agent_run.builder_create_completion_continue",
                steps=len(steps),
            )
            _create_completion_input = _sanitize_input_messages(input_messages)
            _create_completion_input.append(
                HumanMessage(content=_builder_create_completion_directive())
            )
            try:
                async with asyncio.timeout(_timeout):
                    _create_completion_output = await graph.ainvoke(
                        {"messages": _create_completion_input},
                        config=_graph_config,
                        version="v2",
                    )
                    result = await _graph_result_from_output(
                        graph=graph,
                        graph_config=_graph_config,
                        graph_output=_create_completion_output,
                        log=log,
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                log.info(
                    "agent_run.builder_create_completion_continue_ok",
                    created=any(str(s.get("tool", "")) == "create_agent" for s in steps),
                )
            except Exception as _create_completion_exc:
                log.warning(
                    "agent_run.builder_create_completion_continue_failed",
                    error=str(_create_completion_exc)[:300],
                )

        if _needs_deploy_followup(execution_user_message, tools_config, steps, final_reply):
            log.warning(
                "agent_run.deploy_followup_continue",
                has_subagents=bool(subagent_list),
                steps=len(steps),
            )
            _deploy_followup_input = _sanitize_input_messages(input_messages)
            _deploy_followup_input.append(
                HumanMessage(
                    content=_deploy_followup_message(
                        final_reply,
                        steps,
                        has_subagents=bool(subagent_list),
                    )
                )
            )
            try:
                async with asyncio.timeout(_timeout):
                    _deploy_graph_output = await graph.ainvoke(
                        {"messages": _deploy_followup_input},
                        config=_graph_config,
                        version="v2",
                    )
                    result = await _graph_result_from_output(
                        graph=graph,
                        graph_config=_graph_config,
                        graph_output=_deploy_graph_output,
                        log=log,
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                log.info(
                    "agent_run.deploy_followup_continue_ok",
                    has_url=_has_public_url_in_text(final_reply) or _has_public_url_in_steps(steps),
                )
            except Exception as _deploy_followup_exc:
                log.warning(
                    "agent_run.deploy_followup_continue_failed",
                    error=str(_deploy_followup_exc)[:300],
                )

        _needs_wa_file_followup, _wa_shared_file_path = _needs_whatsapp_file_delivery_followup(
            execution_user_message,
            tools_config,
            steps,
            final_reply,
        )
        if _needs_wa_file_followup and _wa_shared_file_path:
            log.info("agent_run.whatsapp_file_delivery_followup", path=_wa_shared_file_path)
            _wa_file_followup_directive = _whatsapp_file_delivery_followup_message(
                final_reply,
                steps,
                _wa_shared_file_path,
            )
            _wa_file_previous_db_messages = list(parsed["db_messages"])
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _wa_file_prompt = (
                    (system_prompt + "\n\n" + _wa_file_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _wa_file_graph = _cra(llm, tools=tools, prompt=_wa_file_prompt)
                _wa_file_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _wa_file_graph.ainvoke(
                        {"messages": _wa_file_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in _wa_file_previous_db_messages:
                    db.add(_msg_record)
                log.info(
                    "agent_run.whatsapp_file_delivery_followup_ok",
                    sent=_has_whatsapp_media_send_step(steps),
                )
            except Exception as _wa_file_followup_exc:
                log.warning(
                    "agent_run.whatsapp_file_delivery_followup_failed",
                    path=_wa_shared_file_path,
                    error=str(_wa_file_followup_exc)[:300],
                )
        for _msg_record in parsed["db_messages"]:
            db.add(_msg_record)

        _needs_forms_followup, _followup_form_id = _needs_google_forms_followup(execution_user_message, steps)
        if _needs_forms_followup and _followup_form_id:
            log.info("agent_run.forms_followup_continue", form_id=_followup_form_id)
            _forms_followup_directive = google_forms_followup_directive(_followup_form_id)
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _forms_prompt = (
                    (system_prompt + "\n\n" + _forms_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _forms_graph = _cra(llm, tools=tools, prompt=_forms_prompt)
                _forms_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _forms_graph.ainvoke(
                        {"messages": _forms_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _forms_followup_exc:
                _forms_followup_err = str(_forms_followup_exc)
                log.warning("agent_run.forms_followup_continue_failed", error=_forms_followup_err[:300], form_id=_followup_form_id)
                _is_missing_requests_err = (
                    "validation error for call[batch_update_form]" in _forms_followup_err.lower()
                    and "missing required argument" in _forms_followup_err.lower()
                    and "requests" in _forms_followup_err.lower()
                )
                if _is_missing_requests_err:
                    _forms_followup_retry_directive = google_forms_followup_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_prompt_retry = (
                            (system_prompt + "\n\n" + _forms_followup_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_graph_retry = _cra(llm, tools=tools, prompt=_forms_prompt_retry)
                        _forms_input_retry = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_graph_retry.ainvoke(
                                {"messages": _forms_input_retry}, config=_graph_config
                            )
                        parsed = parse_agent_result(
                            result=result,
                            input_messages=input_messages,
                            session_id=session.id,
                            run_id=run_id,
                            step_start=step_counter,
                            log=log,
                        )
                        final_reply = parsed["final_reply"]
                        steps = parsed["steps"]
                        total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                        for _msg_record in parsed["db_messages"]:
                            db.add(_msg_record)
                    except Exception as _forms_followup_retry_exc:
                        log.warning(
                            "agent_run.forms_followup_retry_failed",
                            error=str(_forms_followup_retry_exc)[:300],
                            form_id=_followup_form_id,
                        )

        _needs_slides_followup, _followup_presentation_id = _needs_google_slides_followup(execution_user_message, steps)
        if _needs_slides_followup and _followup_presentation_id:
            log.info("agent_run.slides_followup_continue", presentation_id=_followup_presentation_id)
            _slides_followup_directive = google_slides_followup_directive(
                _followup_presentation_id, execution_user_message
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _slides_prompt = (
                    (system_prompt + "\n\n" + _slides_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                _slides_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _slides_graph.ainvoke(
                        {"messages": _slides_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _slides_followup_exc:
                log.warning(
                    "agent_run.slides_followup_continue_failed",
                    error=str(_slides_followup_exc)[:300],
                    presentation_id=_followup_presentation_id,
                )

        _needs_sheets_followup, _followup_spreadsheet_id = _needs_google_sheets_followup(execution_user_message, steps)
        if _needs_sheets_followup and _followup_spreadsheet_id:
            log.info("agent_run.sheets_followup_continue", spreadsheet_id=_followup_spreadsheet_id)
            _sheets_followup_directive = google_sheets_followup_directive(
                _followup_spreadsheet_id, execution_user_message
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _sheets_prompt = (
                    (system_prompt + "\n\n" + _sheets_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _sheets_graph = _cra(llm, tools=tools, prompt=_sheets_prompt)
                _sheets_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _sheets_graph.ainvoke(
                        {"messages": _sheets_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _sheets_followup_exc:
                log.warning(
                    "agent_run.sheets_followup_continue_failed",
                    error=str(_sheets_followup_exc)[:300],
                    spreadsheet_id=_followup_spreadsheet_id,
                )

    await db.flush()

    # ------------------------------------------------------------------ #
    # 10. Long-term memory auto-extraction                                #
    # ------------------------------------------------------------------ #
    if _is_enabled(tools_config, "memory", default=True):
        user_msg_count = await count_user_messages(session.id, db)
        if user_msg_count > 0 and user_msg_count % settings.ltm_extraction_every == 0:
            log.info("agent_run.ltm_trigger", user_messages=user_msg_count)
            recent_for_ltm = await load_history(session.id, db, max_turns=settings.ltm_extraction_every)
            await extract_long_term_memory(
                agent_id=agent_id,
                recent_messages=recent_for_ltm,
                llm=llm,
                db=db,
                log=log,
                scope=_memory_scope,
            )

    await _cancel_wa_long_progress_notice()

    # cleanup
    if sandbox:
        await sandbox.aclose()
    for _ssb in sub_sandboxes:
        await _ssb.aclose()

    if not final_reply:
        _empty_llm = not parsed["has_output"]
        if _empty_llm:
            log.error(
                "agent_run.no_llm_output",
                session_id=str(session.id),
                run_id=str(run_id),
                user_message=execution_user_message[:100],
            )
        else:
            log.warning(
                "agent_run.missing_final_reply",
                steps=len(steps),
                run_id=str(run_id),
            )

    _google_mcp_auth_err_before_override = (
        (mcp_errors.get("google_workspace") if isinstance(mcp_errors, dict) else None)
        or _extract_google_mcp_step_error(steps)
    )
    final_reply, steps, _google_mcp_auth_url = await apply_google_mcp_reply_overrides(
        final_reply=final_reply,
        steps=steps,
        mcp_errors=mcp_errors,
        runtime=google_mcp,
        auth_url=_google_mcp_auth_url,
        llm_raw=llm_raw,
        user_message=execution_user_message,
        agent_id=agent_id,
        api_key=settings.api_key,
        log=log,
    )
    _google_mcp_auth_err = _google_mcp_auth_err_before_override or _extract_google_mcp_step_error(steps)
    _google_mcp_has_artifact = (
        _contains_google_workspace_artifact(final_reply)
        or _has_google_workspace_artifact_step(steps)
    )
    if (
        _google_mcp_auth_err
        and _is_google_auth_or_scope_error(str(_google_mcp_auth_err))
        and not _google_mcp_has_artifact
    ):
        final_reply = await _route_google_workspace_blocker_to_owner_if_customer(
            reply=final_reply,
            session=session,
            agent_model=agent_model,
            user_message=execution_user_message,
            error_text=str(_google_mcp_auth_err),
            auth_url=_google_mcp_auth_url,
            log=log,
        )
    guarded_reply = _task_result_guard_reply(final_reply, steps, execution_user_message)
    if guarded_reply != final_reply:
        log.warning("agent_run.final_reply_overridden_by_task_guard")
        final_reply = guarded_reply
    guarded_reply = _direct_whatsapp_send_guard_reply(final_reply, steps, execution_user_message, input_messages)
    if guarded_reply != final_reply:
        log.warning("agent_run.final_reply_overridden_by_direct_wa_send_guard")
        final_reply = guarded_reply
    guarded_reply = _operator_escalation_reply_guard(final_reply, steps, execution_user_message, escalation_user_jid)
    if guarded_reply != final_reply:
        log.warning("agent_run.final_reply_overridden_by_operator_escalation_guard")
        final_reply = guarded_reply

    _reply_before_non_empty_guard = final_reply
    final_reply = ensure_non_empty_reply(
        final_reply,
        steps,
        tools_config=tools_config,
        active_groups=active_groups,
    )
    if final_reply != _reply_before_non_empty_guard:
        log.warning(
            "agent_run.final_reply_overridden_by_non_empty_guard",
            before_len=len(_reply_before_non_empty_guard or ""),
            after_len=len(final_reply or ""),
            before_preview=(_reply_before_non_empty_guard or "")[:220],
            after_preview=(final_reply or "")[:220],
        )

    _reply_before_google_auth_guard = final_reply
    final_reply = await _append_builder_google_auth_link_if_needed(
        final_reply,
        steps=steps,
        session=session,
        settings_obj=settings,
        log=log,
    )
    if final_reply != _reply_before_google_auth_guard:
        log.warning(
            "agent_run.final_reply_appended_google_auth_link",
            before_len=len(_reply_before_google_auth_guard or ""),
            after_len=len(final_reply or ""),
            before_preview=(_reply_before_google_auth_guard or "")[:220],
            after_preview=(final_reply or "")[:220],
        )

    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        tokens_used=total_tokens_used,
        prompt_tokens=_agent_logger.prompt_tokens_from_callbacks,
        completion_tokens=_agent_logger.completion_tokens_from_callbacks,
        openrouter_cost_usd=round(_agent_logger.openrouter_cost_usd_from_callbacks, 8),
    )

    # Update Run → completed
    run_record.status = "completed"
    run_record.completed_at = datetime.now(timezone.utc)
    _apply_run_usage(total_tokens_used)
    await db.flush()

    return {
        "reply": final_reply,
        "steps": steps,
        "run_id": run_id,
        "tokens_used": total_tokens_used,
        "usage": _usage_summary(),
    }

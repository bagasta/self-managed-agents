"""WhatsApp direct-send guard helpers and detectors.

Extracted from agent_runner.py — pure functions, no DB/session dependencies.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import BaseMessage

from app.core.engine.agent_google_routing import _is_google_chat_intent
from app.core.engine.agent_step_utils import (
    _is_operator_envelope,
    _operator_message_payload,
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
    """Prioritize the normal send tool only for an explicit current send request.

    A generic confirmation belongs to the current conversation, not an inferred
    draft/phone pair from history. The model must call the guarded tool normally.
    """
    if _is_direct_whatsapp_meta_request(user_message):
        return False
    text = _operator_message_payload(user_message).strip().lower()
    if any(marker in text for marker in _WA_MEDIA_REQUEST_MARKERS):
        return False
    return _is_direct_whatsapp_send_request(user_message)


def _extract_direct_whatsapp_confirmation_payload(
    user_message: str,
    history_rows: list[Any] | None,
) -> tuple[str, str] | None:
    """Compatibility shim: history is never authorization for a direct send."""
    return None


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
    for step in steps or []:
        if (step or {}).get("tool") != "send_to_number":
            continue
        result = step.get("result", "")
        if str(result).startswith("[SENT_TO_NUMBER:") or isinstance(result, dict) and result.get("message_id"):
            return True
    return False


def _looks_like_direct_send_success_claim(final_reply: str) -> bool:
    text = (final_reply or "").lower()
    if not text:
        return False
    has_whatsapp_or_phone_context = bool(
        "whatsapp" in text
        or " wa " in f" {text} "
        or "nomor" in text
        or ("pesan" in text and "email" not in text and "gmail" not in text
            and re.search(r"\bkirim\b.*\bke\b", text))
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
    return any(getattr(msg, "type", "") == "tool"
               and str(getattr(msg, "content", "") or "").startswith("[SENT_TO_NUMBER:")
               for msg in messages or [])


def _has_reply_to_user_step(steps: list[dict[str, Any]]) -> bool:
    return any((step or {}).get("tool") == "reply_to_user"
               and str(step.get("result") or "").startswith("[SENT_TO_USER]")
               for step in steps or [])


def _has_prior_reply_to_user_evidence(messages: list[BaseMessage] | None) -> bool:
    return any(getattr(msg, "type", "") == "tool"
               and str(getattr(msg, "content", "") or "").startswith("[SENT_TO_USER]")
               for msg in messages or [])


def _direct_whatsapp_send_guard_reply(
    final_reply: str,
    steps: list[dict[str, Any]],
    user_message: str,
    history_messages: list[BaseMessage] | None = None,
) -> str:
    """Prevent false WhatsApp-send success claims when send_to_number did not run."""
    if not _looks_like_direct_send_success_claim(final_reply):
        return final_reply
    if _has_reply_to_user_step(steps):
        return final_reply
    if _has_send_to_number_step(steps):
        return final_reply
    if _is_direct_whatsapp_meta_request(user_message):
        return final_reply
    # A prior send only proves a delivery-status question such as "apakah tadi
    # sudah terkirim?". It must not let the model claim that a new recipient
    # was contacted when this run did not execute send_to_number.
    status_question = bool(re.search(r"\b(apakah|udah|sudah|status|tadi)\b", _operator_message_payload(user_message).lower()))
    if (_has_prior_send_to_number_evidence(history_messages) or _has_prior_reply_to_user_evidence(history_messages)) and status_question:
        return final_reply
    return (
        "Belum saya kirim. Pengiriman ke nomor tujuan belum terverifikasi. "
        "Periksa status sebelum mencoba mengirim ulang."
    )

"""Google Workspace MCP helpers used by agent_runner.

This module keeps Google-specific intent detection, auth-link generation, and
user-facing fallback replies out of the main orchestration flow.
"""
from __future__ import annotations

import copy
import json
import re
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool, tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field


class ModifySheetValuesArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    spreadsheet_id: str
    range_name: str | None = None
    range: str | None = None
    values: Any = None
    value_input_option: str = "USER_ENTERED"
    clear_values: bool = False


class CreateSpreadsheetArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    spreadsheet_title: str | None = None
    name: str | None = None
    file_name: str | None = None
    sheet_names: Any = None


class CreatePresentationArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    presentation_title: str | None = None
    name: str | None = None
    file_name: str | None = None


class BatchUpdatePresentationArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    presentation_id: str
    requests: list[dict[str, Any]] = Field(default_factory=list)


class CreateDriveFileArgs(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_name: str
    content: str | None = None
    folder_id: str = "root"
    mime_type: str = "text/plain"
    fileUrl: str | None = None
    file_url: str | None = None


class AppendSurveyResponseArgs(BaseModel):
    """Customer-safe payload; Google resource identifiers are never model input."""

    customer_name: str = ""
    satisfaction_score: int = Field(ge=1, le=10)
    expectations_met: str = ""
    liked: str = ""
    improvement: str = ""
    notes: str = ""
    escalation_status: str = "Tidak"


@dataclass
class GoogleMcpRuntime:
    enabled: bool
    workspace_server: dict[str, Any] | None
    connected_user_id: str | None
    auth_url: str | None
    preflight_error: str | None
    integration_url: str
    candidate_user_ids: list[str]
    system_prompt: Any


def _is_google_mcp_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    if _is_plain_google_form_link_reference(m):
        return False
    keywords = (
        "google sheet", "spreadsheet", "gmail", "calendar", "drive", "docs", "sheets",
        "slide", "slides", "presentasi", "presentation", "google slides", "forms",
        "google form", "form google", "formulir", "tasks", "contacts", "chat",
        "kalender", "google kalender", "email", "surel", "google docs", "dokumen google",
        "google dokumen", "google drive",
        "edit sheet", "update sheet", "buka sheet", "ubah sheet", "google workspace",
        "akun google", "sambungkan google", "connect google", "auth google",
        "otentikasi google", "login google",
    )
    return any(k in m for k in keywords)


def _is_plain_google_form_link_reference(message: str) -> bool:
    """A shared Google Form URL can be business info, not a request to run Google tools."""
    if not message:
        return False
    m = message.lower()
    has_form_link = bool(
        re.search(r"https?://(?:forms\.gle|docs\.google\.com/forms)/\S+", m)
    )
    has_plain_form_context = (
        "google form" in m
        and any(
            marker in m
            for marker in (
                "cara order",
                "order via",
                "order lewat",
                "isi google form",
                "pelanggan isi",
                "customer isi",
                "link yang pelanggan",
                "link order",
                "sumber data order",
                "form yang udah aku buat",
                "form yang sudah aku buat",
            )
        )
    )
    if not has_form_link and not has_plain_form_context:
        return False

    action_markers = (
        "buatkan",
        "bikinin",
        "tolong buat",
        "minta buat",
        "bikin google form",
        "buat google form",
        "create",
        "generate",
        "edit",
        "ubah",
        "update",
        "hapus",
        "delete",
        "isi form",
        "submit",
        "jawab form",
        "ambil response",
        "lihat response",
        "cek response",
        "pantau response",
        "sinkron",
        "integrasi",
        "connect",
        "login",
        "auth",
    )
    return not any(marker in m for marker in action_markers)


def is_google_workspace_mcp_configured(tools_config: dict[str, Any]) -> bool:
    """Return True when the Google Workspace MCP server is configured.

    This is intentionally cheaper than opening MCP connections; it is used
    before prompt construction to decide whether Google Workspace requests
    should run in parent-only mode instead of exposing subagent delegation.
    """
    mcp_cfg = tools_config.get("mcp", {}) if isinstance(tools_config, dict) else {}
    if not isinstance(mcp_cfg, dict) or not mcp_cfg:
        return False

    has_wrapper = "enabled" in mcp_cfg or "servers" in mcp_cfg
    if has_wrapper:
        enabled = bool(mcp_cfg.get("enabled", bool(mcp_cfg.get("servers"))))
        servers = mcp_cfg.get("servers", {})
        if not enabled:
            return False
        if isinstance(servers, dict) and "google_workspace" in servers:
            return True
        return bool(os.environ.get("WORKSPACE_MCP_URL"))

    workspace_server = mcp_cfg.get("google_workspace")
    if isinstance(workspace_server, dict):
        return "url" in workspace_server or "command" in workspace_server
    return bool(os.environ.get("WORKSPACE_MCP_URL"))


def _is_google_auth_or_scope_error(error_text: str) -> bool:
    if not error_text:
        return False
    e = error_text.lower()
    markers = (
        "401 unauthorized",
        "invalid_token",
        "token expired",
        "token sudah expired",
        "belum terhubung",
        "belum dikonfigurasi",
        "oauth credentials lack required scopes",
        "required scopes",
        "insufficient scope",
        "insufficient authentication scopes",
        "request had insufficient authentication scopes",
        "permission_denied",
        "insufficientpermissions",
        "access_denied",
        "googleapis.com/auth/",
    )
    return any(m in e for m in markers)


_GOOGLE_MCP_TOOL_NAME_MARKERS = (
    "gmail",
    "calendar",
    "event",
    "freebusy",
    "drive",
    "doc",
    "spreadsheet",
    "sheet",
    "chat",
    "message",
    "form",
    "presentation",
    "slide",
    "contact",
    "script",
)


def _is_google_mcp_tool_name(tool_name: str) -> bool:
    """Return True for Google Workspace MCP tool names.

    Keep this intentionally broad, but do not include generic names like
    ``task`` because Deep Agents uses that for subagent delegation.

    Markers are matched as prefixes of snake_case tokens, not raw substrings:
    ``get_user_subscription`` must not match ``script`` (Apps Script), while
    ``list_calendar_events`` still matches ``calendar``/``event``.
    """
    name = (tool_name or "").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
    return any(
        token.startswith(marker)
        for token in tokens
        for marker in _GOOGLE_MCP_TOOL_NAME_MARKERS
    )


def _google_integration_runtime_url(public_or_configured_url: str) -> str:
    """Prefer local integration API for backend calls in local-dev mode."""
    configured = str(public_or_configured_url or "").rstrip("/")
    try:
        from app.config import get_settings

        settings = get_settings()
        prefer_local = str(getattr(settings, "workspace_mcp_prefer_local", "")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    except Exception:
        prefer_local = False
    if prefer_local and "devtunnels.ms" in configured:
        return "http://localhost:8003"
    return configured


def _extract_google_mcp_step_error(steps: list[dict[str, Any]]) -> str | None:
    for step in steps:
        tool_name = str((step or {}).get("tool", "")).lower()
        result = str((step or {}).get("result", "") or "")
        if not tool_name or not result:
            continue
        if not _is_google_mcp_tool_name(tool_name):
            continue
        if _is_google_auth_or_scope_error(result):
            return result
    return None


def _is_google_resource_not_found_error(error_text: str | None) -> bool:
    """Distinguish a missing Google resource from an OAuth failure."""
    text = str(error_text or "").casefold()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "requested entity was not found",
            "resource not found",
            "spreadsheet not found",
            "file not found",
            '"code": 404',
            '"status_code": 404',
            "status code 404",
            "http 404",
        )
    )


def _extract_google_mcp_resource_error(steps: list[dict[str, Any]]) -> str | None:
    for step in steps:
        tool_name = str((step or {}).get("tool", "")).lower()
        result = str((step or {}).get("result", "") or "")
        if (
            tool_name
            and result
            and _is_google_mcp_tool_name(tool_name)
            and _is_google_resource_not_found_error(result)
        ):
            return result
    return None


def customer_survey_google_resource(
    session: Any,
    agent_model: Any,
) -> dict[str, Any] | None:
    """Return a verified append-only survey resource for customer sessions."""
    from app.core.engine.agent_identity import _is_customer_whatsapp_session

    if not _is_customer_whatsapp_session(session, agent_model):
        return None
    tools_config = getattr(agent_model, "tools_config", None)
    tools_config = tools_config if isinstance(tools_config, dict) else {}
    resource = tools_config.get("google_workspace_resources")
    if not isinstance(resource, dict):
        return None
    spreadsheet_id = str(resource.get("survey_spreadsheet_id") or "").strip()
    tab_name = str(resource.get("survey_sheet_name") or "").strip()
    headers = resource.get("survey_headers")
    if (
        resource.get("verified") is not True
        or resource.get("customer_append_enabled") is not True
        or not spreadsheet_id
        or not re.fullmatch(r"[\w .-]{1,100}", tab_name)
        or not isinstance(headers, list)
        or len(headers) != 9
    ):
        return None
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": tab_name,
        "headers": [str(value) for value in headers],
    }


def build_customer_survey_append_tools(
    mcp_tools: list[Any],
    *,
    resource: dict[str, Any],
    customer_phone: str,
    log: Any,
) -> list[Any]:
    """Expose one append-only Sheet tool instead of the Owner's Google tools."""
    read_tool = next(
        (tool for tool in mcp_tools if getattr(tool, "name", "") == "read_sheet_values"),
        None,
    )
    modify_tool = next(
        (tool for tool in mcp_tools if getattr(tool, "name", "") == "modify_sheet_values"),
        None,
    )
    spreadsheet_id = str(resource.get("spreadsheet_id") or "").strip()
    sheet_name = str(resource.get("sheet_name") or "").strip()
    if not read_tool or not modify_tool or not spreadsheet_id or not sheet_name:
        return []

    async def _append_sheet_survey_response(
        customer_name: str = "",
        satisfaction_score: int = 1,
        expectations_met: str = "",
        liked: str = "",
        improvement: str = "",
        notes: str = "",
        escalation_status: str = "Tidak",
    ) -> str:
        # Serialize read-next-row-write across all API replicas. The model never
        # receives or supplies a spreadsheet ID/range, so prompt injection
        # cannot redirect this delegated write to another Owner resource.
        from sqlalchemy import text

        from app.database import AsyncSessionLocal

        lock_key = f"survey-sheet:{spreadsheet_id}:{sheet_name}"
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": lock_key},
            )
            current = await read_tool.ainvoke(
                {
                    "spreadsheet_id": spreadsheet_id,
                    "range_name": f"{sheet_name}!A1:I10000",
                }
            )
            current_text = str(current)
            if (
                _is_google_auth_or_scope_error(current_text)
                or _is_google_resource_not_found_error(current_text)
            ):
                raise RuntimeError(current_text)
            match = re.search(r"Successfully read\s+(\d+)\s+rows?", current_text, re.I)
            if not match:
                raise RuntimeError(
                    "SURVEY_SHEET_READ_FAILED: jumlah baris Sheet tidak dapat diverifikasi"
                )
            next_row = max(2, int(match.group(1)) + 1)
            row_values = [
                datetime.now(timezone.utc).isoformat(),
                customer_phone,
                customer_name.strip(),
                str(satisfaction_score),
                expectations_met.strip(),
                liked.strip(),
                improvement.strip(),
                notes.strip(),
                escalation_status.strip() or "Tidak",
            ]
            target_range = f"{sheet_name}!A{next_row}:I{next_row}"
            written = await modify_tool.ainvoke(
                {
                    "spreadsheet_id": spreadsheet_id,
                    "range_name": target_range,
                    "values": [row_values],
                    "value_input_option": "USER_ENTERED",
                }
            )
            written_text = str(written)
            if (
                _is_google_auth_or_scope_error(written_text)
                or _is_google_resource_not_found_error(written_text)
                or "error" in written_text.casefold()
            ):
                raise RuntimeError(written_text)
            verified = await read_tool.ainvoke(
                {
                    "spreadsheet_id": spreadsheet_id,
                    "range_name": target_range,
                }
            )
            verified_text = str(verified)
            if customer_phone not in verified_text:
                raise RuntimeError(
                    "SURVEY_SHEET_VERIFY_FAILED: hasil tulis tidak ditemukan saat readback"
                )
            await db.commit()
        log.info(
            "agent_run.customer_survey_response_appended",
            sheet_name=sheet_name,
            row=next_row,
        )
        return "SURVEY_RESPONSE_SAVED: Jawaban survey berhasil disimpan dan diverifikasi."

    return [
        StructuredTool.from_function(
            coroutine=_append_sheet_survey_response,
            name="append_sheet_survey_response",
            description=(
                "Simpan satu hasil survey customer ke Sheet bisnis yang sudah ditetapkan Owner. "
                "Panggil tepat sekali setelah jawaban survey lengkap. Tool ini append-only dan "
                "tidak dapat membaca atau menulis resource Google lain."
            ),
            args_schema=AppendSurveyResponseArgs,
        )
    ]


def _looks_like_progress_claim(reply_text: str) -> bool:
    if not reply_text:
        return False
    t = reply_text.lower()
    markers = (
        "lagi proses",
        "sedang proses",
        "on progress",
        "sebentar lagi",
        "akan saya kirim",
        "akan gue kirim",
        "begitu selesai",
        "processing",
        "working on",
    )
    return any(m in t for m in markers)


def _looks_like_google_mcp_success_claim(reply_text: str) -> bool:
    if not reply_text:
        return False
    t = reply_text.lower()
    service_markers = (
        "google slide",
        "slides",
        "presentasi",
        "google form",
        "form",
        "google sheet",
        "spreadsheet",
        "google doc",
        "docs",
        "drive",
        "gmail",
        "calendar",
        "kalender",
    )
    success_markers = (
        "sudah saya buat",
        "sudah dibuat",
        "berhasil dibuat",
        "sudah saya siapkan",
        "sudah siap",
        "siap kamu akses",
        "link",
        "url",
    )
    return any(s in t for s in service_markers) and any(s in t for s in success_markers)


def _looks_like_google_auth_recovery_reply(reply_text: str) -> bool:
    if not reply_text:
        return False
    t = reply_text.lower()
    google_markers = ("google", "gmail", "mcp")
    auth_markers = (
        "belum terhubung",
        "tidak terhubung",
        "login",
        "otentikasi",
        "autentikasi",
        "auth",
        "izin akses",
        "berikan izin",
        "reconnect",
        "connect",
        "sambungkan",
        "link otentikasi",
        "link autentikasi",
    )
    return any(marker in t for marker in google_markers) and any(
        marker in t for marker in auth_markers
    )


def _looks_like_google_auth_confirmation(message: str) -> bool:
    """Return True when the user likely confirms completing Google OAuth."""
    if not message:
        return False
    t = re.sub(r"\s+", " ", message.strip().lower())
    if not t:
        return False
    exact_markers = {
        "sudah",
        "udah",
        "done",
        "selesai",
        "ok",
        "oke",
        "ok sudah",
        "oke sudah",
        "sudah selesai",
        "udah selesai",
        "sudah login",
        "udah login",
        "sudah reconnect",
        "udah reconnect",
        "sudah connect",
        "udah connect",
        "sudah saya connect",
        "sudah saya reconnect",
        "sudah dihubungkan",
        "sudah tersambung",
        "connected",
        "reconnected",
    }
    if t in exact_markers:
        return True
    confirmation_markers = (
        "sudah saya klik",
        "udah saya klik",
        "sudah authorize",
        "sudah otorisasi",
        "sudah autentikasi",
        "sudah otentikasi",
        "sudah kasih izin",
        "sudah beri izin",
        "sudah login google",
        "google sudah connect",
        "google sudah tersambung",
    )
    return any(marker in t for marker in confirmation_markers)


def is_google_auth_recovery_followup(message: str, history_rows: list[Any], *, max_messages: int = 8) -> bool:
    """Detect a short OAuth completion follow-up after a Google auth blocker.

    The current user message often only says "sudah", so keyword intent detection
    cannot see the original Google Workspace request. We intentionally tie this
    to recent assistant history to avoid treating unrelated "sudah" replies as
    Google MCP intent.
    """
    if not _looks_like_google_auth_confirmation(message):
        return False
    recent_rows = list(history_rows or [])[-max_messages:]
    for row in reversed(recent_rows):
        role = getattr(row, "role", None)
        content = getattr(row, "content", None)
        if role in {"assistant", "agent"} and _looks_like_google_auth_recovery_reply(str(content or "")):
            return True
    return False


def find_last_google_workspace_user_request(history_rows: list[Any]) -> str | None:
    """Return the most recent user request that needs Google Workspace MCP."""
    for row in reversed(list(history_rows or [])):
        if getattr(row, "role", None) != "user":
            continue
        content = str(getattr(row, "content", None) or "").strip()
        if not content:
            continue
        if _looks_like_google_auth_confirmation(content):
            continue
        if _is_google_mcp_intent(content):
            return content
    return None


def _ensure_google_auth_link_in_reply(reply_text: str, auth_url: str | None) -> str:
    if not auth_url:
        return reply_text
    if auth_url in (reply_text or ""):
        return reply_text
    return f"{reply_text.rstrip()}\n\nLink otentikasi Google:\n{auth_url}"


_URL_FRAGMENT_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


def _sanitize_user_facing_google_terms(reply_text: str) -> str:
    """Avoid leaking internal integration protocol terms in user-facing replies."""
    if not reply_text:
        return reply_text
    replacements = (
        (r"\bMCP\s+Google\s+Workspace\b", "Google Workspace"),
        (r"\bGoogle\s+Workspace\s+MCP\b", "Google Workspace"),
        (r"\bGoogle\s+MCP\b", "Google Workspace"),
        (r"\bMCP\s+Google\b", "Google Workspace"),
        (r"\bMCP\s+tools?\b", "integrasi Google"),
        (r"\btools?\s+MCP\b", "integrasi Google"),
        (r"\bmelalui\s+MCP\b", "melalui integrasi Google"),
        (r"\blewat\s+MCP\b", "lewat integrasi Google"),
        (r"\bvia\s+MCP\b", "via integrasi Google"),
        (r"\bMCP\b", "integrasi Google"),
    )
    parts = _URL_FRAGMENT_RE.split(reply_text)
    urls = _URL_FRAGMENT_RE.findall(reply_text)
    sanitized_parts: list[str] = []
    for part in parts:
        sanitized = part
        for pattern, replacement in replacements:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
        sanitized_parts.append(sanitized)

    merged: list[str] = []
    for idx, part in enumerate(sanitized_parts):
        merged.append(part)
        if idx < len(urls):
            merged.append(urls[idx])
    return "".join(merged)


def _build_google_mcp_not_executed_reply(user_message: str) -> str:
    lower = (user_message or "").lower()
    if "link" in lower or "url" in lower:
        return (
            "Belum ada link Google Workspace yang valid untuk saya kirim. "
            "Run sebelumnya tidak menunjukkan integrasi Google benar-benar terpanggil, jadi saya tidak mau mengarang link. "
            "Tolong minta saya jalankan ulang pembuatan file-nya, nanti saya akan pakai integrasi Google langsung."
        )
    return (
        "Belum berhasil saya eksekusi lewat Google Workspace. "
        "Run ini tidak memanggil tool Google apa pun, jadi saya tidak akan mengklaim file sudah dibuat. "
        "Silakan coba ulang, dan saya akan menjalankan integrasi Google langsung."
    )


_GOOGLE_WORKSPACE_ARTIFACT_RE = re.compile(
    r"https://docs\.google\.com/(?:presentation|spreadsheets|document|forms)/[^\s\"']+",
    re.IGNORECASE,
)


def _contains_google_workspace_artifact(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return bool(_GOOGLE_WORKSPACE_ARTIFACT_RE.search(text)) or any(
        marker in lowered
        for marker in (
            "created and populated slide deck",
            "created and populated google doc",
            "successfully created and populated survey form",
            "presentation id:",
            "spreadsheet id:",
            "document id:",
            "form id:",
        )
    )


def _extract_requested_slide_count(message: str) -> int | None:
    if not message:
        return None
    m = re.search(
        r"\b(\d{1,2})\s*(?:slide|slides|halaman|page|pages|lembar)\b",
        message.lower(),
    )
    if not m:
        return None
    try:
        n = int(m.group(1))
    except Exception:
        return None
    if 1 <= n <= 12:
        return n
    return None


def _extract_presentation_total_slides(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"Total Slides:\s*(\d{1,3})", text, re.IGNORECASE)
    if not m:
        m = re.search(r"Slides:\s*(\d{1,3})\s*slide", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _is_google_slides_relayout_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    slides_markers = (
        "slide",
        "slides",
        "presentasi",
        "presentation",
    )
    relayout_markers = (
        "rapih",
        "rapikan",
        "rapihkan",
        "jadikan",
        "layout",
        "restructure",
        "susun ulang",
        "bikin",
        "buat",
    )
    return any(k in m for k in slides_markers) and any(k in m for k in relayout_markers)


def _is_google_forms_authoring_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    forms_markers = (
        "google form",
        "google forms",
        "form",
        "survei",
        "survey",
        "kuesioner",
        "kuisioner",
        "questionnaire",
    )
    authoring_markers = (
        "bikin",
        "buat",
        "isi",
        "pertanyaan",
        "question",
        "kirim link",
        "link",
        "mcp",
    )
    return any(k in m for k in forms_markers) and any(k in m for k in authoring_markers)


def _is_google_sheets_authoring_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    sheet_markers = (
        "google sheet",
        "google sheets",
        "spreadsheet",
        "sheet",
        "sheets",
        "excel",
        "xlsx",
        "tabel",
        "table",
        "rumus",
        "formula",
    )
    authoring_markers = (
        "bikin",
        "buat",
        "generate",
        "isi",
        "edit",
        "ubah",
        "update",
        "tambah",
        "masukkan",
        "laporan",
        "rekap",
        "budget",
        "anggaran",
        "jadwal",
        "tracker",
        "invoice",
        "rumus",
        "formula",
        "tabel",
        "table",
    )
    if not any(k in m for k in sheet_markers):
        return False
    if _is_blank_spreadsheet_only_intent(m):
        return False
    return any(k in m for k in authoring_markers)


def _is_blank_spreadsheet_only_intent(message_lower: str) -> bool:
    blank_markers = (
        "kosong",
        "blank",
        "empty",
        "file aja",
        "file saja",
        "spreadsheet aja",
        "spreadsheet saja",
        "sheet aja",
        "sheet saja",
        "tanpa isi",
        "tanpa tabel",
    )
    return any(k in message_lower for k in blank_markers)


def _extract_form_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"Form ID:\s*([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"/forms/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    return None


def _extract_spreadsheet_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\bID:\s*([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bspreadsheet\s+([A-Za-z0-9_-]{12,})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_presentation_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\bPresentation ID:\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/presentation/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bpresentation\s+([A-Za-z0-9_-]{12,})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _fallback_unqualified_sheet_range(range_name: str) -> str | None:
    if not range_name or "!" not in range_name:
        return None

    sheet_name, cell_range = range_name.split("!", 1)
    sheet_name = sheet_name.strip().strip("'").strip('"').lower().replace(" ", "")
    cell_range = cell_range.strip()
    if not cell_range:
        return None
    if sheet_name in {"sheet1", "lembar1"}:
        return cell_range
    return None


def _split_simple_sheet_range(range_name: str) -> tuple[str, str] | None:
    if not range_name or "!" not in range_name:
        return None
    sheet_name, cell_range = range_name.split("!", 1)
    sheet_name = sheet_name.strip().strip("'").strip('"')
    cell_range = cell_range.strip()
    if not sheet_name or not cell_range:
        return None
    return sheet_name, cell_range


def _normalize_sheet_values_for_mcp(values: Any) -> Any:
    if values is None:
        return None
    if isinstance(values, str):
        return values
    if isinstance(values, dict):
        rows = [["Field", "Value"]]
        rows.extend([[key, value] for key, value in values.items()])
        return json.dumps(rows, ensure_ascii=False)
    if isinstance(values, list):
        if not values:
            return json.dumps([], ensure_ascii=False)
        if all(isinstance(item, dict) for item in values):
            headers: list[str] = []
            seen: set[str] = set()
            for row in values:
                for key in row:
                    key_text = str(key)
                    if key_text not in seen:
                        seen.add(key_text)
                        headers.append(key_text)
            rows = [headers]
            rows.extend([[row.get(header, "") for header in headers] for row in values])
            return json.dumps(rows, ensure_ascii=False)
        if all(not isinstance(item, list) for item in values):
            return json.dumps([values], ensure_ascii=False)
        return json.dumps(values, ensure_ascii=False)
    return json.dumps([[values]], ensure_ascii=False)


def _normalize_string_list_arg(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [part.strip() for part in stripped.split(",") if part.strip()]
    return value


_CALENDAR_EVENT_ID_RE = re.compile(r"\b(?:Event ID|ID):\s*([A-Za-z0-9_-]+)\b")


def _is_missing_calendar_event_id(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"none", "null", "undefined", "no id"}


def _looks_like_calendar_id_not_event_id(value: Any) -> bool:
    if _is_missing_calendar_event_id(value):
        return False
    text = str(value).strip().lower()
    return text == "primary" or "@" in text


def _extract_calendar_event_ids(text: str) -> list[str]:
    if not text:
        return []

    ids: list[str] = []
    seen: set[str] = set()
    for match in _CALENDAR_EVENT_ID_RE.finditer(text):
        event_id = match.group(1).strip()
        if not event_id or event_id.lower() == "no":
            continue
        if event_id not in seen:
            seen.add(event_id)
            ids.append(event_id)
    return ids


async def _lookup_calendar_event_ids(
    *,
    get_events_tool: Any,
    calendar_id: str,
    summary: str | None,
    start_time: str | None,
    end_time: str | None,
    log: Any,
) -> tuple[list[str], str]:
    lookup_kwargs: dict[str, Any] = {
        "calendar_id": calendar_id or "primary",
        "max_results": 10,
        "detailed": True,
    }
    if start_time:
        lookup_kwargs["time_min"] = start_time
    if end_time:
        lookup_kwargs["time_max"] = end_time
    if summary:
        lookup_kwargs["query"] = summary

    lookup_result = await get_events_tool.ainvoke(lookup_kwargs)
    lookup_text = str(lookup_result or "")
    ids = _extract_calendar_event_ids(lookup_text)
    if ids or not summary:
        return ids, lookup_text

    retry_kwargs = dict(lookup_kwargs)
    retry_kwargs.pop("query", None)
    log.warning(
        "agent_run.calendar_event_lookup_retry_without_query",
        calendar_id=calendar_id,
        start_time=start_time,
        end_time=end_time,
        summary=summary,
    )
    retry_result = await get_events_tool.ainvoke(retry_kwargs)
    retry_text = str(retry_result or "")
    return _extract_calendar_event_ids(retry_text), retry_text


_SLIDES_ELEMENT_PROPERTY_REQUESTS = {
    "createShape",
    "createImage",
    "createVideo",
    "createLine",
    "createTable",
    "createSheetsChart",
}

_SLIDES_VALID_SHAPE_TYPES = {
    "TEXT_BOX",
    "RECTANGLE",
    "ROUND_RECTANGLE",
    "ELLIPSE",
    "ARC",
    "BENT_ARROW",
    "BENT_UP_ARROW",
    "BEVEL",
    "BLOCK_ARC",
    "BRACE_PAIR",
    "BRACKET_PAIR",
    "CAN",
    "CHART",
    "CHEVRON",
    "CLOUD",
    "CORNER",
    "CUBE",
    "CURVED_DOWN_ARROW",
    "CURVED_LEFT_ARROW",
    "CURVED_RIGHT_ARROW",
    "CURVED_UP_ARROW",
    "DECAGON",
    "DIAMOND",
    "DOWN_ARROW",
    "ELLIPSE",
    "FOLDED_CORNER",
    "FRAME",
    "HEART",
    "HEXAGON",
    "HOME_PLATE",
    "HORIZONTAL_SCROLL",
    "LEFT_ARROW",
    "LEFT_BRACE",
    "LEFT_BRACKET",
    "LEFT_RIGHT_ARROW",
    "LEFT_CIRCULAR_ARROW",
    "LEFT_RIGHT_UP_ARROW",
    "LEFT_UP_ARROW",
    "LIGHTNING_BOLT",
    "LINE",
    "MOON",
    "NO_SMOKING",
    "NOTCHED_RIGHT_ARROW",
    "OCTAGON",
    "PARALLELOGRAM",
    "PENTAGON",
    "PIE",
    "PLAQUE",
    "PLUS",
    "QUAD_ARROW",
    "QUAD_ARROW_CALLOUT",
    "RIBBON",
    "RIBBON_2",
    "RIGHT_ARROW",
    "RIGHT_BRACE",
    "RIGHT_BRACKET",
    "ROUND_1_RECTANGLE",
    "ROUND_2_DIAGONAL_RECTANGLE",
    "ROUND_2_SAME_RECTANGLE",
    "RT_TRIANGLE",
    "SMILEY_FACE",
    "SNIP_1_RECTANGLE",
    "SNIP_2_DIAGONAL_RECTANGLE",
    "SNIP_2_SAME_RECTANGLE",
    "SNIP_ROUND_RECTANGLE",
    "STAR_10",
    "STAR_12",
    "STAR_16",
    "STAR_24",
    "STAR_32",
    "STAR_4",
    "STAR_5",
    "STAR_6",
    "STAR_7",
    "STAR_8",
    "STRIPED_RIGHT_ARROW",
    "SUN",
    "TRAPEZOID",
    "TRIANGLE",
    "UP_ARROW",
    "UP_DOWN_ARROW",
    "UTURN_ARROW",
    "WAVE",
    "WEDGE_ELLIPSE_CALLOUT",
    "WEDGE_RECTANGLE_CALLOUT",
    "WEDGE_ROUND_RECTANGLE_CALLOUT",
}


_SLIDES_REQUEST_ALIASES = {
    "create_slide": "createSlide",
    "createSlide": "createSlide",
    "create_shape": "createShape",
    "createShape": "createShape",
    "insert_text": "insertText",
    "insertText": "insertText",
    "delete_object": "deleteObject",
    "deleteObject": "deleteObject",
    "update_text_style": "updateTextStyle",
    "updateTextStyle": "updateTextStyle",
    "update_paragraph_style": "updateParagraphStyle",
    "updateParagraphStyle": "updateParagraphStyle",
    "create_image": "createImage",
    "createImage": "createImage",
    "replace_all_text": "replaceAllText",
    "replaceAllText": "replaceAllText",
}

_SLIDES_CREATE_OBJECT_REQUEST_TYPES = (
    "createSlide",
    "createShape",
    "createImage",
    "createLine",
    "createVideo",
    "createSheetsChart",
)


def _camelize_slides_payload_keys(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_camelize_slides_payload_keys(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    key_map = {
        "object_id": "objectId",
        "page_object_id": "pageObjectId",
        "shape_type": "shapeType",
        "insertion_index": "insertionIndex",
        "text_style": "style",
        "cell_location": "cellLocation",
        "element_properties": "elementProperties",
        "page_element_id": "pageElementId",
        "replace_text": "replaceText",
        "contains_text": "containsText",
        "match_case": "matchCase",
    }
    return {
        key_map.get(str(key), key): _camelize_slides_payload_keys(value)
        for key, value in payload.items()
    }


def _normalize_slides_request_aliases(requests: Any) -> Any:
    if not isinstance(requests, list):
        return requests

    normalized: list[Any] = []
    for request in requests:
        if not isinstance(request, dict):
            normalized.append(request)
            continue

        if len(request) == 1:
            raw_type, raw_payload = next(iter(request.items()))
            request_type = _SLIDES_REQUEST_ALIASES.get(str(raw_type), raw_type)
            payload = _camelize_slides_payload_keys(raw_payload)
        else:
            matched_type = next((key for key in request if key in _SLIDES_REQUEST_ALIASES), None)
            if matched_type is None:
                normalized.append(_camelize_slides_payload_keys(request))
                continue
            request_type = _SLIDES_REQUEST_ALIASES[str(matched_type)]
            payload = _camelize_slides_payload_keys(request.get(matched_type) or {})

        if request_type == "createShape" and isinstance(payload, dict):
            page_object_id = payload.pop("pageObjectId", None)
            element_properties = payload.setdefault("elementProperties", {})
            if page_object_id and isinstance(element_properties, dict):
                element_properties.setdefault("pageObjectId", page_object_id)
        normalized.append({request_type: payload})

    return normalized


def _normalize_slides_batch_requests(requests: Any) -> Any:
    if not isinstance(requests, list):
        return requests

    normalized = copy.deepcopy(_normalize_slides_request_aliases(requests))
    _uniquify_slides_created_object_ids(normalized)
    for request in normalized:
        _normalize_slides_request(request)

    return normalized


def _safe_slides_object_id(raw_object_id: str, suffix: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_:-]", "_", str(raw_object_id or "").strip())
    if not base or not re.match(r"^[A-Za-z0-9_]", base):
        base = f"obj_{base}"
    # Keep comfortably below Slides' object ID length limit while preserving
    # enough of the model-provided name for readable tool logs.
    base = base[:36].rstrip("_:-") or "obj"
    return f"{base}_{suffix}"


def _replace_slides_object_id_refs(value: Any, id_map: dict[str, str]) -> None:
    if not id_map:
        return
    if isinstance(value, dict):
        for key, nested in list(value.items()):
            if isinstance(nested, str) and nested in id_map:
                value[key] = id_map[nested]
            else:
                _replace_slides_object_id_refs(nested, id_map)
    elif isinstance(value, list):
        for nested in value:
            _replace_slides_object_id_refs(nested, id_map)


def _uniquify_slides_created_object_ids(requests: list[Any]) -> None:
    """Make newly-created Slides object IDs unique and rewrite in-batch refs.

    Google Slides rejects reused object IDs across the whole presentation. LLMs
    often retry with stable IDs like ``slide2``; replacing IDs for objects this
    batch creates avoids collisions while preserving internal references such as
    createShape.elementProperties.pageObjectId and insertText.objectId.
    """
    if not isinstance(requests, list):
        return

    id_map: dict[str, str] = {}
    suffix = uuid.uuid4().hex[:8]
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            continue
        _replace_slides_object_id_refs(request, id_map)
        for request_type in _SLIDES_CREATE_OBJECT_REQUEST_TYPES:
            payload = request.get(request_type)
            if not isinstance(payload, dict):
                continue
            object_id = payload.get("objectId")
            if not isinstance(object_id, str) or not object_id.strip():
                continue
            new_object_id = _safe_slides_object_id(object_id, f"{suffix}_{index}")
            payload["objectId"] = new_object_id
            id_map[object_id] = new_object_id
            break

    for request in requests:
        _replace_slides_object_id_refs(request, id_map)


def _normalize_slides_request(request: Any) -> None:
    if not isinstance(request, dict):
        return

    _normalize_slides_structure(request)

    for request_type in _SLIDES_ELEMENT_PROPERTY_REQUESTS:
        payload = request.get(request_type)
        if not isinstance(payload, dict):
            continue
        element_properties = payload.get("elementProperties")
        if isinstance(element_properties, dict):
            _normalize_slides_element_properties(element_properties)

    payload = request.get("updatePageElementTransform")
    if isinstance(payload, dict):
        transform = payload.get("transform")
        if isinstance(transform, dict):
            _ensure_slides_transform_unit(transform)


def _normalize_slides_structure(value: Any) -> None:
    if isinstance(value, dict):
        _ensure_slides_dimension_unit(value)
        _ensure_slides_transform_unit(value)
        _normalize_slides_shape_type(value)
        for nested_value in value.values():
            _normalize_slides_structure(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _normalize_slides_structure(nested_value)


def _normalize_slides_element_properties(element_properties: dict[str, Any]) -> None:
    size = element_properties.get("size")
    if isinstance(size, dict):
        for key in ("width", "height"):
            _ensure_slides_dimension_unit(size.get(key))

    transform = element_properties.get("transform")
    if isinstance(transform, dict):
        _ensure_slides_transform_unit(transform)


def _ensure_slides_dimension_unit(dimension: Any) -> None:
    if not isinstance(dimension, dict):
        return
    if "magnitude" not in dimension:
        return
    if not dimension.get("unit") or str(dimension.get("unit")).upper() == "UNIT_UNSPECIFIED":
        dimension["unit"] = "PT"


def _ensure_slides_transform_unit(transform: dict[str, Any]) -> None:
    transform_keys = {"scaleX", "scaleY", "shearX", "shearY", "translateX", "translateY"}
    if not any(key in transform for key in transform_keys):
        return
    if not transform.get("unit") or str(transform.get("unit")).upper() == "UNIT_UNSPECIFIED":
        transform["unit"] = "PT"


def _normalize_slides_shape_type(payload: dict[str, Any]) -> None:
    for key in ("shape_type", "shapeType"):
        if key not in payload:
            continue
        shape_type = payload.get(key)
        if not isinstance(shape_type, str):
            continue
        normalized = shape_type.strip().upper()
        if normalized in _SLIDES_VALID_SHAPE_TYPES:
            payload[key] = normalized
            return
        if any(marker in normalized for marker in ("TITLE", "BODY", "SUBTITLE", "PLACEHOLDER", "TEXT")):
            payload[key] = "TEXT_BOX"
            return
        payload[key] = normalized
        return


def _slides_batch_args_have_text_write(args: Any) -> bool:
    if not isinstance(args, dict):
        return False

    requests = args.get("requests")
    if not isinstance(requests, list):
        return False

    for request in requests:
        if not isinstance(request, dict):
            continue
        insert_text = request.get("insertText")
        if isinstance(insert_text, dict) and str(insert_text.get("text") or "").strip():
            return True

        replace_text = request.get("replaceAllText")
        if isinstance(replace_text, dict) and str(replace_text.get("replaceText") or "").strip():
            return True

    return False


def _presentation_result_has_non_empty_text(result: str) -> bool:
    if not result:
        return False
    lowered = result.lower()
    if "text:" not in lowered:
        return False
    non_empty_lines = [
        line
        for line in lowered.splitlines()
        if "text:" in line and "text: empty" not in line
    ]
    return bool(non_empty_lines) or "\n    > " in result


def _needs_google_forms_followup(user_message: str, steps: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not _is_google_forms_authoring_intent(user_message):
        return False, None
    saw_create = False
    saw_batch = False
    saw_get = False
    form_id: str | None = None
    for step in steps or []:
        tool_name = str((step or {}).get("tool", "") or "").lower()
        result = str((step or {}).get("result", "") or "")
        if tool_name == "create_form":
            saw_create = True
            form_id = form_id or _extract_form_id_from_text(result)
        elif tool_name == "batch_update_form":
            saw_batch = True
        elif tool_name == "get_form":
            saw_get = True
            form_id = form_id or _extract_form_id_from_text(result)
    return (saw_create and (not saw_batch or not saw_get)), form_id


def _needs_google_sheets_followup(user_message: str, steps: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not _is_google_sheets_authoring_intent(user_message):
        return False, None

    saw_create_spreadsheet = False
    saw_content_write = False
    spreadsheet_id: str | None = None
    for step in steps or []:
        tool_name = str((step or {}).get("tool", "") or "").lower()
        result = str((step or {}).get("result", "") or "")
        if tool_name == "create_spreadsheet":
            saw_create_spreadsheet = True
            spreadsheet_id = spreadsheet_id or _extract_spreadsheet_id_from_text(result)
        elif tool_name in {"modify_sheet_values", "append_table_rows"}:
            saw_content_write = True
            spreadsheet_id = spreadsheet_id or _extract_spreadsheet_id_from_text(result)
        elif (
            "sheet" in tool_name
            and any(marker in tool_name for marker in ("write", "update_values", "append"))
        ):
            saw_content_write = True
            spreadsheet_id = spreadsheet_id or _extract_spreadsheet_id_from_text(result)

    return (saw_create_spreadsheet and not saw_content_write), spreadsheet_id


def _needs_google_slides_followup(user_message: str, steps: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not _is_google_slides_relayout_intent(user_message):
        return False, None

    saw_create_presentation = False
    saw_content_update = False
    presentation_id: str | None = None
    requested_slides = _extract_requested_slide_count(user_message)
    max_total_slides: int | None = None
    for step in steps or []:
        tool_name = str((step or {}).get("tool", "") or "").lower()
        result = str((step or {}).get("result", "") or "")
        total_slides = _extract_presentation_total_slides(result)
        if total_slides is not None:
            max_total_slides = max(max_total_slides or 0, total_slides)
        if tool_name == "create_presentation":
            saw_create_presentation = True
            presentation_id = presentation_id or _extract_presentation_id_from_text(result)
        elif tool_name == "batch_update_presentation":
            if _slides_batch_args_have_text_write((step or {}).get("args")):
                saw_content_update = True
            presentation_id = presentation_id or _extract_presentation_id_from_text(result)
        elif tool_name in {"get_presentation", "get_page"}:
            presentation_id = presentation_id or _extract_presentation_id_from_text(result)
            if _presentation_result_has_non_empty_text(result):
                saw_content_update = True

    needs_more_slides = (
        saw_create_presentation
        and bool(requested_slides)
        and max_total_slides is not None
        and max_total_slides < int(requested_slides or 0)
    )
    return (saw_create_presentation and (not saw_content_update or needs_more_slides)), presentation_id


def _build_google_mcp_validation_reply(error_text: str) -> str:
    e = (error_text or "").lower()
    if "batch_update_presentation" in e and "requests" in e and "missing required argument" in e:
        return (
            "Maaf, edit Google Slides belum berhasil dijalankan karena format perintah editnya belum lengkap. "
            "Saya harus ambil struktur presentasinya dulu lalu kirim perubahan slide dalam format edit yang benar. "
            "Silakan coba lagi, sekarang agent sudah diarahkan untuk ambil struktur slide dulu sebelum mengedit."
        )
    if (
        "batch_update_presentation" in e
        and "invalid slides batch update request" in e
        and "inserttext.objectid" in e
    ):
        return (
            "Maaf, edit Google Slides belum berhasil karena teks diarahkan ke ID slide halaman, "
            "padahal insertText harus ke shape/text box. Saya perlu buat shape dulu lalu isi teks ke shape tersebut. "
            "Silakan coba lagi."
        )
    if (
        "batch_update_presentation" in e
        and ("invalid value" in e or "unknown dimension unit" in e or "unit_unspecified" in e)
        and "dimension" in e
        and ("create_shape" in e or "createshape" in e)
    ):
        return (
            "Maaf, edit Google Slides belum berhasil karena ukuran elemen slide tidak valid. "
            "Saya harus pakai size/transform dengan unit yang benar (PT) dan dimensi yang wajar, lalu kirim ulang editnya. "
            "Silakan coba lagi."
        )
    if (
        "batch_update_presentation" in e
        and "create_shape.shape_type" in e
        and "title" in e
    ):
        return (
            "Maaf, edit Google Slides belum berhasil karena tipe shape yang dipakai bukan tipe yang valid untuk createShape. "
            "Untuk judul dan isi teks, saya harus pakai shape text box dulu, bukan TITLE placeholder. "
            "Silakan coba lagi."
        )
    if (
        "error calling tool 'create_form'" in e
        and "only info.title can be set when creating a form" in e
    ):
        return (
            "Maaf, pembuatan Google Form belum berhasil karena saat create_form hanya field title yang boleh diisi. "
            "Saya harus buat form dulu dengan title saja, lalu isi deskripsi/pertanyaan lewat update lanjutan (batchUpdate). "
            "Silakan coba lagi."
        )
    if (
        "validation error for call[batch_update_form]" in e
        and "missing required argument" in e
        and "requests" in e
    ):
        return (
            "Maaf, pengisian Google Form belum berhasil karena format update belum menyertakan daftar requests. "
            "Saya harus kirim batch_update_form dengan requests yang berisi updateFormInfo dan createItem pertanyaan. "
            "Silakan coba lagi."
        )
    if (
        "error calling tool 'batch_update_form'" in e
        and "request kind was not provided" in e
    ):
        return (
            "Maaf, pengisian Google Form belum berhasil karena requests batch_update_form berisi objek kosong. "
            "Setiap request harus punya jenis operasi seperti updateFormInfo atau createItem. "
            "Untuk pembuatan form baru, lebih aman gunakan create_survey_form agar form dibuat dan diisi dalam satu langkah."
        )
    return (
        "Maaf, aksi Google Workspace belum berhasil dijalankan karena format input tool belum lengkap. "
        "Silakan coba lagi."
    )


def build_google_mcp_usage_notice(user_message: str) -> str:
    notice = "\n\n[SYSTEM NOTICE - GOOGLE WORKSPACE TOOL USAGE]\n"
    notice += (
        "GOOGLE WORKSPACE TOOLING ADALAH PARENT-ONLY EXECUTION. "
        "Jika user meminta Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, Contacts, Chat, atau Apps Script, "
        "main agent WAJIB memanggil tool Google Workspace langsung. "
        "JANGAN delegasikan aksi Google Workspace ke subagent/task(), jangan meminta subagent membuat link, "
        "dan jangan menganggap output task() sebagai bukti file Google sudah dibuat. "
        "Jika perlu bantuan konten, pikirkan outline sendiri lalu tetap eksekusi file/link final dengan tool Google Workspace di parent. "
        "Task selesai hanya setelah tool Google Workspace yang relevan berhasil dan URL/hasilnya berasal dari output tool tersebut. "
        "Saat memakai tool Google Workspace, WAJIB ikuti schema tool secara persis. "
        "Jangan menyebut istilah teknis internal/protokol tool kepada user. "
        "Jangan mengira-ngira nama argumen. Contoh penting: "
        "modify_sheet_values memakai argumen range_name (bukan range); "
        "draft_gmail_message.to/cc/bcc berupa string tunggal; "
        "manage_contact.emails/phones berupa list of objects; "
        "UNTUK SPREADSHEET YANG SUDAH ADA: jangan buat spreadsheet baru dan jangan panggil manage_drive_access hanya agar agent bisa menulis. "
        "Panggil read_sheet_values dulu untuk membaca nama sheet, header, dan data yang ada; gunakan append_table_rows untuk menambah record, "
        "atau modify_sheet_values hanya setelah struktur/range target sudah terbaca. Jangan menebak Sheet1, header, atau range A1/A2. "
        "manage_drive_access hanya boleh dipakai bila Owner secara eksplisit meminta perubahan akses dan penerimanya adalah alamat email Google, bukan nomor WhatsApp. "
        "UNTUK GOOGLE DRIVE: create_drive_folder dipakai untuk membuat folder. create_drive_file hanya untuk upload file jika ada content teks atau fileUrl/file_url valid; "
        "jangan panggil create_drive_file dengan content null dan fileUrl null. Untuk laporan spreadsheet/xlsx baru, gunakan create_spreadsheet + modify_sheet_values, lalu pindahkan file ke folder dengan update_drive_file(add_parents=<folder_id>); "
        "UNTUK GOOGLE CALENDAR: manage_event action update/delete/rsvp WAJIB memakai event_id asli dari Google Calendar. "
        "Jika user meminta edit/hapus/RSVP event tetapi event_id belum ada di konteks, panggil get_events dulu dengan calendar_id, rentang waktu, dan query judul/deskripsi untuk mengambil ID; "
        "baru panggil manage_event dengan event_id tersebut. Jangan kirim event_id None/null. Jika mengubah waktu, sertakan start_time serta end_time; "
        "UNTUK GOOGLE SLIDES: jangan pernah panggil batch_update_presentation tanpa requests; "
        "jika user minta edit slide, WAJIB panggil get_presentation dulu untuk ambil struktur slide/object; "
        "jangan insertText ke page/slide objectId (mis. 'p'), karena insertText hanya valid untuk shape atau table cell; "
        "buat shape/text box dulu (createShape) lalu insertText ke objectId shape tersebut, baru panggil batch_update_presentation; "
        "untuk title/body gunakan createShape.shape_type='TEXT_BOX', bukan 'TITLE' atau placeholder shape lain; "
        "untuk slide yang masih template/placeholder, bersihkan teks placeholder seperti 'Klik - tambahkan judul' dan 'Klik untuk menambahkan subjudul'; "
        "hindari menumpuk banyak teks di koordinat yang sama; gunakan maksimal 2-3 shape utama per slide (title, body kiri, body kanan atau subtitle), "
        "set ukuran dan posisi yang masuk akal, dan jika mengedit semua isi slide lebih aman membuat shape baru yang rapi daripada menulis ke elemen yang tidak jelas; "
        "untuk createShape WAJIB sertakan unit='PT' di elementProperties.size.width, elementProperties.size.height, dan elementProperties.transform; "
        "jangan biarkan unit kosong/UNIT_UNSPECIFIED. Gunakan ukuran konservatif yang valid (contoh title width 300-500 PT, body width 250-350 PT), "
        "hindari width/height ekstrem yang berisiko invalid dimension; "
        "UNTUK GOOGLE FORMS: jika tool create_survey_form tersedia dan user meminta membuat Google Form baru/survei, "
        "GUNAKAN create_survey_form sebagai pilihan utama karena tool itu membuat form, mengisi pertanyaan, dan mengambil link secara aman dalam satu langkah. "
        "Saat memakai create_survey_form, questions WAJIB berisi pertanyaan final yang spesifik dan relevan dengan topik user; "
        "JANGAN gunakan placeholder seperti 'Pertanyaan 1', 'Pertanyaan 2', 'Question 1', atau judul generik serupa. "
        "Setiap question minimal punya title yang bermakna, type (short_answer, paragraph, multiple_choice), required, dan options jika multiple_choice. "
        "Jika harus memakai create_form, create_form hanya boleh mengirim title. "
        "Jangan kirim description/document_title/items saat create_form. Setelah form jadi, lanjutkan isi deskripsi dan pertanyaan via batch_update_form. "
        "Untuk batch_update_form, JANGAN PERNAH kirim requests berupa [{}], objek kosong, atau list berisi request tanpa kind. "
        "Setiap item requests WAJIB punya tepat satu kind valid: updateFormInfo, createItem, updateItem, deleteItem, moveItem, atau updateSettings.\n"
    )
    notice += "[/SYSTEM NOTICE]\n"

    sheets_intent = _is_google_sheets_authoring_intent(user_message)
    if sheets_intent:
        notice += "\n\n[SYSTEM NOTICE - SHEETS WORKFLOW MODE]\n"
        notice += (
            "User meminta pembuatan atau pengeditan Google Sheets. create_spreadsheet hanya membuat file kosong; "
            "Untuk request Google Sheets/spreadsheet, JANGAN panggil manage_event karena manage_event hanya untuk Google Calendar. "
            "JANGAN berhenti setelah create_spreadsheet jika user meminta tabel, data, laporan, tracker, edit, rumus, atau formula. "
            "Workflow wajib untuk spreadsheet baru: "
            "(1) create_spreadsheet dengan title dan sheet_names bila perlu; "
            "(2) modify_sheet_values untuk mengisi header, baris data, dan formula dengan argumen spreadsheet_id, range_name, values, value_input_option='USER_ENTERED'; "
            "(3) format_sheet_range untuk header/angka bila tool tersedia; "
            "(4) resize_sheet_dimensions untuk freeze header dan auto-resize kolom bila tool tersedia; "
            "(5) read_sheet_values dengan include_formulas=True untuk verifikasi. "
            "modify_sheet_values memakai range_name, bukan range. "
            "Jika perlu beberapa tab seperti Pemasukan/Pengeluaran/Ringkasan, sertakan sheet_names=['Pemasukan','Pengeluaran','Ringkasan'] saat create_spreadsheet atau panggil create_sheet sebelum menulis ke tab itu. "
            "Jangan menulis ke range bertab seperti Pemasukan!A1:C10 kecuali tab Pemasukan sudah dibuat atau sudah muncul dari get_spreadsheet_info. "
            "Untuk spreadsheet baru tanpa sheet_names eksplisit, jangan hardcode Sheet1!A1:F10 karena nama tab default bisa berbeda per locale; pakai range tanpa nama sheet seperti A1:F10, atau ambil nama tab dari get_spreadsheet_info dulu. "
            "Untuk rumus, tulis formula sebagai string diawali '=' dan gunakan value_input_option='USER_ENTERED', contoh '=SUM(B2:B10)', '=AVERAGE(C2:C10)', '=IF(D2>=80,\"OK\",\"Review\")'. "
            "Jika user tidak memberi data lengkap, buat tabel template yang relevan dengan konteks user, berisi header siap pakai, beberapa baris contoh wajar, dan kolom formula yang menghitung total/rata-rata/status. "
            "Balasan final harus menyebut sheet sudah diisi dan formula apa yang dibuat, bukan hanya mengirim link file kosong."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    message_lower = (user_message or "").lower()
    explicit_calendar_intent = any(
        marker in message_lower
        for marker in ("google calendar", "calendar", "kalender")
    )
    calendar_like_non_sheet_intent = any(
        marker in (user_message or "").lower()
        for marker in ("jadwal", "event", "reminder", "meeting", "rapat", "edit juga")
    )
    if explicit_calendar_intent or (calendar_like_non_sheet_intent and not sheets_intent):
        notice += "\n\n[SYSTEM NOTICE - CALENDAR EDIT WORKFLOW]\n"
        notice += (
            "Untuk edit/hapus kalender, urutan wajib adalah: "
            "(1) jika event_id belum diketahui, panggil get_events dengan calendar_id yang relevan, query dari judul/deskripsi, dan time_min/time_max jika ada; "
            "(2) pilih event yang cocok dari output get_events dan ambil nilai `ID:` / `Event ID:`; "
            "(3) panggil manage_event action update/delete/rsvp dengan event_id tersebut. "
            "Jika get_events menemukan beberapa kandidat, jangan menebak; minta user memilih event atau waktu yang lebih spesifik. "
            "Jika get_events tidak menemukan event, sampaikan bahwa event tidak ditemukan dan jangan membuat event baru kecuali user eksplisit minta create."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    if _is_google_slides_relayout_intent(user_message):
        requested_slides = _extract_requested_slide_count(user_message) or 3
        notice += "\n\n[SYSTEM NOTICE - SLIDES TEMPLATE MODE]\n"
        notice += (
            f"User meminta pembuatan/perapihan Google Slides. Targetkan {requested_slides} slide yang rapi, ringkas, dan mudah dibaca. "
            "create_presentation hanya membuat file kosong; JANGAN berhenti setelah create_presentation jika user meminta dibuatkan slide/presentasi. "
            "Workflow wajib untuk presentasi baru: (1) create_presentation; (2) get_presentation untuk ambil slide ID awal; "
            "(3) batch_update_presentation untuk mengisi konten dengan createShape + insertText; (4) get_presentation lagi untuk verifikasi teks sudah ada. "
            "WAJIB gunakan pola: createSlide (jika perlu) -> createShape title/body -> insertText ke SHAPE saja. "
            "DILARANG insertText ke page/slide objectId. "
            "Untuk createShape title/body, shape_type harus TEXT_BOX; jangan gunakan TITLE/BODY placeholder type. "
            "Tiap slide maksimal 2-3 shape utama dan hindari overlap. "
            "Ringkas konten panjang menjadi poin inti; jangan dump semua paragraf mentah. "
            "Jika user hanya memberi topik, buat outline presentasi sendiri yang relevan: cover, 1-2 slide isi utama, dan penutup/rekomendasi. "
            "Jika elemen lama tidak jelas, buat shape baru dengan objectId unik yang eksplisit. "
            "Untuk createShape, selalu pakai size.unit='PT' dan transform.unit='PT' dengan nilai konservatif (hindari dimensi ekstrem). "
            "Saat user minta 'buatkan slide', 'rapihkan', atau 'jadikan N slide', task belum selesai sebelum batch_update_presentation berhasil membuat teks nyata di slide."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    if _is_google_forms_authoring_intent(user_message):
        notice += "\n\n[SYSTEM NOTICE - FORMS WORKFLOW MODE]\n"
        notice += (
            "User meminta pembuatan/pengisian Google Form. Jika tool create_survey_form tersedia, WAJIB prioritaskan create_survey_form untuk form baru. "
            "Isi argumen title, description, topic_hint, dan questions bila user memberi pertanyaan spesifik. "
            "Jika user tidak memberi daftar pertanyaan rinci, buatkan draft questions relevan minimal 5-8 pertanyaan sesuai konteks user, dengan tipe campuran seperlunya (short_answer, paragraph, multiple_choice). "
            "JANGAN buat title pertanyaan berupa placeholder seperti 'Pertanyaan 1', 'Pertanyaan 2', 'Question 1', atau sekadar nomor. "
            "Tulis title pertanyaan yang siap dibaca responden dan terkait langsung dengan topik. "
            "Untuk multiple_choice, isi options minimal 2-5 opsi bermakna. "
            "Contoh questions valid untuk create_survey_form: "
            "[{title:'Seberapa sering Anda mengikuti demonstrasi?',type:'multiple_choice',required:true,options:['Tidak pernah','Kadang-kadang','Sering']},"
            "{title:'Menurut Anda, apa dampak utama kegiatan tersebut?',type:'paragraph',required:false}]. "
            "Jika create_survey_form tidak tersedia, jalankan workflow manual end-to-end: "
            "(1) create_form dengan title saja; "
            "(2) batch_update_form dengan requests valid berisi updateFormInfo dan createItem pertanyaan; "
            "(3) get_form untuk verifikasi hasil dan ambil responder URL/edit URL; "
            "(4) balas user dengan link final. "
            "Saat user minta link, pastikan URL form dikirim di jawaban final dan jangan jawab normatif tanpa eksekusi tool. "
            "Untuk batch_update_form, requests tidak boleh kosong secara semantik: jangan pernah kirim [{}]. "
            "Contoh request valid: {updateFormInfo:{info:{description:'...'},updateMask:'description'}} atau "
            "{createItem:{item:{title:'Pertanyaan',questionItem:{question:{required:true,textQuestion:{}}}},location:{index:0}}}."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    return notice


def build_mcp_unavailable_notice(mcp_errors: dict[str, str], google_mcp_auth_url: str | None) -> str:
    notice = "\n\n[SYSTEM NOTICE - CONNECTED TOOL UNAVAILABLE]\n"
    notice += (
        "HARD RULE: Jika tool integrasi yang dibutuhkan user sedang unavailable, "
        "JANGAN pernah mengklaim pekerjaan sudah diproses, diupdate, sedang berjalan, atau selesai. "
        "JANGAN membuat janji seperti 'lagi diproses', 'sebentar lagi selesai', atau 'nanti saya kirim link'. "
        "Jawab secara jujur bahwa aksi belum dieksekusi.\n"
    )
    for server_name, error in mcp_errors.items():
        if "401" in error or "Unauthorized" in error:
            fallback = ""
            if server_name == "google_workspace" and google_mcp_auth_url:
                fallback = f"Jika tool gagal, fallback link ini: {google_mcp_auth_url}. "
            notice += (
                f"- {server_name}: Akun Google belum terhubung atau token tidak valid. "
                f"Panggil tool get_google_workspace_auth_link untuk mengambil link re-auth terbaru, "
                f"lalu jelaskan ke user dalam bahasa user secara natural. "
                f"{fallback}"
                f"JANGAN coba mencari file credential, token, "
                f"atau mengakses email/kalender dengan cara lain.\n"
            )
        else:
            notice += (
                f"- {server_name}: Koneksi gagal ({error[:100]}). "
                f"Beritahu user bahwa layanan ini sedang tidak tersedia.\n"
            )
    notice += "[/SYSTEM NOTICE]\n"
    return notice


def build_google_mcp_runtime_state_notice(runtime: GoogleMcpRuntime) -> str:
    if not runtime.enabled or not runtime.workspace_server:
        state = "disabled"
        action = "Google Workspace tidak aktif untuk agent ini. Jangan klaim bisa mengakses Google."
    elif runtime.connected_user_id and not runtime.preflight_error:
        state = "connected"
        action = (
            "Google Workspace sudah terhubung untuk Owner/user yang sesuai. "
            "Gunakan tool Google Workspace sebagai sumber kebenaran sebelum mengklaim aksi berhasil."
        )
    elif runtime.auth_url:
        state = "enabled_needs_auth"
        action = (
            "Google Workspace aktif tapi belum bisa dipakai sampai Owner membuka link otentikasi. "
            "Jika user meminta aksi Google, minta Owner login lewat link yang tersedia dan jangan mengarang hasil."
        )
    elif runtime.preflight_error:
        state = "auth_error"
        action = (
            "Google Workspace aktif tapi koneksi/auth sedang bermasalah. "
            "Jelaskan bahwa Owner perlu menghubungkan ulang atau admin platform perlu mengecek integrasi."
        )
    else:
        state = "enabled_unknown_auth"
        action = (
            "Google Workspace aktif, tetapi status auth belum terkonfirmasi. "
            "Panggil tool Google/reauth yang tersedia dulu; jangan klaim sukses sebelum tool Google berhasil."
        )

    lines = [
        "\n\n## Google Workspace Runtime State",
        f"- State: {state}",
        f"- Connected User: {runtime.connected_user_id or 'none'}",
        f"- Auth Link Available: {'yes' if runtime.auth_url else 'no'}",
        f"- Preflight Error: {runtime.preflight_error or 'none'}",
        f"- Rule: {action}",
    ]
    if runtime.auth_url:
        lines.append(f"- Link otentikasi untuk Owner: {runtime.auth_url}")
    return "\n".join(lines)


def google_slides_dimension_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE SLIDES DIMENSION]\n"
        "Perbaiki payload createShape sekarang juga.\n"
        "WAJIB: setiap createShape.elementProperties.size.width/height punya field magnitude + unit='PT'.\n"
        "WAJIB: createShape.elementProperties.transform.unit='PT'. Jangan biarkan unit kosong atau UNIT_UNSPECIFIED.\n"
        "WAJIB: createShape.shape_type untuk title/body harus TEXT_BOX, bukan TITLE atau placeholder lain.\n"
        "Gunakan dimensi konservatif valid (mis: title width 420PT height 60PT; body width 420PT height 220PT), hindari angka ekstrem.\n"
        "Jangan hapus semua slide sekaligus jika tidak perlu; fokus relayout aman.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_slides_shape_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE SLIDES]\n"
        "Perbaiki langkah edit Google Slides sekarang juga.\n"
        "WAJIB: jangan insertText ke page/slide objectId (contoh: 'p', 'slide2').\n"
        "Langkah benar: get_presentation/get_page -> identifikasi slide target -> createShape pada pageObjectId slide -> "
        "insertText ke objectId shape yang baru dibuat.\n"
        "Untuk shape title/body, gunakan createShape.shape_type='TEXT_BOX'.\n"
        "Untuk rapihkan konten jadi beberapa slide, buat shape title/body per slide dengan posisi tidak overlap.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_slides_followup_directive(presentation_id: str, user_message: str) -> str:
    requested_slides = _extract_requested_slide_count(user_message) or 3
    return (
        "[SYSTEM FOLLOW-UP DIRECTIVE - GOOGLE SLIDES]\n"
        f"Presentation sudah dibuat dengan presentation_id={presentation_id}, tetapi kontennya belum dibuat. "
        "Lanjutkan SEKARANG juga sampai slide berisi teks nyata, bukan file kosong. "
        "WAJIB panggil get_presentation terlebih dahulu untuk mengambil slide ID yang ada. "
        "Lalu panggil batch_update_presentation dengan requests non-kosong untuk membuat konten. "
        f"Targetkan {requested_slides} slide total yang relevan dengan request user berikut: "
        f"{user_message[:500]}. "
        "Gunakan slide pertama yang sudah ada untuk cover atau pembuka; bila butuh slide tambahan, buat dengan createSlide. "
        "Untuk setiap slide, buat shape title/body dengan createShape, lalu insertText ke objectId shape tersebut. "
        "JANGAN insertText ke objectId slide/page. "
        "Untuk title/body shape, gunakan createShape.shape_type='TEXT_BOX'. "
        "Setiap createShape.elementProperties.size.width/height harus punya magnitude + unit='PT', dan transform.unit='PT'. "
        "Gunakan objectId unik yang eksplisit seperti slide1_title, slide1_body, slide2_title. "
        "Jika user tidak memberi materi rinci, buat outline presentasi yang wajar dari topik user: cover, poin utama, detail/analisis, dan penutup/rekomendasi sesuai jumlah slide. "
        "Setelah batch_update_presentation berhasil, WAJIB panggil get_presentation lagi untuk verifikasi bahwa setiap slide punya text tidak kosong. "
        "Balasan final HARUS berisi link Google Slides serta ringkasan isi tiap slide yang dibuat. "
        "[/SYSTEM FOLLOW-UP DIRECTIVE]"
    )


def google_forms_create_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE FORMS]\n"
        "Perbaiki langkah pembuatan Google Form sekarang juga.\n"
        "WAJIB: saat create_form hanya kirim title saja.\n"
        "JANGAN kirim description/document_title/items/settings saat create_form.\n"
        "Setelah create_form berhasil, lanjutkan update deskripsi/pertanyaan dengan tool update/batchUpdate forms.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_forms_request_kind_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE FORMS REQUEST KIND]\n"
        "Perbaiki sekarang: batch_update_form gagal karena ada request kosong atau request tanpa kind.\n"
        "JANGAN panggil batch_update_form dengan requests=[{}] atau list berisi objek kosong.\n"
        "Jika tool create_survey_form tersedia dan task adalah membuat form baru, gunakan create_survey_form sekarang.\n"
        "Saat memakai create_survey_form, questions harus berisi pertanyaan spesifik sesuai topik user, bukan placeholder seperti 'Pertanyaan 1'.\n"
        "Jika harus batch_update_form, setiap request WAJIB punya satu kind valid: updateFormInfo atau createItem.\n"
        "Contoh updateFormInfo valid: {updateFormInfo:{info:{description:'...'},updateMask:'description'}}.\n"
        "Contoh createItem valid: {createItem:{item:{title:'Pertanyaan',questionItem:{question:{required:true,textQuestion:{}}}},location:{index:0}}}.\n"
        "Lanjutkan sampai get_form/link final berhasil.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_forms_followup_directive(form_id: str) -> str:
    return (
        "[SYSTEM FOLLOW-UP DIRECTIVE - GOOGLE FORMS]\n"
        f"Form sudah dibuat dengan form_id={form_id}. "
        "Lanjutkan SEKARANG juga workflow yang belum selesai. "
        "WAJIB panggil batch_update_form DENGAN ARGUMEN requests (list non-kosong). "
        "JANGAN kirim requests=[{}] atau request kosong tanpa kind. "
        "JANGAN gunakan judul placeholder seperti 'Pertanyaan 1'; setiap createItem.item.title harus berupa pertanyaan final yang relevan dengan topik user. "
        "Contoh struktur minimal yang VALID untuk requests: "
        "[{updateFormInfo:{info:{description:'...'},updateMask:'description'}}, "
        "{createItem:{item:{title:'Pertanyaan 1',questionItem:{question:{required:true,textQuestion:{}}}},location:{index:0}}}] . "
        "Tambah minimal 5 createItem pertanyaan relevan jika user belum kasih daftar rinci. "
        "Setelah batch_update_form berhasil, WAJIB panggil get_form agar responder URL/edit URL terambil. "
        "Balasan final HARUS berisi link Google Form dan ringkasan pertanyaan yang ditambahkan. "
        "[/SYSTEM FOLLOW-UP DIRECTIVE]"
    )


def google_forms_followup_retry_directive() -> str:
    return (
        "[SYSTEM FOLLOW-UP RETRY DIRECTIVE - GOOGLE FORMS REQUESTS]\n"
        "Perbaiki sekarang: batch_update_form WAJIB menyertakan requests sebagai list non-kosong.\n"
        "JANGAN kirim [{}]; setiap request harus punya kind valid seperti updateFormInfo atau createItem.\n"
        "JANGAN gunakan title placeholder seperti 'Pertanyaan 1'. Tulis pertanyaan final yang bermakna dan relevan.\n"
        "Gunakan urutan: updateFormInfo(description) + minimal 5 createItem pertanyaan + get_form.\n"
        "Jangan panggil batch_update_form tanpa requests.\n"
        "[/SYSTEM FOLLOW-UP RETRY DIRECTIVE]"
    )


def google_sheets_followup_directive(spreadsheet_id: str, user_message: str) -> str:
    return (
        "[SYSTEM FOLLOW-UP DIRECTIVE - GOOGLE SHEETS]\n"
        f"Spreadsheet sudah dibuat dengan spreadsheet_id={spreadsheet_id}, tetapi isinya belum dibuat. "
        "Lanjutkan SEKARANG juga workflow spreadsheet sampai ada tabel/data/rumus yang benar. "
        "WAJIB panggil modify_sheet_values dengan argumen spreadsheet_id, range_name, values, dan value_input_option='USER_ENTERED'. "
        "JANGAN gunakan argumen bernama range; tool ini memakai range_name. "
        "Untuk file baru, pakai range_name tanpa nama sheet seperti A1:F10 kecuali kamu sudah tahu nama tab sebenarnya dari get_spreadsheet_info. "
        "JANGAN hardcode Sheet1!A1:F10 karena tab default bisa bernama berbeda dan memicu Unable to parse range. "
        "Buat tabel yang relevan dengan request user berikut: "
        f"{user_message[:500]}. "
        "Jika user tidak memberi data rinci, buat template praktis dengan header siap pakai, beberapa baris contoh, dan minimal satu kolom formula. "
        "Formula harus ditulis sebagai string diawali '=' agar Google Sheets menghitungnya, contoh '=SUM(B2:B10)', '=AVERAGE(C2:C10)', '=IF(D2>=80,\"OK\",\"Review\")'. "
        "Setelah values berhasil ditulis, rapikan dengan format_sheet_range untuk header dan resize_sheet_dimensions untuk freeze header/auto-resize kolom bila tool tersedia. "
        "Terakhir, panggil read_sheet_values dengan include_formulas=True untuk verifikasi. "
        "Balasan final HARUS berisi link spreadsheet serta ringkasan tabel dan formula yang dibuat. "
        "[/SYSTEM FOLLOW-UP DIRECTIVE]"
    )


async def _fetch_google_auth_link(
    *, integration_url: str, api_key: str, agent_id: uuid.UUID, candidate_user_ids: list[str]
) -> str | None:
    if not integration_url:
        return None
    try:
        import httpx as _httpx

        async with _httpx.AsyncClient(timeout=8.0) as _hc:
            for candidate in candidate_user_ids:
                resp = await _hc.post(
                    f"{integration_url}/v1/integrations/google/connect",
                    json={"external_user_id": candidate, "agent_id": str(agent_id)},
                    headers={"X-API-Key": api_key},
                )
                if resp.status_code == 200:
                    data = resp.json() if resp.text else {}
                    auth_url = data.get("auth_url") or data.get("authorization_url")
                    if auth_url:
                        auth_url = str(auth_url)
                        if auth_url.startswith("http://") or auth_url.startswith("https://"):
                            return auth_url
    except Exception:
        return None
    return None


def _has_google_mcp_step(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        tool_name = str((step or {}).get("tool", "")).lower()
        if tool_name and _is_google_mcp_tool_name(tool_name):
            return True
        result = str((step or {}).get("result", "") or "")
        if tool_name == "task" and _contains_google_workspace_artifact(result):
            return True
    return False


def _has_google_workspace_artifact_step(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        result = str((step or {}).get("result", "") or "")
        if _contains_google_workspace_artifact(result):
            return True
    return False


def _candidate_external_user_ids(primary: str | None, channel_user_phone: str | None) -> list[str]:
    vals: list[str] = []
    for raw in (primary, channel_user_phone):
        if not raw:
            continue
        s = str(raw).strip()
        if s:
            vals.append(s)

    candidates: list[str] = []
    seen: set[str] = set()
    for value in vals:
        variants = [value]
        if value.startswith("+"):
            variants.append(value[1:])
        if value.isdigit() and not value.startswith("+"):
            variants.append(f"+{value}")
            if value.startswith("62"):
                variants.append("0" + value[2:])
        if value.startswith("0") and value[1:].isdigit():
            variants.append("62" + value[1:])
            variants.append("+62" + value[1:])
        if "@" in value:
            variants.append(value.split("@", 1)[0])

        for variant in variants:
            key = variant.strip()
            if key and key not in seen:
                seen.add(key)
                candidates.append(key)
    return candidates


def _build_google_reauth_tool(
    *,
    integration_url: str,
    api_key: str,
    agent_id: uuid.UUID,
    candidate_user_ids: list[str],
    preferred_auth_url: str | None = None,
) -> list:
    @tool
    async def get_google_workspace_auth_link() -> str:
        """Generate and return Google Workspace re-auth link for current user."""
        if preferred_auth_url:
            return preferred_auth_url
        auth_url = await _fetch_google_auth_link(
            integration_url=integration_url,
            api_key=api_key,
            agent_id=agent_id,
            candidate_user_ids=candidate_user_ids,
        )
        if not auth_url:
            return (
                "AUTH_LINK_UNAVAILABLE: Layanan integrasi Google tidak dapat dihubungi saat ini. "
                "JANGAN minta user login ulang — user mungkin sudah berhasil login sebelumnya. "
                "Beritahu user bahwa ada gangguan sementara pada integrasi Google dan minta coba kirim pesan lagi "
                "beberapa menit kemudian. Jangan mengarang link atau mengklaim aksi Google berhasil."
            )
        return auth_url

    return [get_google_workspace_auth_link]


def sanitize_google_forms_tools(mcp_tools: list, log: Any) -> list:
    """Wrap Google Workspace tools to repair weak LLM payloads before MCP execution."""
    wrapped_tools: list = []
    sanitized_tool_names: list[str] = []
    get_events_tool = next((tool for tool in mcp_tools if getattr(tool, "name", "") == "get_events"), None)
    create_sheet_tool = next((tool for tool in mcp_tools if getattr(tool, "name", "") == "create_sheet"), None)
    read_sheet_values_available = any(
        getattr(tool, "name", "") == "read_sheet_values" for tool in mcp_tools
    )
    inspected_spreadsheet_ids: set[str] = set()
    created_spreadsheet_ids: set[str] = set()
    for mcp_tool in mcp_tools:
        tool_name = getattr(mcp_tool, "name", "")
        if tool_name == "create_spreadsheet":
            def _build_create_spreadsheet_guarded(tool_to_call: Any):
                async def _create_spreadsheet_guarded(**kwargs):
                    spreadsheet_title = kwargs.pop("spreadsheet_title", None)
                    name = kwargs.pop("name", None)
                    file_name = kwargs.pop("file_name", None)
                    title = kwargs.get("title") or spreadsheet_title or name or file_name
                    if not title:
                        return (
                            "SHEETS_TITLE_REQUIRED: create_spreadsheet membutuhkan title. "
                            "Tentukan judul spreadsheet dari request user, lalu panggil create_spreadsheet(title=...)."
                        )
                    kwargs["title"] = str(title)
                    if kwargs.get("sheet_names") is None:
                        kwargs.pop("sheet_names", None)
                    elif "sheet_names" in kwargs:
                        kwargs["sheet_names"] = _normalize_string_list_arg(kwargs.get("sheet_names"))
                    result = await tool_to_call.ainvoke(kwargs)
                    spreadsheet_id = _extract_spreadsheet_id_from_text(str(result))
                    if spreadsheet_id:
                        created_spreadsheet_ids.add(spreadsheet_id)
                    return result

                return _create_spreadsheet_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_create_spreadsheet_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=CreateSpreadsheetArgs,
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "create_presentation":
            def _build_create_presentation_guarded(tool_to_call: Any):
                async def _create_presentation_guarded(**kwargs):
                    presentation_title = kwargs.pop("presentation_title", None)
                    name = kwargs.pop("name", None)
                    file_name = kwargs.pop("file_name", None)
                    title = kwargs.get("title") or presentation_title or name or file_name
                    if not title:
                        return (
                            "SLIDES_TITLE_REQUIRED: create_presentation membutuhkan title. "
                            "Tentukan judul presentasi dari request user, lalu panggil create_presentation(title=...)."
                        )
                    kwargs["title"] = str(title)
                    return await tool_to_call.ainvoke(kwargs)

                return _create_presentation_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_create_presentation_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=CreatePresentationArgs,
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "create_drive_file":
            def _build_create_drive_file_guarded(tool_to_call: Any):
                async def _create_drive_file_guarded(**kwargs):
                    file_url_alias = kwargs.pop("file_url", None)
                    if not kwargs.get("fileUrl") and file_url_alias:
                        kwargs["fileUrl"] = file_url_alias

                    content = kwargs.get("content")
                    file_url = kwargs.get("fileUrl")
                    mime_type = str(kwargs.get("mime_type") or "text/plain")
                    file_name = str(kwargs.get("file_name") or "")
                    has_content = content is not None and str(content) != ""
                    has_file_url = file_url is not None and str(file_url).strip() != ""
                    is_folder = mime_type == "application/vnd.google-apps.folder"
                    if not has_content and not has_file_url and not is_folder:
                        lower_name = file_name.lower()
                        if lower_name.endswith((".xlsx", ".xls", ".csv")) or "spreadsheet" in mime_type:
                            return (
                                "DRIVE_FILE_SOURCE_REQUIRED: create_drive_file tidak bisa upload spreadsheet tanpa content atau fileUrl. "
                                "Untuk membuat laporan spreadsheet baru di Google Drive, gunakan workflow ini: "
                                "1) create_spreadsheet(title=...), 2) modify_sheet_values(...) untuk mengisi data, "
                                "3) jika perlu masuk folder tertentu, panggil update_drive_file(file_id=<spreadsheet_id>, add_parents=<folder_id>). "
                                "Jangan mengklaim file sudah diupload sampai tool Sheets/Drive berhasil."
                            )
                        if "." not in file_name:
                            return (
                                "DRIVE_FOLDER_OR_CONTENT_REQUIRED: Jika user meminta folder, panggil create_drive_folder(folder_name=..., parent_folder_id=...). "
                                "Jika user meminta file, create_drive_file wajib diberi content teks atau fileUrl/file_url yang bisa diakses server integrasi."
                            )
                        return (
                            "DRIVE_FILE_SOURCE_REQUIRED: create_drive_file wajib diberi salah satu dari content atau fileUrl/file_url. "
                            "Server integrasi tidak bisa mengupload file kosong atau file lokal sandbox yang tidak diberikan sebagai URL. "
                            "Buat/ambil konten file dulu, atau gunakan tool Google native yang sesuai seperti create_spreadsheet/create_presentation/create_doc."
                        )

                    return await tool_to_call.ainvoke(kwargs)

                return _create_drive_file_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_create_drive_file_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=CreateDriveFileArgs,
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "manage_event":
            def _build_manage_event_guarded(tool_to_call: Any):
                async def _manage_event_guarded(**kwargs):
                    action = str(kwargs.get("action") or "").lower().strip()
                    event_id_value = kwargs.get("event_id")
                    event_id_missing_or_invalid = (
                        _is_missing_calendar_event_id(event_id_value)
                        or _looks_like_calendar_id_not_event_id(event_id_value)
                    )
                    if action in {"update", "delete", "rsvp"} and event_id_missing_or_invalid:
                        if get_events_tool is None:
                            return (
                                "CALENDAR_EVENT_ID_REQUIRED: manage_event action "
                                f"'{action}' membutuhkan event_id asli dari output get_events. "
                                "Jangan memakai calendar_id/email kalender sebagai event_id. "
                                "Panggil get_events dulu untuk mencari event, ambil nilai ID/Event ID dari hasilnya, "
                                "lalu ulangi manage_event dengan event_id tersebut."
                            )

                        calendar_id = str(kwargs.get("calendar_id") or "primary")
                        if (
                            _looks_like_calendar_id_not_event_id(event_id_value)
                            and calendar_id == "primary"
                        ):
                            calendar_id = str(event_id_value).strip()
                        summary = str(kwargs.get("summary") or "").strip() or None
                        start_time = str(kwargs.get("start_time") or "").strip() or None
                        end_time = str(kwargs.get("end_time") or "").strip() or None
                        try:
                            ids, lookup_text = await _lookup_calendar_event_ids(
                                get_events_tool=get_events_tool,
                                calendar_id=calendar_id,
                                summary=summary,
                                start_time=start_time,
                                end_time=end_time,
                                log=log,
                            )
                        except Exception as exc:
                            return (
                                "CALENDAR_EVENT_ID_REQUIRED: manage_event tidak bisa dilanjutkan tanpa event_id, "
                                f"dan lookup get_events gagal: {exc}. Panggil get_events secara eksplisit, lalu ulangi manage_event dengan event_id."
                            )

                        if len(ids) == 1:
                            retry_kwargs = dict(kwargs)
                            retry_kwargs["event_id"] = ids[0]
                            retry_kwargs["calendar_id"] = calendar_id
                            log.warning(
                                "agent_run.calendar_manage_event_auto_event_id",
                                action=action,
                                event_id=ids[0],
                                calendar_id=calendar_id,
                                summary=summary,
                            )
                            return await tool_to_call.ainvoke(retry_kwargs)

                        if len(ids) > 1:
                            return (
                                "CALENDAR_EVENT_ID_AMBIGUOUS: get_events menemukan beberapa kandidat event. "
                                "Jangan menebak event_id. Minta user memilih event yang tepat, atau ulangi get_events dengan waktu/query yang lebih spesifik.\n\n"
                                f"Hasil get_events:\n{lookup_text}"
                            )

                        return (
                            "CALENDAR_EVENT_NOT_FOUND: Tidak ada event yang cocok untuk di-update/delete/rsvp. "
                            "Jangan membuat event baru kecuali user eksplisit minta create. "
                            "Minta user memberi judul/waktu event yang lebih spesifik, atau panggil get_events dengan rentang waktu lebih luas.\n\n"
                            f"Hasil get_events:\n{lookup_text}"
                        )

                    return await tool_to_call.ainvoke(kwargs)

                return _manage_event_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_manage_event_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "batch_update_presentation":
            def _build_batch_update_presentation_guarded(tool_to_call: Any):
                async def _batch_update_presentation_guarded(**kwargs):
                    original_requests = kwargs.get("requests")
                    if isinstance(original_requests, dict):
                        if isinstance(original_requests.get("requests"), list):
                            original_requests = original_requests["requests"]
                        else:
                            original_requests = [original_requests]
                    if not original_requests:
                        return (
                            "SLIDES_REQUESTS_REQUIRED: batch_update_presentation membutuhkan requests non-kosong. "
                            "Untuk membuat konten slide, gunakan list request Google Slides API seperti createSlide, createShape, lalu insertText ke objectId shape."
                        )
                    normalized_requests = _normalize_slides_batch_requests(original_requests)
                    if normalized_requests is not original_requests:
                        kwargs["requests"] = normalized_requests
                    try:
                        return await tool_to_call.ainvoke(kwargs)
                    except Exception as exc:
                        err = str(exc).lower()
                        if (
                            "batch_update_presentation" in err
                            and (
                                "unknown dimension unit" in err
                                or "unit_unspecified" in err
                                or "invalid value" in err
                            )
                            and "dimension" in err
                        ):
                            retry_kwargs = dict(kwargs)
                            retry_kwargs["requests"] = _normalize_slides_batch_requests(
                                retry_kwargs.get("requests")
                            )
                            log.warning(
                                "agent_run.slides_dimension_retry_guard",
                                error=str(exc)[:300],
                            )
                            return await tool_to_call.ainvoke(retry_kwargs)
                        raise

                return _batch_update_presentation_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_batch_update_presentation_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=BatchUpdatePresentationArgs,
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "create_shape":
            def _build_create_shape_guarded(tool_to_call: Any):
                async def _create_shape_guarded(**kwargs):
                    normalized_kwargs = _normalize_create_shape_kwargs(kwargs)
                    return await tool_to_call.ainvoke(normalized_kwargs)

                return _create_shape_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_create_shape_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "read_sheet_values":
            def _build_read_sheet_values_guarded(tool_to_call: Any):
                async def _read_sheet_values_guarded(**kwargs):
                    result = await tool_to_call.ainvoke(kwargs)
                    spreadsheet_id = str(kwargs.get("spreadsheet_id") or "").strip()
                    if spreadsheet_id:
                        inspected_spreadsheet_ids.add(spreadsheet_id)
                    return result

                return _read_sheet_values_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_read_sheet_values_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "modify_sheet_values":
            def _build_modify_sheet_values_guarded(tool_to_call: Any):
                async def _modify_sheet_values_guarded(**kwargs):
                    spreadsheet_id = str(kwargs.get("spreadsheet_id") or "").strip()
                    if (
                        read_sheet_values_available
                        and spreadsheet_id
                        and spreadsheet_id not in inspected_spreadsheet_ids
                        and spreadsheet_id not in created_spreadsheet_ids
                    ):
                        return (
                            "SHEETS_STRUCTURE_REQUIRED: Sebelum mengubah spreadsheet yang sudah ada, "
                            "panggil read_sheet_values(spreadsheet_id=..., range_name='A1:ZZ20') untuk "
                            "membaca nama sheet, header, dan baris yang tersedia. Jangan menebak Sheet1, "
                            "range A1, atau posisi baris."
                        )
                    range_alias = kwargs.pop("range", None)
                    if not kwargs.get("range_name") and range_alias:
                        kwargs["range_name"] = range_alias
                    if not kwargs.get("range_name") and kwargs.get("values") is not None:
                        kwargs["range_name"] = "A1"
                    kwargs["values"] = _normalize_sheet_values_for_mcp(kwargs.get("values"))
                    range_name = str(kwargs.get("range_name") or "")
                    try:
                        return await tool_to_call.ainvoke(kwargs)
                    except Exception as exc:
                        err = str(exc)
                        missing_range = _split_simple_sheet_range(range_name)
                        if missing_range and "unable to parse range" in err.lower():
                            sheet_name, cell_range = missing_range
                            if create_sheet_tool is not None:
                                try:
                                    await create_sheet_tool.ainvoke(
                                        {
                                            "spreadsheet_id": kwargs.get("spreadsheet_id"),
                                            "sheet_name": sheet_name,
                                        }
                                    )
                                    retry_kwargs = dict(kwargs)
                                    log.warning(
                                        "agent_run.sheets_missing_tab_created_retry",
                                        sheet_name=sheet_name,
                                        range_name=range_name,
                                    )
                                    return await tool_to_call.ainvoke(retry_kwargs)
                                except Exception as create_exc:
                                    create_err = str(create_exc).lower()
                                    if "already exists" not in create_err and "already exist" not in create_err:
                                        log.warning(
                                            "agent_run.sheets_missing_tab_create_failed",
                                            sheet_name=sheet_name,
                                            range_name=range_name,
                                            error=str(create_exc)[:300],
                                        )
                                        raise
                                    retry_kwargs = dict(kwargs)
                                    return await tool_to_call.ainvoke(retry_kwargs)

                            retry_kwargs = dict(kwargs)
                            retry_kwargs["range_name"] = cell_range
                            log.warning(
                                "agent_run.sheets_missing_tab_fallback_unqualified",
                                original_range=range_name,
                                retry_range=cell_range,
                            )
                            return await tool_to_call.ainvoke(retry_kwargs)

                        fallback_range = _fallback_unqualified_sheet_range(range_name)
                        if fallback_range and "unable to parse range" in err.lower():
                            retry_kwargs = dict(kwargs)
                            retry_kwargs["range_name"] = fallback_range
                            log.warning(
                                "agent_run.sheets_range_retry_unqualified",
                                original_range=range_name,
                                retry_range=fallback_range,
                            )
                            return await tool_to_call.ainvoke(retry_kwargs)
                        raise

                return _modify_sheet_values_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_modify_sheet_values_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=ModifySheetValuesArgs,
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name == "manage_drive_access":
            def _build_manage_drive_access_guarded(tool_to_call: Any):
                async def _manage_drive_access_guarded(**kwargs):
                    action = str(kwargs.get("action") or "").strip().lower()
                    share_type = str(kwargs.get("share_type") or "").strip().lower()
                    share_with = str(kwargs.get("share_with") or "").strip()
                    if action == "grant" and share_type == "user" and (not share_with or "@" not in share_with):
                        return (
                            "DRIVE_EMAIL_REQUIRED: manage_drive_access untuk share_type='user' hanya menerima "
                            "alamat email Google yang valid. Nomor WhatsApp/telepon tidak dapat diberi permission "
                            "Google Drive. Jangan ubah akses hanya agar agent dapat menulis ke spreadsheet yang "
                            "sudah disediakan; gunakan koneksi Google Owner yang aktif."
                        )
                    return await tool_to_call.ainvoke(kwargs)

                return _manage_drive_access_guarded

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_build_manage_drive_access_guarded(mcp_tool),
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            sanitized_tool_names.append(tool_name)
            continue

        if tool_name != "create_survey_form":
            wrapped_tools.append(mcp_tool)
            continue

        def _build_create_survey_form_guarded(tool_to_call: Any):
            async def _create_survey_form_guarded(**kwargs):
                original_questions = kwargs.get("questions")
                if _needs_generated_form_questions(original_questions):
                    kwargs["questions"] = build_default_form_questions(
                        title=str(kwargs.get("title") or ""),
                        description=str(kwargs.get("description") or ""),
                        topic_hint=str(kwargs.get("topic_hint") or ""),
                    )
                    log.warning(
                        "agent_run.forms_questions_autofilled",
                        tool="create_survey_form",
                        original_questions=original_questions,
                        generated=len(kwargs["questions"]),
                    )
                return await tool_to_call.ainvoke(kwargs)

            return _create_survey_form_guarded

        wrapped_tools.append(
            StructuredTool.from_function(
                coroutine=_build_create_survey_form_guarded(mcp_tool),
                name=mcp_tool.name,
                description=getattr(mcp_tool, "description", None),
                args_schema=getattr(mcp_tool, "args_schema", None),
            )
        )
        sanitized_tool_names.append(tool_name)
    if sanitized_tool_names:
        log.warning(
            "agent_run.google_mcp_tools_sanitized",
            tools=sanitized_tool_names,
            total=len(sanitized_tool_names),
        )
    return wrapped_tools


def _normalize_create_shape_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(kwargs)
    payload = normalized.get("shape_type")
    if isinstance(payload, str):
        normalized["shape_type"] = _normalize_slides_shape_type_value(payload)

    payload = normalized.get("shapeType")
    if isinstance(payload, str):
        normalized["shapeType"] = _normalize_slides_shape_type_value(payload)

    element_properties = normalized.get("elementProperties")
    if isinstance(element_properties, dict):
        _normalize_slides_element_properties(element_properties)

    return normalized


def _normalize_slides_shape_type_value(shape_type: str) -> str:
    normalized = shape_type.strip().upper()
    if normalized in _SLIDES_VALID_SHAPE_TYPES:
        return normalized
    if any(marker in normalized for marker in ("TITLE", "BODY", "SUBTITLE", "PLACEHOLDER", "TEXT")):
        return "TEXT_BOX"
    return normalized


def _needs_generated_form_questions(questions: Any) -> bool:
    if not isinstance(questions, list) or not questions:
        return False

    meaningful = 0
    blank_or_placeholder = 0
    for question in questions:
        if not isinstance(question, dict) or not question:
            blank_or_placeholder += 1
            continue
        title = str(question.get("title") or "").strip()
        if not title or _is_placeholder_question_title(title):
            blank_or_placeholder += 1
            continue
        meaningful += 1

    return meaningful < 3 or blank_or_placeholder > 0


def _is_placeholder_question_title(title: str) -> bool:
    normalized = title.strip().lower()
    return bool(re.fullmatch(r"(pertanyaan|question)\s*\d+", normalized))


def build_default_form_questions(
    *,
    title: str,
    description: str = "",
    topic_hint: str = "",
) -> list[dict[str, Any]]:
    topic = _derive_form_topic(title=title, description=description, topic_hint=topic_hint)
    return [
        {
            "title": "Nama atau inisial responden",
            "type": "short_answer",
            "required": False,
        },
        {
            "title": f"Apakah Anda pernah melihat atau terlibat langsung dalam {topic}?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Ya, pernah langsung", "Pernah melihat dari jauh/media", "Tidak pernah"],
        },
        {
            "title": f"Seberapa sering {topic} terjadi dalam pengalaman atau pengamatan Anda?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Sangat sering", "Cukup sering", "Jarang", "Tidak pernah"],
        },
        {
            "title": f"Menurut Anda, seberapa efektif {topic} dalam menarik perhatian publik?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Sangat efektif", "Cukup efektif", "Kurang efektif", "Tidak efektif"],
        },
        {
            "title": f"Apa dampak positif yang Anda lihat dari {topic}?",
            "type": "paragraph",
            "required": False,
        },
        {
            "title": f"Apa risiko atau dampak negatif yang muncul dari {topic}?",
            "type": "paragraph",
            "required": False,
        },
        {
            "title": f"Bagaimana tanggapan masyarakat sekitar terhadap {topic}?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Mendukung", "Netral", "Kurang mendukung", "Menolak", "Tidak tahu"],
        },
        {
            "title": f"Apa saran Anda agar kegiatan terkait {topic} lebih aman dan tetap efektif?",
            "type": "paragraph",
            "required": False,
        },
    ]


def _derive_form_topic(*, title: str, description: str, topic_hint: str) -> str:
    for raw in (topic_hint, title, description):
        text = str(raw or "").strip()
        if text:
            return text
    return "topik survei ini"


async def prepare_google_mcp_runtime(
    *,
    tools_config: dict[str, Any],
    tools: list,
    active_groups: list[str],
    session: Any,
    agent_id: uuid.UUID,
    memory_scope: str | None,
    api_key: str,
    user_message: str,
    system_prompt: Any,
    log: Any,
    fallback_external_user_id: str | None = None,
) -> GoogleMcpRuntime:
    mcp_cfg = tools_config.get("mcp", {})
    mcp_enabled = False
    workspace_server = None
    if isinstance(mcp_cfg, dict):
        has_wrapper = "enabled" in mcp_cfg or "servers" in mcp_cfg
        if has_wrapper:
            mcp_enabled = bool(mcp_cfg.get("enabled", bool(mcp_cfg.get("servers"))))
            servers = mcp_cfg.get("servers", {}) if isinstance(mcp_cfg.get("servers", {}), dict) else {}
            workspace_server = servers.get("google_workspace")
        else:
            workspace_server = mcp_cfg.get("google_workspace") if isinstance(mcp_cfg.get("google_workspace"), dict) else None
            mcp_enabled = bool(workspace_server)

    from app.config import get_settings

    integration_url = _google_integration_runtime_url(
        str(get_settings().google_integration_service_url).rstrip("/")
    )
    channel_cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    candidate_ids = _candidate_external_user_ids(
        memory_scope or getattr(session, "external_user_id", None) or fallback_external_user_id,
        channel_cfg.get("user_phone") or fallback_external_user_id,
    )
    # Operational customer workflows may use one explicitly delegated,
    # resource-bound tool. In that case authentication still belongs to the
    # Owner, never to the customer identity.
    for owner_candidate in _candidate_external_user_ids(
        fallback_external_user_id,
        fallback_external_user_id,
    ):
        if owner_candidate not in candidate_ids:
            candidate_ids.append(owner_candidate)

    connected_user_id: str | None = None
    auth_url: str | None = None
    preflight_error: str | None = None

    if mcp_enabled and workspace_server and not integration_url:
        preflight_error = (
            "GOOGLE_INTEGRATION_SERVICE_URL belum dikonfigurasi; "
            "auth Google Workspace harus memakai URL dev tunnel, bukan localhost."
        )
        log.warning("agent_run.google_mcp_integration_url_missing")

    if mcp_enabled and workspace_server and integration_url:
        try:
            import httpx as _httpx

            jwt = None
            jwt_external_user_id = None
            async with _httpx.AsyncClient(timeout=5.0) as http_client:
                for candidate in candidate_ids:
                    status_payload: dict[str, Any] | None = None
                    for agent_param in (str(agent_id), None):
                        params = {"external_user_id": candidate}
                        if agent_param:
                            params["agent_id"] = agent_param

                        status_resp = await http_client.get(
                            f"{integration_url}/v1/integrations/google/status",
                            params=params,
                            headers={"X-API-Key": api_key},
                        )
                        if status_resp.status_code == 200:
                            status_payload = status_resp.json() if status_resp.text else {}
                            if bool(status_payload.get("connected")):
                                connected_user_id = candidate
                                break

                    if not status_payload or not bool(status_payload.get("connected")):
                        connect_resp = await http_client.post(
                            f"{integration_url}/v1/integrations/google/connect",
                            json={"external_user_id": candidate, "agent_id": str(agent_id)},
                            headers={"X-API-Key": api_key},
                        )
                        if connect_resp.status_code == 200:
                            connect_data = connect_resp.json() if connect_resp.text else {}
                            auth_url = connect_data.get("auth_url") or connect_data.get("authorization_url")
                        preflight_error = "Google Workspace belum terhubung atau token sudah expired"
                        if connected_user_id is None:
                            connected_user_id = candidate
                        continue

                    for agent_param in (str(agent_id), None):
                        params = {"external_user_id": candidate}
                        if agent_param:
                            params["agent_id"] = agent_param
                        resp = await http_client.get(
                            f"{integration_url}/v1/integrations/google/token",
                            params=params,
                            headers={"X-API-Key": api_key},
                        )
                        if resp.status_code == 200:
                            jwt = resp.json().get("bearer_token")
                            jwt_external_user_id = candidate
                            break
                    if jwt:
                        break

                    connect_resp = await http_client.post(
                        f"{integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": candidate, "agent_id": str(agent_id)},
                        headers={"X-API-Key": api_key},
                    )
                    if connect_resp.status_code == 200:
                        connect_data = connect_resp.json() if connect_resp.text else {}
                        auth_url = connect_data.get("auth_url") or connect_data.get("authorization_url")
                    preflight_error = "Google Workspace belum terhubung atau token sudah expired"

            if jwt:
                workspace_server.setdefault("headers", {})["Authorization"] = f"Bearer {jwt}"
                connected_user_id = jwt_external_user_id
                auth_url = None
                preflight_error = None
                log.info("agent_run.google_mcp_token_injected", external_user_id=jwt_external_user_id)
            elif candidate_ids:
                log.info("agent_run.google_mcp_not_connected", external_user_ids=candidate_ids)
            else:
                log.info("agent_run.google_mcp_missing_external_user_id")
        except Exception as err:
            log.warning("agent_run.google_mcp_token_error", error=str(err))
            if not jwt and not preflight_error:
                preflight_error = "Layanan integrasi Google tidak dapat dihubungi sementara. Coba lagi beberapa saat."

    if mcp_enabled and workspace_server and integration_url and candidate_ids:
        tools.extend(
            _build_google_reauth_tool(
                integration_url=integration_url,
                api_key=api_key,
                agent_id=agent_id,
                candidate_user_ids=candidate_ids,
                preferred_auth_url=auth_url,
            )
        )
        active_groups.append("google_reauth")

    runtime = GoogleMcpRuntime(
        enabled=mcp_enabled,
        workspace_server=workspace_server,
        connected_user_id=connected_user_id,
        auth_url=auth_url,
        preflight_error=preflight_error,
        integration_url=integration_url,
        candidate_user_ids=candidate_ids,
        system_prompt=system_prompt,
    )
    if mcp_enabled and workspace_server and isinstance(system_prompt, str):
        runtime.system_prompt = (
            system_prompt
            + build_google_mcp_usage_notice(user_message)
            + build_google_mcp_runtime_state_notice(runtime)
        )
    return runtime


async def apply_mcp_error_notice(
    *,
    mcp_errors: dict[str, str],
    runtime: GoogleMcpRuntime,
    agent_id: uuid.UUID,
    memory_scope: str | None,
    api_key: str,
    system_prompt: Any,
    log: Any,
) -> tuple[str | None, Any]:
    auth_url = runtime.auth_url
    google_mcp_err = str(mcp_errors.get("google_workspace", ""))
    if google_mcp_err and ("401" in google_mcp_err or "Unauthorized" in google_mcp_err):
        reauth_user = runtime.connected_user_id or memory_scope
        if reauth_user:
            try:
                import httpx as _httpx

                async with _httpx.AsyncClient(timeout=5.0) as http_client:
                    resp = await http_client.post(
                        f"{runtime.integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": reauth_user, "agent_id": str(agent_id)},
                        headers={"X-API-Key": api_key},
                    )
                if resp.status_code == 200:
                    data = resp.json() if resp.text else {}
                    auth_url = data.get("auth_url") or data.get("authorization_url")
            except Exception as err:
                log.warning("agent_run.google_mcp_reauth_link_error", error=str(err))

    if isinstance(system_prompt, str):
        system_prompt += build_mcp_unavailable_notice(mcp_errors, auth_url)
    return auth_url, system_prompt


async def apply_google_mcp_reply_overrides(
    *,
    final_reply: str,
    steps: list,
    mcp_errors: dict[str, str],
    runtime: GoogleMcpRuntime,
    auth_url: str | None,
    llm_raw: ChatOpenAI,
    user_message: str,
    agent_id: uuid.UUID,
    api_key: str,
    log: Any,
) -> tuple[str, list, str | None]:
    google_mcp_err = mcp_errors.get("google_workspace") if isinstance(mcp_errors, dict) else None
    google_mcp_step_err = _extract_google_mcp_step_error(steps)
    google_mcp_auth_err = google_mcp_err or google_mcp_step_err
    google_mcp_has_artifact = _contains_google_workspace_artifact(
        final_reply
    ) or _has_google_workspace_artifact_step(steps)
    must_override_google_auth = (
        bool(google_mcp_auth_err)
        and _is_google_mcp_intent(user_message)
        and _is_google_auth_or_scope_error(str(google_mcp_auth_err))
        and not google_mcp_has_artifact
    )

    if must_override_google_auth:
        if not auth_url:
            auth_url = await _fetch_google_auth_link(
                integration_url=runtime.integration_url,
                api_key=api_key,
                agent_id=agent_id,
                candidate_user_ids=runtime.candidate_user_ids,
            )
        final_reply = await _build_google_mcp_auth_failure_reply(
            llm=llm_raw,
            user_message=user_message,
            error_text=str(google_mcp_auth_err),
            auth_url=auth_url,
        )
        steps = []
        log.warning("agent_run.reply_overridden_mcp_auth_failed", error=str(google_mcp_auth_err)[:200])

    must_override_google_unavailable = (
        bool(google_mcp_err)
        and not must_override_google_auth
        and _is_google_mcp_intent(user_message)
        and not google_mcp_has_artifact
        and (not final_reply or _looks_like_progress_claim(final_reply))
    )
    if must_override_google_unavailable:
        previous_reply = final_reply or ""
        final_reply = _build_google_mcp_unavailable_reply(str(google_mcp_err))
        steps = []
        log.warning(
            "agent_run.reply_overridden_mcp_unavailable",
            error=str(google_mcp_err)[:200],
            previous_reply=previous_reply[:200],
        )

    must_override_google_not_executed = (
        not google_mcp_err
        and not must_override_google_auth
        and _is_google_mcp_intent(user_message)
        and not _has_google_mcp_step(steps)
        and not _contains_google_workspace_artifact(final_reply)
        and not _looks_like_google_auth_recovery_reply(final_reply)
        and (
            _looks_like_progress_claim(final_reply)
            or _looks_like_google_mcp_success_claim(final_reply)
        )
    )
    if must_override_google_not_executed:
        previous_reply = final_reply or ""
        final_reply = _build_google_mcp_not_executed_reply(user_message)
        log.warning(
            "agent_run.reply_overridden_google_mcp_not_executed",
            previous_reply=previous_reply[:200],
        )

    if (
        not must_override_google_not_executed
        and not google_mcp_err
        and not must_override_google_auth
        and auth_url
        and (
            _is_google_mcp_intent(user_message)
            or _looks_like_google_auth_recovery_reply(final_reply)
        )
        and not _has_google_mcp_step(steps)
        and _looks_like_google_auth_recovery_reply(final_reply)
    ):
        updated_reply = _ensure_google_auth_link_in_reply(final_reply, auth_url)
        if updated_reply != final_reply:
            log.warning("agent_run.google_mcp_auth_link_appended_to_recovery_reply")
            final_reply = updated_reply

    return _sanitize_user_facing_google_terms(final_reply), steps, auth_url


async def _build_google_mcp_auth_failure_reply(
    *,
    llm: ChatOpenAI,
    user_message: str,
    error_text: str,
    auth_url: str | None,
) -> str:
    if auth_url:
        return (
            "Google Workspace belum terhubung atau tokennya sudah expired, "
            "jadi saya belum menjalankan request ini.\n\n"
            "Klik link ini untuk reconnect Google:\n"
            f"{auth_url}\n\n"
            "Setelah selesai, balas `sudah` supaya saya lanjutkan."
        )
    return (
        "Google Workspace belum terhubung atau tokennya sudah expired, "
        "jadi saya belum menjalankan request ini. Saya belum berhasil membuat "
        "link reconnect otomatis; silakan reconnect Google dari pengaturan integrasi, "
        "lalu coba lagi."
    )


def _build_google_mcp_unavailable_reply(error_text: str) -> str:
    e = (error_text or "").lower()
    if "504" in e or "timeout" in e or "gateway timeout" in e:
        return (
            "Maaf, aksi Google Workspace belum berhasil dijalankan karena koneksi ke layanan Google sedang timeout. "
            "Jadi presentasi/link belum berhasil dibuat atau diambil. Coba kirim lagi sebentar lagi."
        )
    return (
        "Maaf, aksi Google Workspace belum berhasil dijalankan karena layanan sedang tidak tersedia. "
        "Jadi perubahan atau link belum berhasil dibuat. Coba kirim lagi beberapa saat lagi."
    )

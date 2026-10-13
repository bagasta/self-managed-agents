"""Minimal final-reply fallback.

The former implementation contained legacy Arthur builder heuristics that
rewrote valid model replies.  Arthur V2 owns its own tool contract and wording,
so this module must never reinterpret a non-empty response.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any


class ReplyGuardReason(str, Enum):
    """Machine-readable reason for the only remaining fallback behavior."""

    PASS_THROUGH = "pass_through"
    UNVERIFIED_GOOGLE_DELIVERABLE = "unverified_google_deliverable"
    FALLBACK_VERIFIED_ARTIFACT = "fallback_verified_artifact"
    FALLBACK_EMPTY_REPLY = "fallback_empty_reply"


def _record_guard_reason(
    decision_trace: dict[str, str] | None,
    reason: ReplyGuardReason,
) -> None:
    if decision_trace is not None:
        decision_trace["reason"] = reason.value


def _step_tool_names(steps: list[dict[str, Any]]) -> list[str]:
    return [
        str(step.get("tool") or "").strip()
        for step in steps or []
        if str(step.get("tool") or "").strip()
    ]


_VERIFIED_ARTIFACT_URL_RE = re.compile(
    r"https://(?:docs\.google\.com/(?:document|spreadsheets|presentation|forms)/|drive\.google\.com/(?:file/|drive/))[^\s\"']+",
    re.IGNORECASE,
)
_ARTIFACT_TOOL_MARKERS = (
    "document",
    "drive",
    "form",
    "presentation",
    "sheet",
    "slide",
)
_FAILED_RESULT_MARKERS = (
    "error",
    "failed",
    "forbidden",
    "insufficient authentication scopes",
    "permission denied",
    "traceback",
)


def _verified_artifact_url_from_steps(steps: list[dict[str, Any]]) -> str | None:
    """Return only a Google artifact emitted by a matching artifact tool.

    Search/browse tools frequently return arbitrary URLs.  They are evidence,
    not deliverables, and must never be promoted into a success reply merely
    because the model's final message was empty.
    """
    for step in reversed(steps or []):
        tool_name = str(step.get("tool") or "").strip().casefold()
        result = str(step.get("result") or "").strip()
        if not tool_name or not result:
            continue
        if not any(marker in tool_name for marker in _ARTIFACT_TOOL_MARKERS):
            continue
        lowered_result = result.casefold()
        if any(marker in lowered_result for marker in _FAILED_RESULT_MARKERS):
            continue
        match = _VERIFIED_ARTIFACT_URL_RE.search(result)
        if match:
            return match.group(0).rstrip(".,)")
    return None


def _google_artifact_requirement(message: str) -> tuple[str, tuple[str, ...]] | None:
    text = " ".join(str(message or "").casefold().split())
    requests_write = any(
        marker in text
        for marker in (
            "buat",
            "bikin",
            "create",
            "isi",
            "simpan",
            "tulis",
            "laporan",
            "report",
        )
    )
    if not requests_write:
        return None
    requirements = (
        (("google docs", "google document"), "Google Docs", ("docs.google.com/document/",)),
        (("google sheets", "google sheet", "spreadsheet"), "Google Sheets", ("docs.google.com/spreadsheets/",)),
        (("google slides", "google presentation", "presentasi google"), "Google Slides", ("docs.google.com/presentation/",)),
        (("google forms", "google form"), "Google Forms", ("docs.google.com/forms/",)),
        (("google drive",), "Google Drive", ("drive.google.com/",)),
    )
    for markers, label, url_markers in requirements:
        if any(marker in text for marker in markers):
            return label, url_markers
    return None


def _claims_completed_deliverable(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(
        marker in normalized
        for marker in (
            "proses selesai",
            "sudah selesai",
            "berhasil dibuat",
            "sudah dibuat",
            "cek hasil",
            "link dokumen",
            "dokumennya di sini",
            "laporannya di sini",
        )
    )


def ensure_non_empty_reply(
    reply: str,
    steps: list[dict[str, Any]],
    *,
    tools_config: dict[str, Any] | None = None,
    active_groups: list[str] | tuple[str, ...] | set[str] | None = None,
    user_message: str = "",
    builder_whatsapp_action: str | None = None,
    decision_trace: dict[str, str] | None = None,
    system_plugin: str | None = None,
) -> str:
    """Return a model reply unchanged; only provide text for truly empty output.

    Compatibility parameters are intentionally retained while legacy builder
    reply rewriting has been removed.
    """
    del tools_config, active_groups, builder_whatsapp_action
    text = str(reply or "").strip()
    # A builder must never fabricate/replay an OAuth URL from chat history.
    # The only legitimate source is the current start_assistant_google_oauth
    # tool result, which is recorded in ``steps`` for this run.
    if system_plugin == "arthur_v2" and text:
        tool_names = set(_step_tool_names(steps))
        oauth_url = re.compile(
            r"https://google-workspace-mcp\.chiefaiofficer\.id/v1/integrations/google/start\?t=[^\s)]+",
            re.IGNORECASE,
        )
        if oauth_url.search(text) and "start_assistant_google_oauth" not in tool_names:
            _record_guard_reason(decision_trace, ReplyGuardReason.PASS_THROUGH)
            return (
                "Saya tidak bisa mengirim ulang link OAuth tanpa membuatnya lewat pemeriksaan resmi. "
                "Saya akan cek status koneksi Google agent terlebih dahulu."
            )
        login_claim = re.search(r"\b(sudah|udah|udh|dah|telah)\s+login\b", user_message or "", re.IGNORECASE)
        google_claim = re.search(r"\b(auth_pending|oauth|google\s+(sudah\s+)?(terhubung|belum\s+terhubung))\b", text, re.IGNORECASE)
        if login_claim and google_claim and not ({"inspect_managed_assistant", "start_assistant_google_oauth"} & tool_names):
            _record_guard_reason(decision_trace, ReplyGuardReason.PASS_THROUGH)
            return "Saya belum bisa menyimpulkan status Google tanpa pemeriksaan resmi. Saya perlu cek status koneksi agent terlebih dahulu."
    artifact_requirement = _google_artifact_requirement(user_message)
    if text and artifact_requirement and _claims_completed_deliverable(text):
        artifact_label, expected_url_markers = artifact_requirement
        artifact_url = _verified_artifact_url_from_steps(steps)
        if not artifact_url or not any(
            marker in artifact_url.casefold() for marker in expected_url_markers
        ):
            _record_guard_reason(
                decision_trace,
                ReplyGuardReason.UNVERIFIED_GOOGLE_DELIVERABLE,
            )
            return (
                f"Proses pendukung mungkin sudah berjalan, tetapi {artifact_label} belum berhasil dibuat dan "
                "diverifikasi. Saya tidak akan mengirim link lain sebagai link hasil. "
                "Silakan coba lagi setelah akses Google aktif."
            )
    if text:
        _record_guard_reason(decision_trace, ReplyGuardReason.PASS_THROUGH)
        return text

    artifact_url = _verified_artifact_url_from_steps(steps)
    if artifact_url:
        _record_guard_reason(decision_trace, ReplyGuardReason.FALLBACK_VERIFIED_ARTIFACT)
        return f"Proses selesai. Cek hasilnya di sini: {artifact_url}"

    _record_guard_reason(decision_trace, ReplyGuardReason.FALLBACK_EMPTY_REPLY)
    if _step_tool_names(steps):
        return (
            "Proses belum menghasilkan jawaban akhir yang terverifikasi. "
            "Tidak ada link hasil yang dapat saya pastikan benar; silakan kirim ulang permintaanmu."
        )
    return "Maaf, respons belum tersedia. Coba kirim ulang pesanmu ya."

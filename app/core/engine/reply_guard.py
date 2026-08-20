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
    del tools_config, active_groups, user_message, builder_whatsapp_action, system_plugin
    text = str(reply or "").strip()
    if text:
        _record_guard_reason(decision_trace, ReplyGuardReason.PASS_THROUGH)
        return text

    _record_guard_reason(decision_trace, ReplyGuardReason.FALLBACK_EMPTY_REPLY)
    url_pat = re.compile(r"https://[^\s\"']+")
    for step in reversed(steps or []):
        match = url_pat.search(str(step.get("result") or ""))
        if match:
            return f"Proses selesai. Cek hasilnya di sini: {match.group(0).rstrip('.,)')}"

    if _step_tool_names(steps):
        return "Prosesnya sudah dijalankan, tetapi respons akhir belum tersedia. Kirim pesan lagi untuk melanjutkan."
    return "Maaf, respons belum tersedia. Coba kirim ulang pesanmu ya."

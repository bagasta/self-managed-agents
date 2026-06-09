"""Low-level shared helpers for the agent run loop.

Leaf module — must NOT import from any other `agent_*` engine module so it can
be safely imported by `agent_runner` and the per-domain modules extracted from
it (e.g. `agent_google_routing`) without creating import cycles.
"""
from __future__ import annotations

import json
import re
from typing import Any

_URL_RE = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")


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


def _operator_message_payload(message: str) -> str:
    """Return the actual operator text from WA/API operator envelopes."""
    text = message or ""
    if text.startswith("[OPERATOR] "):
        return text.removeprefix("[OPERATOR] ").strip()
    if text.startswith("<OPERATOR>") or text.startswith("<OWNER>"):
        marker = "\nPesan:"
        idx = text.find(marker)
        if idx != -1:
            return text[idx + len(marker):].strip()
    return text


def _is_operator_envelope(message: str) -> bool:
    text = message or ""
    return text.startswith("[OPERATOR] ") or text.startswith("<OPERATOR>")


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

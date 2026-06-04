"""Low-level shared helpers for the agent run loop.

Leaf module — must NOT import from any other `agent_*` engine module so it can
be safely imported by `agent_runner` and the per-domain modules extracted from
it (e.g. `agent_google_routing`) without creating import cycles.
"""
from __future__ import annotations

import json
from typing import Any


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
    if text.startswith("<OPERATOR>"):
        marker = "\nPesan:"
        idx = text.find(marker)
        if idx != -1:
            return text[idx + len(marker):].strip()
    return text

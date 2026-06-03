"""SOP-based runtime gate: removes final-action tools when agent SOP is not mature."""
from __future__ import annotations

from typing import Any, Iterable

FINAL_ACTION_TOOLS: frozenset[str] = frozenset({
    "send_whatsapp_document",
    "send_whatsapp_image",
})


def is_sop_locked(sop: dict[str, Any] | None) -> bool:
    """Return True when the SOP is not mature enough to allow final-action tools.

    Locked when:
    - sop is None or not a dict
    - owner_review_required is True
    - maturity is draft / needs_review / missing
    """
    if not isinstance(sop, dict):
        return True
    if bool(sop.get("owner_review_required")):
        return True
    return str(sop.get("maturity") or "").lower() in {"draft", "needs_review", "missing"}


def gated_tool_names(tool_names: Iterable[str], *, sop: dict[str, Any] | None) -> set[str]:
    """Return tool names with final-action tools removed when SOP is locked."""
    names = {str(n) for n in tool_names}
    if not is_sop_locked(sop):
        return names
    return names - FINAL_ACTION_TOOLS


def filter_tools_by_sop(
    tools: list,
    *,
    sop: dict[str, Any] | None,
    caps: list[str] | None,
) -> list:
    """Filter tool objects, removing final-action tools when SOP is locked.

    Args:
        tools: list of tool objects with a .name attribute.
        sop: the agent's operating manual dict (or None).
        caps: the agent's capabilities list. Builder/system agents bypass gating.

    Returns:
        Filtered list of tools.
    """
    _caps = caps or []
    if "builder" in _caps or "system" in _caps:
        return tools
    if not is_sop_locked(sop):
        return tools
    return [t for t in tools if getattr(t, "name", None) not in FINAL_ACTION_TOOLS]

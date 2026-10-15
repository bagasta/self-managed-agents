"""Opt-in service controls. Agents without these flags retain their behavior."""
from __future__ import annotations

from app.core.utils.phone_utils import normalize_phone

CONTROL_KEY = "arthur_service_controls"


def controls(agent):
    return (getattr(agent, "tools_config", None) or {}).get(CONTROL_KEY) or {}


def is_agent_paused(agent):
    return controls(agent).get("paused") is True


def service_snapshot(agent):
    """Keep record existence distinct from the service's current run state."""
    deleted = bool(getattr(agent, "is_deleted", False))
    paused = is_agent_paused(agent)
    return {"paused": paused, "service_status": "deleted" if deleted else "paused" if paused else "active"}


def service_block_reason(agent, sender=None, *, whatsapp=False):
    if is_agent_paused(agent):
        return "agent_paused"
    if not whatsapp or not controls(agent).get("managed_access"):
        return None
    sender = normalize_phone(str(sender or ""))
    operators = {normalize_phone(str(p)) for p in (getattr(agent, "operator_ids", None) or [])}
    operators.add(normalize_phone(str(getattr(agent, "owner_external_id", None) or "")))
    escalation = getattr(agent, "escalation_config", None) or {}
    if escalation.get("operator_phone"):
        operators.add(normalize_phone(str(escalation["operator_phone"])))
    operators.discard("")
    if sender and sender in operators:
        return None
    allowed = getattr(agent, "allowed_senders", None)
    if allowed is None:
        return None
    if not sender or sender not in {normalize_phone(str(p)) for p in allowed}:
        return "sender_not_allowed"
    return None

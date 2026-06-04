"""Session and phone identity helpers.

Extracted from agent_runner.py — pure functions that resolve real phone
numbers, operator IDs, and session ownership from Session/Agent models.
"""
from __future__ import annotations

from typing import Any

from app.models.session import Session
from app.core.utils.phone_utils import normalize_phone


def _session_real_phone(session: Session) -> str:
    cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    phone = str(cfg.get("phone_number") or "").strip()
    if phone:
        return phone
    user_phone = str(cfg.get("user_phone") or "").strip()
    if "@lid" not in user_phone.lower():
        return user_phone
    return ""


def _normalized_agent_operator_ids(agent_model: Any) -> set[str]:
    ids: set[str] = set()
    owner = normalize_phone(str(getattr(agent_model, "owner_external_id", "") or ""))
    if owner:
        ids.add(owner)

    escalation_cfg = getattr(agent_model, "escalation_config", None)
    if isinstance(escalation_cfg, dict):
        op_phone = normalize_phone(str(escalation_cfg.get("operator_phone") or ""))
        if op_phone:
            ids.add(op_phone)

    raw_operator_ids = getattr(agent_model, "operator_ids", None)
    if isinstance(raw_operator_ids, list):
        for raw in raw_operator_ids:
            normalized = normalize_phone(str(raw or ""))
            if normalized:
                ids.add(normalized)
    return ids


def _session_sender_phone(session: Session) -> str:
    cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    for key in ("phone_number", "sender_phone", "sender_alt", "user_phone"):
        raw = str(cfg.get(key) or "").strip()
        if raw and "@lid" not in raw.lower():
            normalized = normalize_phone(raw)
            if normalized:
                return normalized
    raw_external = str(getattr(session, "external_user_id", "") or "").strip()
    if raw_external and "@lid" not in raw_external.lower():
        return normalize_phone(raw_external)
    return ""


def _is_customer_whatsapp_session(session: Session, agent_model: Any) -> bool:
    if getattr(session, "channel_type", None) != "whatsapp":
        return False
    sender = _session_sender_phone(session)
    if not sender:
        return False
    return sender not in _normalized_agent_operator_ids(agent_model)


def _owner_notification_target(agent_model: Any) -> str:
    owner = str(getattr(agent_model, "owner_external_id", "") or "").strip()
    if owner:
        return owner

    raw_operator_ids = getattr(agent_model, "operator_ids", None)
    if isinstance(raw_operator_ids, list):
        for raw in raw_operator_ids:
            candidate = str(raw or "").strip()
            if candidate:
                return candidate

    escalation_cfg = getattr(agent_model, "escalation_config", None)
    if isinstance(escalation_cfg, dict):
        return str(escalation_cfg.get("operator_phone") or "").strip()
    return ""

from __future__ import annotations

from typing import Any

from app.core.utils.phone_utils import normalize_phone


def is_probable_whatsapp_lid(value: str | None) -> bool:
    """Detect WhatsApp LID/JID values that should not be used as provisioning IDs."""
    raw = (value or "").strip().lower()
    if not raw:
        return False
    if "@lid" in raw:
        return True
    normalized = normalize_phone(raw)
    return bool(normalized and normalized.isdigit() and len(normalized) > 15)


def resolve_incoming_wa_phone(from_phone: str | None, resolved_phone: str | None) -> str | None:
    """Return a real WA phone when available; reject LID-only identifiers."""
    if resolved_phone:
        normalized = normalize_phone(resolved_phone)
        return normalized or None

    raw = (from_phone or "").strip()
    if not raw or is_probable_whatsapp_lid(raw):
        return None

    normalized = normalize_phone(raw)
    return normalized or None


def resolve_auto_provision_external_id(
    *,
    channel_type: str | None,
    channel_config: dict[str, Any] | None,
    payload_external_user_id: str | None,
    session_external_user_id: str | None,
) -> str | None:
    """
    Resolve the external user id we can safely provision into users table.

    WhatsApp sessions must provide a real phone number via channel_config["phone_number"].
    If we only have a LID/JID-like identity, skip provisioning so we do not
    create the wrong user row.
    """
    cfg = channel_config if isinstance(channel_config, dict) else {}
    phone_number = normalize_phone(str(cfg.get("phone_number") or ""))
    if phone_number and not is_probable_whatsapp_lid(phone_number):
        return phone_number

    if channel_type == "whatsapp":
        return None

    candidate = (payload_external_user_id or session_external_user_id or "").strip()
    if not candidate or is_probable_whatsapp_lid(candidate):
        return None

    normalized = normalize_phone(candidate)
    return normalized or candidate

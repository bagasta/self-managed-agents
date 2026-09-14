"""Short-lived signed callback tokens for Google OAuth completion events."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.config import get_settings

_TTL_SECONDS = 20 * 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_google_oauth_callback_token(*, external_user_id: str, agent_id: str) -> str:
    payload = {"external_user_id": external_user_id, "agent_id": agent_id, "exp": int(time.time()) + _TTL_SECONDS}
    body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(get_settings().api_key.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_encode(signature)}"


def verify_google_oauth_callback_token(token: str, *, external_user_id: str, agent_id: str | None) -> bool:
    try:
        body, supplied_signature = token.split(".", 1)
        expected_signature = _encode(hmac.new(get_settings().api_key.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False
        payload = json.loads(_decode(body))
        return (
            isinstance(payload, dict)
            and int(payload.get("exp", 0)) >= int(time.time())
            and payload.get("external_user_id") == external_user_id
            and payload.get("agent_id") == agent_id
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False

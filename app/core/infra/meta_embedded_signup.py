"""Small, provider-specific adapter for Meta WhatsApp Embedded Signup."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
import uuid

import httpx


def _settings():
    from app.config import get_settings
    return get_settings()


def _require_meta_configuration() -> None:
    settings = _settings()
    missing = [name for name in ("meta_app_id", "meta_app_secret", "meta_embedded_signup_config_id") if not getattr(settings, name)]
    if missing:
        raise RuntimeError("Meta Embedded Signup is not configured: " + ", ".join(missing))


def build_signup_state(agent_id: uuid.UUID | str) -> str:
    """Create a compact, short-lived signed bearer state for an agent.

    WhatsApp clients often make long links awkward to tap, so this intentionally
    encodes only a version byte, expiry, and UUID.  The truncated HMAC is still
    96 bits, which is ample forgery resistance for a short-lived checkout link.
    """
    _require_meta_configuration()
    settings = _settings()
    expires_at = int(time.time()) + settings.meta_signup_state_ttl_seconds
    payload = b"\x01" + struct.pack("!I", expires_at) + uuid.UUID(str(agent_id)).bytes
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    tag = hmac.new(settings.meta_app_secret.encode(), encoded.encode(), hashlib.sha256).digest()[:12]
    signature = base64.urlsafe_b64encode(tag).decode().rstrip("=")
    return f"{encoded}.{signature}"


def verify_signup_state(state: str) -> uuid.UUID:
    try:
        encoded, supplied = state.rsplit(".", 1)
        padded = encoded + "=" * (-len(encoded) % 4)
        raw_payload = base64.urlsafe_b64decode(padded)

        if len(raw_payload) == 21 and raw_payload[0] == 1:
            expected = base64.urlsafe_b64encode(
                hmac.new(_settings().meta_app_secret.encode(), encoded.encode(), hashlib.sha256).digest()[:12]
            ).decode().rstrip("=")
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            expires_at = struct.unpack("!I", raw_payload[1:5])[0]
            if expires_at < time.time():
                raise ValueError("expired")
            return uuid.UUID(bytes=raw_payload[5:])

        # Keep checkout links created before compact states were deployed valid
        # until their normal expiry, rather than breaking an in-progress signup.
        expected = hmac.new(_settings().meta_app_secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature")
        payload = json.loads(raw_payload.decode())
        if int(payload["exp"]) < time.time():
            raise ValueError("expired")
        return uuid.UUID(str(payload["agent_id"]))
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError, struct.error) as exc:
        raise ValueError("Invalid or expired signup state") from exc


def verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    secret = _settings().meta_app_secret
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


async def exchange_code_for_token(code: str) -> str:
    _require_meta_configuration()
    settings = _settings()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/oauth/access_token",
            params={"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code},
        )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise ValueError("Meta did not return an access token")
    return str(token)


async def subscribe_waba_to_webhooks(waba_id: str, access_token: str) -> None:
    settings = _settings()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    response.raise_for_status()


async def get_waba_phone_numbers(waba_id: str, access_token: str) -> list[dict]:
    settings = _settings()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/{waba_id}/phone_numbers",
            params={"fields": "id,display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    response.raise_for_status()
    return list(response.json().get("data") or [])

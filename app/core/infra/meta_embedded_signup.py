"""Server-side helpers for Meta Embedded Signup (WhatsApp Business onboarding).

Embedded Signup allows users to register a WhatsApp Business Account (WABA)
directly from within the Clevio frontend. This module handles:
  - Exchanging the short-lived code from Facebook Login for a System User token
  - Retrieving WABA and phone number information
  - Subscribing the WABA to webhook events
  - Validating webhook verification requests from Meta
"""
from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.facebook.com"
_TOKEN_TIMEOUT = 15
_DEFAULT_TIMEOUT = 10


def _api_version() -> str:
    from app.config import get_settings
    return get_settings().meta_graph_api_version


def build_official_meta_signup_url(agent_id: str) -> str:
    """Build the official Meta Embedded Signup launch URL.

    Points to the dedicated JS SDK launcher endpoint /v1/meta/signup/launch?agent_id=...
    which initializes Meta's Facebook JS SDK and triggers Embedded Signup natively.
    """
    from app.config import get_settings
    settings = get_settings()

    base_url = settings.app_public_url.rstrip("/")
    return f"{base_url}/v1/meta/signup/launch?agent_id={agent_id}"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

async def exchange_code_for_token(code: str) -> dict:
    """Exchange the short-lived code from Facebook Login for a long-lived token.

    The frontend receives a `code` after the user completes the Embedded Signup
    dialog. We exchange it here for a System User Access Token that can be used
    to call the WhatsApp Cloud API on behalf of the user's WABA.

    Returns dict with at least: access_token, token_type, expires_in
    """
    from app.config import get_settings
    settings = get_settings()

    url = f"{_GRAPH_BASE}/{_api_version()}/oauth/access_token"
    params = {
        "client_id": settings.meta_app_id,
        "client_secret": settings.meta_app_secret,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"Token exchange failed: {data}")
    logger.info("meta_signup.token_exchanged", token_type=data.get("token_type"))
    return data


# ---------------------------------------------------------------------------
# WABA info retrieval
# ---------------------------------------------------------------------------

async def get_shared_waba_id(access_token: str) -> str | None:
    """After Embedded Signup, the token grants access to the user's WABA.

    Use the debug_token endpoint or the /me/businesses endpoint to find the
    WABA ID. In practice, the frontend usually passes this directly from the
    onFinished callback of the Embedded Signup JS SDK.
    """
    url = f"{_GRAPH_BASE}/{_api_version()}/debug_token"
    params = {"input_token": access_token}
    from app.config import get_settings
    settings = get_settings()
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            url, params=params,
            headers={"Authorization": f"Bearer {settings.meta_app_secret}"},
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            # The granular_scopes may contain the WABA ID
            scopes = data.get("granular_scopes", [])
            for scope in scopes:
                if scope.get("permission") == "whatsapp_business_messaging":
                    target_ids = scope.get("target_ids", [])
                    if target_ids:
                        return str(target_ids[0])
    return None


async def get_waba_phone_numbers(waba_id: str, access_token: str) -> list[dict]:
    """List phone numbers registered under a WABA."""
    url = f"{_GRAPH_BASE}/{_api_version()}/{waba_id}/phone_numbers"
    params = {"fields": "verified_name,code_verification_status,display_phone_number,quality_rating,id"}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            url, params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def get_waba_info(waba_id: str, access_token: str) -> dict:
    """Get WABA details including name, timezone, etc."""
    url = f"{_GRAPH_BASE}/{_api_version()}/{waba_id}"
    params = {"fields": "name,timezone_id,message_template_namespace,account_review_status"}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            url, params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Webhook subscription
# ---------------------------------------------------------------------------

async def subscribe_waba_to_webhooks(waba_id: str, access_token: str) -> dict:
    """Subscribe the WABA to receive webhook events (messages, statuses, etc.)."""
    url = f"{_GRAPH_BASE}/{_api_version()}/{waba_id}/subscribed_apps"
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    logger.info("meta_signup.webhook_subscribed", waba_id=waba_id, success=data.get("success"))
    return data


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------

def validate_webhook_verification(
    hub_mode: str | None,
    hub_verify_token: str | None,
    hub_challenge: str | None,
) -> str | None:
    """Validate a Meta webhook verification request.

    Returns the hub_challenge string if valid, None otherwise.
    Meta sends GET requests with hub.mode=subscribe, hub.verify_token, and
    hub.challenge. We must respond with the challenge value if the token matches.
    """
    from app.config import get_settings
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.meta_webhook_verify_token:
        return hub_challenge
    return None

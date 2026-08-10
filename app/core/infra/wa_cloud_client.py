"""HTTP client for the Meta WhatsApp Cloud API (graph.facebook.com).

This module replaces the Go wa-service for agents using the official Cloud API.
It communicates directly with Meta's Graph API to send/receive messages.
"""
from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)

_SEND_TIMEOUT = 30
_DEFAULT_TIMEOUT = 10


def _graph_url(version: str | None = None) -> str:
    from app.config import get_settings
    v = version or get_settings().meta_graph_api_version
    return f"https://graph.facebook.com/{v}"


# ---------------------------------------------------------------------------
# Sending messages
# ---------------------------------------------------------------------------

async def send_text_message(
    phone_number_id: str,
    to: str,
    text: str,
    access_token: str,
) -> dict:
    """Send a plain text WhatsApp message via Cloud API."""
    url = f"{_graph_url()}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    message_id = ""
    if "messages" in data and data["messages"]:
        message_id = data["messages"][0].get("id", "")
    logger.info(
        "wa_cloud.send_text",
        phone_number_id=phone_number_id,
        to=to,
        message_id=message_id,
    )
    return {"status": "sent", "message_id": message_id}


async def send_image_message(
    phone_number_id: str,
    to: str,
    image_url: str,
    caption: str,
    access_token: str,
) -> dict:
    """Send an image message via Cloud API using a public URL."""
    url = f"{_graph_url()}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
        resp = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    message_id = ""
    if "messages" in data and data["messages"]:
        message_id = data["messages"][0].get("id", "")
    return {"status": "sent", "message_id": message_id}


async def send_document_message(
    phone_number_id: str,
    to: str,
    document_url: str,
    filename: str,
    caption: str,
    access_token: str,
) -> dict:
    """Send a document message via Cloud API using a public URL."""
    url = f"{_graph_url()}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
            "caption": caption,
        },
    }
    async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
        resp = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    message_id = ""
    if "messages" in data and data["messages"]:
        message_id = data["messages"][0].get("id", "")
    return {"status": "sent", "message_id": message_id}


async def send_template_message(
    phone_number_id: str,
    to: str,
    template_name: str,
    language_code: str,
    access_token: str,
    components: list[dict] | None = None,
) -> dict:
    """Send a pre-approved template message (required outside 24h window)."""
    url = f"{_graph_url()}/{phone_number_id}/messages"
    template: dict = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template["components"] = components
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }
    async with httpx.AsyncClient(timeout=_SEND_TIMEOUT) as client:
        resp = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
    message_id = ""
    if "messages" in data and data["messages"]:
        message_id = data["messages"][0].get("id", "")
    return {"status": "sent", "message_id": message_id}


async def mark_message_read(
    phone_number_id: str,
    message_id: str,
    access_token: str,
) -> None:
    """Mark an incoming message as read (blue ticks)."""
    url = f"{_graph_url()}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("wa_cloud.mark_read_failed", error=str(exc)[:200])


# ---------------------------------------------------------------------------
# Phone number & business profile
# ---------------------------------------------------------------------------

async def get_phone_number_info(
    phone_number_id: str,
    access_token: str,
) -> dict:
    """Get phone number registration status and display info."""
    url = f"{_graph_url()}/{phone_number_id}"
    params = {"fields": "verified_name,code_verification_status,display_phone_number,quality_rating,platform_type"}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            url, params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_business_profile(
    phone_number_id: str,
    access_token: str,
) -> dict:
    """Get the WhatsApp Business Profile for a phone number."""
    url = f"{_graph_url()}/{phone_number_id}/whatsapp_business_profile"
    params = {"fields": "about,address,description,email,profile_picture_url,websites,vertical"}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            url, params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def register_phone_number(
    phone_number_id: str,
    access_token: str,
    pin: str = "000000",
) -> dict:
    """Register (or re-register) a phone number for Cloud API messaging."""
    url = f"{_graph_url()}/{phone_number_id}/register"
    payload = {"messaging_product": "whatsapp", "pin": pin}
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Media download (Cloud API returns media IDs, not base64)
# ---------------------------------------------------------------------------

async def download_media(
    media_id: str,
    access_token: str,
) -> tuple[bytes, str]:
    """Download media by its Cloud API media ID. Returns (bytes, mime_type)."""
    # Step 1: Get the media URL
    url = f"{_graph_url()}/{media_id}"
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        media_info = resp.json()

    media_url = media_info.get("url", "")
    mime_type = media_info.get("mime_type", "application/octet-stream")
    if not media_url:
        raise ValueError(f"No URL returned for media {media_id}")

    # Step 2: Download the actual media bytes
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            media_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.content, mime_type

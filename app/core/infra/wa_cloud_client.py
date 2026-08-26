"""Outbound messages through Meta's official WhatsApp Cloud API."""
from __future__ import annotations

import httpx


async def register_phone_number(phone_number_id: str, access_token: str, pin: str) -> None:
    """Register a selected Cloud API phone number with Meta.

    ``pin`` is the operator's six-digit WhatsApp two-step verification PIN.
    It is deliberately caller-supplied and never persisted or logged.
    """
    from app.config import get_settings

    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/{phone_number_id}/register",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"messaging_product": "whatsapp", "pin": pin},
        )
    response.raise_for_status()


async def send_text_message(phone_number_id: str, to: str, text: str, access_token: str) -> dict:
    from app.config import get_settings
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}},
        )
    response.raise_for_status()
    return response.json()


async def mark_message_read(phone_number_id: str, message_id: str, access_token: str) -> None:
    from app.config import get_settings
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
        )
    response.raise_for_status()

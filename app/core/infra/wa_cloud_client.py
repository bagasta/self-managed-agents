"""Outbound messages through Meta's official WhatsApp Cloud API."""
from __future__ import annotations

import httpx


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

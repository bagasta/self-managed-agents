"""Outbound messages through Meta's official WhatsApp Cloud API."""
from __future__ import annotations

import httpx


async def download_media(media_id: str, access_token: str, *, max_bytes: int = 16 * 1024 * 1024) -> tuple[bytes, str]:
    """Download one inbound Cloud API media object without persisting its URL.

    Meta first returns a short-lived download URL. Both calls require the
    customer's business token. The byte cap protects the webhook worker from
    unexpectedly large uploads.
    """
    from app.config import get_settings

    settings = get_settings()
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        metadata = await client.get(
            f"https://graph.facebook.com/{settings.meta_graph_api_version}/{media_id}",
            headers=headers,
        )
        metadata.raise_for_status()
        details = metadata.json()
        size = int(details.get("file_size") or 0)
        if size > max_bytes:
            raise ValueError(f"Media terlalu besar untuk diproses ({size} bytes; maksimum {max_bytes} bytes)")
        download_url = str(details.get("url") or "")
        if not download_url:
            raise ValueError("Meta tidak mengembalikan URL media")
        mime_type = str(details.get("mime_type") or "application/octet-stream")
        content = bytearray()
        async with client.stream("GET", download_url, headers=headers) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length") or 0)
            if content_length > max_bytes:
                raise ValueError(f"Media terlalu besar untuk diproses ({content_length} bytes; maksimum {max_bytes} bytes)")
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError(f"Media terlalu besar untuk diproses (maksimum {max_bytes} bytes)")
    return bytes(content), mime_type


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

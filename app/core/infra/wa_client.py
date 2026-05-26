"""HTTP client for the wa-service (Go WhatsApp microservice)."""
from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger(__name__)

_WA_TIMEOUT_CREATE = 35  # longer: waits up to 30s for first QR code
_WA_TIMEOUT_DEFAULT = 10


def _base_url() -> str:
    from app.config import get_settings
    return get_settings().wa_service_url.rstrip("/")


def _wa_dev_base_url() -> str:
    from app.config import get_settings
    return get_settings().wa_dev_service_url.rstrip("/")


async def create_wa_device(device_id: str) -> dict:
    """Call Go service to initialise a new WhatsApp device. Returns {qr_image, status}."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_CREATE) as client:
        resp = await client.post(
            f"{_base_url()}/devices",
            json={"device_id": device_id},
        )
        resp.raise_for_status()
        return resp.json()


async def get_wa_qr(device_id: str) -> dict:
    """Get the latest QR code for a device."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.get(f"{_base_url()}/devices/{device_id}/qr")
        resp.raise_for_status()
        return resp.json()


async def refresh_wa_qr(device_id: str) -> dict:
    """Force a fresh QR scan — disconnects session and returns a new QR image."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_CREATE) as client:
        resp = await client.post(f"{_base_url()}/devices/{device_id}/qr")
        resp.raise_for_status()
        return resp.json()


async def get_wa_status(device_id: str) -> dict:
    """Get connection status for a device."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.get(f"{_base_url()}/devices/{device_id}/status")
        resp.raise_for_status()
        return resp.json()


async def send_wa_message(device_id: str, to: str, text: str) -> dict:
    """Send a WhatsApp text message via Go service."""
    if device_id.startswith("wadev_"):
        async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
            resp = await client.post(
                f"{_wa_dev_base_url()}/send/text",
                json={"to": to, "text": text},
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {"status": "sent"}
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.post(
            f"{_base_url()}/devices/{device_id}/send",
            json={"to": to, "message": text},
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "sent"}


async def get_wa_dev_status() -> dict:
    """Return shared wa-dev-service connection status."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.get(f"{_wa_dev_base_url()}/status")
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def send_wa_dev_contact(to: str, display_name: str, phone: str) -> dict:
    """Send the shared wa-dev-service number as a WhatsApp contact card."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.post(
            f"{_wa_dev_base_url()}/send/contact",
            json={"to": to, "display_name": display_name, "phone": phone},
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "sent"}


async def send_wa_contact(device_id: str, to: str, display_name: str, phone: str) -> dict:
    """Send a WhatsApp contact card from the specified device."""
    if device_id.startswith("wadev_"):
        return await send_wa_dev_contact(to, display_name, phone)
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.post(
            f"{_base_url()}/devices/{device_id}/send-contact",
            json={"to": to, "display_name": display_name, "phone": phone},
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "sent"}


async def start_wa_typing(device_id: str, to: str) -> None:
    """Start or refresh the WhatsApp typing keep-alive for a chat."""
    if device_id.startswith("wadev_"):
        return
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.post(
            f"{_base_url()}/devices/{device_id}/typing/start",
            json={"to": to},
        )
        resp.raise_for_status()


async def stop_wa_typing(device_id: str, to: str) -> None:
    """Stop the WhatsApp typing keep-alive for a chat."""
    if device_id.startswith("wadev_"):
        return
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.post(
            f"{_base_url()}/devices/{device_id}/typing/stop",
            json={"to": to},
        )
        resp.raise_for_status()


async def send_wa_image(
    device_id: str,
    to: str,
    image_base64: str,
    caption: str = "",
    mimetype: str = "image/jpeg",
) -> dict:
    """Send a WhatsApp image message via Go service. image_base64 is raw base64-encoded image bytes."""
    if device_id.startswith("wadev_"):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_wa_dev_base_url()}/send/image",
                json={"to": to, "image": image_base64, "caption": caption, "mimetype": mimetype},
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {"status": "sent"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_base_url()}/devices/{device_id}/send-image",
            json={"to": to, "image_base64": image_base64, "caption": caption, "mimetype": mimetype},
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "sent"}


async def send_wa_document(
    device_id: str,
    to: str,
    document_base64: str,
    filename: str = "file",
    caption: str = "",
    mimetype: str = "application/octet-stream",
) -> dict:
    """Send a WhatsApp document message via Go service. document_base64 is raw base64-encoded file bytes."""
    if device_id.startswith("wadev_"):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{_wa_dev_base_url()}/send/document",
                json={
                    "to": to,
                    "data": document_base64,
                    "filename": filename,
                    "caption": caption,
                    "mimetype": mimetype,
                },
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {"status": "sent"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{_base_url()}/devices/{device_id}/send-document",
            json={
                "to": to,
                "document_base64": document_base64,
                "filename": filename,
                "caption": caption,
                "mimetype": mimetype,
            },
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "sent"}


async def delete_wa_device(device_id: str) -> None:
    """Logout and delete a WhatsApp device."""
    async with httpx.AsyncClient(timeout=_WA_TIMEOUT_DEFAULT) as client:
        resp = await client.delete(f"{_base_url()}/devices/{device_id}")
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()


async def resolve_wa_phones(device_id: str, phones: list[str]) -> dict[str, str]:
    """Resolve phone numbers to their actual WA JIDs via Go IsOnWhatsApp API.

    Returns dict: normalized_phone -> JID string (e.g. "6282xxx" -> "9876@lid" or "6282xxx@s.whatsapp.net").
    Falls back to empty dict on error (caller should treat unresolved phones as @s.whatsapp.net).
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if device_id.startswith("wadev_"):
                # wa-dev-service uses a shared WA connection; resolve via its own endpoint
                url = f"{_wa_dev_base_url()}/resolve-phones"
            else:
                url = f"{_base_url()}/devices/{device_id}/resolve-phones"
            resp = await client.post(url, json={"phones": phones})
            if resp.status_code == 200:
                return resp.json().get("resolved", {})
    except Exception:
        pass
    return {}

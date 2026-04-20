"""
Channel Service — kirim pesan keluar ke berbagai channel.

Mendukung:
  - whatsapp  : WhatsApp Business API (Meta Cloud API)
  - telegram  : Telegram Bot API
  - slack     : Slack Incoming Webhook
  - webhook   : Generic HTTP POST
  - in-app    : Tidak kirim ke mana-mana, pesan hanya tersimpan di DB

Credential (api_key, bot_token, dll) disimpan ter-enkripsi di
session.channel_config. Gunakan encrypt_value / decrypt_value untuk
read/write field sensitif sebelum simpan ke DB.

Enkripsi: Fernet (AES-128-CBC + HMAC-SHA256), key dari env CHANNEL_SECRET_KEY.
Jika CHANNEL_SECRET_KEY tidak di-set, enkripsi di-skip (dev mode).
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Enkripsi / Dekripsi credential
# ---------------------------------------------------------------------------

def _get_fernet():
    """Return a Fernet instance if CHANNEL_SECRET_KEY is set, else None."""
    key = os.getenv("CHANNEL_SECRET_KEY", "")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        # Key harus 32 bytes URL-safe base64; jika user set raw string, derive-kan
        raw = key.encode()
        if len(raw) != 44:
            # Pad/trim ke 32 bytes lalu encode ulang
            raw = (raw * 32)[:32]
            key_bytes = base64.urlsafe_b64encode(raw)
        else:
            key_bytes = raw
        return Fernet(key_bytes)
    except Exception as exc:
        logger.warning("channel_service.fernet_init_failed", error=str(exc))
        return None


def encrypt_value(plaintext: str) -> str:
    """Enkripsi string. Return ciphertext (prefixed 'enc:') atau plaintext jika no key."""
    f = _get_fernet()
    if f is None:
        return plaintext
    return "enc:" + f.encrypt(plaintext.encode()).decode()


def decrypt_value(value: str) -> str:
    """Dekripsi string yang dienkripsi encrypt_value. Return plaintext."""
    if not value.startswith("enc:"):
        return value
    f = _get_fernet()
    if f is None:
        return value[4:]
    return f.decrypt(value[4:].encode()).decode()


def encrypt_channel_config(config: dict) -> dict:
    """Enkripsi field sensitif (api_key, token, secret) dalam channel_config."""
    sensitive = {"api_key", "token", "bot_token", "secret", "password"}
    return {
        k: encrypt_value(v) if k in sensitive and isinstance(v, str) else v
        for k, v in config.items()
    }


def decrypt_channel_config(config: dict) -> dict:
    """Dekripsi field sensitif dalam channel_config untuk dipakai."""
    sensitive = {"api_key", "token", "bot_token", "secret", "password"}
    return {
        k: decrypt_value(v) if k in sensitive and isinstance(v, str) else v
        for k, v in config.items()
    }


# ---------------------------------------------------------------------------
# Channel Adapters
# ---------------------------------------------------------------------------

async def _send_whatsapp(to_phone: str, text: str, config: dict) -> None:
    """
    Kirim pesan via WhatsApp Business API (Meta Cloud API).
    config harus punya: api_key (Bearer token), phone_number_id
    """
    api_key = config.get("api_key", "")
    phone_number_id = config.get("phone_number_id", "")
    if not api_key or not phone_number_id:
        logger.warning(
            "channel_service.whatsapp.missing_config — dev mode, message logged only",
            to=to_phone, text_preview=text[:80],
        )
        return

    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        logger.error("channel_service.whatsapp.send_failed", status=resp.status_code, body=resp.text)
    else:
        logger.info("channel_service.whatsapp.sent", to=to_phone)


async def _send_telegram(to_chat_id: str, text: str, config: dict) -> None:
    """
    config harus punya: bot_token
    """
    bot_token = config.get("bot_token", "")
    if not bot_token:
        logger.warning("channel_service.telegram.missing_bot_token — dev mode", to=to_chat_id, text_preview=text[:80])
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={"chat_id": to_chat_id, "text": text})
    if resp.status_code >= 400:
        logger.error("channel_service.telegram.send_failed", status=resp.status_code)
    else:
        logger.info("channel_service.telegram.sent", chat_id=to_chat_id)


async def _send_slack(text: str, config: dict) -> None:
    """
    config harus punya: webhook_url (Slack Incoming Webhook URL)
    """
    webhook_url = config.get("webhook_url", "")
    if not webhook_url:
        logger.error("channel_service.slack.missing_webhook_url")
        return

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json={"text": text})
    if resp.status_code >= 400:
        logger.error("channel_service.slack.send_failed", status=resp.status_code)
    else:
        logger.info("channel_service.slack.sent")


async def _send_webhook(text: str, config: dict, extra: dict | None = None) -> None:
    """
    Generic HTTP POST. config harus punya: url
    Optional: headers (dict), extra context dikirim sebagai JSON body.
    """
    url = config.get("url", "")
    if not url:
        logger.error("channel_service.webhook.missing_url")
        return

    headers = config.get("headers", {})
    body: dict[str, Any] = {"message": text}
    if extra:
        body.update(extra)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        logger.error("channel_service.webhook.send_failed", status=resp.status_code)
    else:
        logger.info("channel_service.webhook.sent", url=url)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_message(
    channel_type: str,
    channel_config: dict,
    text: str,
    to_override: str | None = None,
) -> None:
    """
    Kirim pesan ke channel yang sesuai.

    Args:
        channel_type : "whatsapp" | "telegram" | "slack" | "webhook" | "in-app"
        channel_config: config dari session (sudah di-decrypt sebelum dipanggil)
        text         : isi pesan
        to_override  : override nomor tujuan (untuk send_to_number dari operator)
    """
    cfg = decrypt_channel_config(channel_config)
    to = to_override or cfg.get("user_phone") or cfg.get("chat_id") or ""

    if channel_type == "whatsapp":
        await _send_whatsapp(to, text, cfg)
    elif channel_type == "telegram":
        await _send_telegram(to, text, cfg)
    elif channel_type == "slack":
        await _send_slack(text, cfg)
    elif channel_type == "webhook":
        await _send_webhook(text, cfg, extra={"to": to} if to else None)
    elif channel_type == "in-app":
        # Pesan in-app cukup tersimpan di DB message oleh agent_runner
        logger.info("channel_service.in_app.noop", text_len=len(text))
    else:
        logger.warning("channel_service.unknown_channel_type", channel_type=channel_type)

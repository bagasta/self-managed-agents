"""Outbound WhatsApp abuse guards for direct agent-initiated sends."""
from __future__ import annotations

import re
import time
from typing import Any

import structlog

from app.core.engine.agent_step_utils import _operator_message_payload
from app.core.infra.redis_client import get_redis
from app.core.utils.phone_utils import normalize_phone

log = structlog.get_logger(__name__)

_mem_outbound_windows: dict[str, list[float]] = {}

WA_OUTBOUND_DIRECT_LIMIT = 1
WA_OUTBOUND_DIRECT_WINDOW_SECONDS = 300


def normalize_wa_outbound_target(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.split("@")[0]
    return normalize_phone(raw) or raw.lower()


def normalize_wa_outbound_source(device_id: str | None) -> str:
    raw = str(device_id or "").strip().lower()
    if not raw:
        return "unknown"
    if raw == "wa-dev-service" or raw.startswith("wadev_"):
        return "wadev_shared"
    return raw


def looks_like_outbound_wa_spam_request(text: Any) -> bool:
    """Detect user instructions that try to use the agent as a WA spam sender."""
    payload = _operator_message_payload(str(text or "")).lower()
    if not payload:
        return False
    has_send_topic = any(marker in payload for marker in ("kirim", "send", "wa", "whatsapp", "pesan", "spam"))
    if not has_send_topic:
        return False
    spam_markers = (
        "spam",
        "spamming",
        "flood",
        "bombardir",
        "bom wa",
        "bom pesan",
        "berulang",
        "berkali",
        "berkali-kali",
        "terus menerus",
        "terus-menerus",
        "banyak pesan",
        "banyak kali",
        "sebanyak mungkin",
        "nonstop",
    )
    if any(marker in payload for marker in spam_markers):
        return True
    if re.search(r"\b(?:puluhan|ratusan|ribuan)\s+(?:kali|pesan|message|wa)\b", payload):
        return True
    if re.search(r"\b\d{1,4}\s*(?:x|kali|pesan|message|wa|whatsapp)\b", payload):
        return True
    return False


def wa_outbound_block_reply(reason: str = "rate_limit") -> str:
    if reason == "spam_request":
        return (
            "Saya tidak bisa membantu mengirim pesan WhatsApp berulang/spam ke satu nomor. "
            "Kalau perlu, saya hanya bisa bantu susun satu pesan yang wajar."
        )
    return (
        "Pengiriman WhatsApp ke nomor itu saya batasi sementara untuk mencegah spam. "
        "Coba lagi nanti atau kirim satu pesan yang memang diperlukan."
    )


async def check_wa_outbound_direct_window(
    *,
    device_id: str | None,
    target: str | None,
    limit: int = WA_OUTBOUND_DIRECT_LIMIT,
    window_seconds: int = WA_OUTBOUND_DIRECT_WINDOW_SECONDS,
) -> tuple[bool, int]:
    """Return (allowed, count) for direct outbound WA sends per source device + target."""
    source_key = normalize_wa_outbound_source(device_id)
    target_key = normalize_wa_outbound_target(target)
    if not target_key:
        return True, 0

    now = time.time()
    key = f"wa_outbound_direct:{source_key}:{target_key}"
    r = await get_redis()
    if r:
        try:
            await r.zremrangebyscore(key, 0, now - window_seconds)
            await r.zadd(key, {str(now): now})
            await r.expire(key, window_seconds * 2)
            count = int(await r.zcard(key))
            return count <= limit, count
        except Exception as exc:
            log.warning("wa_outbound_guard.redis_fail", error=str(exc))

    timestamps = _mem_outbound_windows.setdefault(key, [])
    timestamps[:] = [ts for ts in timestamps if now - ts <= window_seconds]
    timestamps.append(now)
    if len(_mem_outbound_windows) > 5000:
        stale_keys = [
            existing_key
            for existing_key, values in _mem_outbound_windows.items()
            if not values or now - values[-1] > window_seconds * 2
        ]
        for stale_key in stale_keys:
            _mem_outbound_windows.pop(stale_key, None)
    count = len(timestamps)
    return count <= limit, count


def clear_wa_outbound_direct_memory() -> None:
    _mem_outbound_windows.clear()

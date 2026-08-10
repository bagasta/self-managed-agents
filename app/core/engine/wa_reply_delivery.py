from __future__ import annotations

import re
from typing import Any


_URL_RE = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")


def _text_value(value: Any) -> str:
    return str(value or "").strip()


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _urls(value: str) -> set[str]:
    return {match.rstrip(".,)") for match in _URL_RE.findall(value or "")}


def should_skip_whatsapp_final_reply(reply: str, steps: list[dict[str, Any]]) -> bool:
    """Return True when the final WA reply would duplicate an outbound tool send."""
    reply_text = _text_value(reply)
    if not reply_text:
        return True

    reply_norm = _normalized_text(reply_text)
    reply_urls = _urls(reply_text)

    for step in steps or []:
        tool_name = str((step or {}).get("tool") or "")
        args = (step or {}).get("args") if isinstance((step or {}).get("args"), dict) else {}

        if tool_name == "notify_user":
            sent_text = _text_value(args.get("message"))
            sent_norm = _normalized_text(sent_text)
            if not sent_norm:
                continue
            same_urls = bool(reply_urls and reply_urls == _urls(sent_text))
            same_text = reply_norm == sent_norm
            contained = (
                min(len(reply_norm), len(sent_norm)) >= 40
                and (reply_norm in sent_norm or sent_norm in reply_norm)
            )
            if same_text or contained or same_urls:
                return True

        if tool_name in {"send_whatsapp_document", "send_whatsapp_image"}:
            if not reply_urls and any(
                marker in reply_norm
                for marker in (
                    "sudah saya kirim",
                    "sudah terkirim",
                    "berhasil saya kirim",
                    "file sudah sampai",
                    "gambar sudah sampai",
                )
            ):
                return True

    return False

"""Input sanitization: flag potential prompt injection and strip control chars."""
from __future__ import annotations

import re

import structlog

log = structlog.get_logger(__name__)

_INJECTION_PATTERNS = [
    r"ignore (previous|all|prior) instructions",
    r"system prompt",
    r"you are now",
    r"disregard (your|all)",
    r"act as (if|though)",
]


def flag_potential_injection(text: str) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in _INJECTION_PATTERNS)


def sanitize_user_input(text: str) -> str:
    text = text.replace("\x00", "").strip()
    if flag_potential_injection(text):
        log.warning("input.potential_injection_detected", text_preview=text[:100])
    return text

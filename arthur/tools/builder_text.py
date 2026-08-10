"""Text helpers shared by Arthur builder tools."""
from __future__ import annotations

import re


def find_unfilled_placeholders(text: str) -> list[str]:
    """Find only real template placeholders, not examples like [instruksi]."""
    if not text:
        return []
    patterns = [
        r"\{(?:name|role|business|business_info|tasks|persona|escalation|extra_rules|agent_name|operator_phone)\}",
        r"\[(?:xxx|nama|nama [^\]]+|bisnis|produk|harga|operator|isi [^\]]+|contoh [^\]]+)\]",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return found


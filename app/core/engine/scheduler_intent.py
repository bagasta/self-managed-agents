"""Neutral reminder-workflow detection shared by runtime tool setup."""
from __future__ import annotations

import re


def looks_like_scheduler_workflow(text: str) -> bool:
    normalized = str(text or "").lower()
    normalized = re.sub(
        r"\b(?:tidak|nggak|enggak|gak|ga|tanpa|jangan)\s+"
        r"(?:perlu\s+|butuh\s+|pakai\s+|gunakan\s+|membuat\s+|buat\s+)?"
        r"(?:reminder|pengingat|alarm|timer)\b",
        " ",
        normalized,
    )
    patterns = (
        r"\b(?:reminder|remind|pengingat|alarm|timer)\b",
        r"\b(?:ingatkan|ingetin|mengingatkan|jadwalkan|jadwalin|menjadwalkan)\b",
        r"\bfollow[-\s]?up\b.{0,48}\b(?:otomatis|terjadwal|nanti|jam|tanggal|waktu)\b",
        r"\b(?:otomatis|terjadwal|nanti|jam|tanggal|waktu)\b.{0,48}\bfollow[-\s]?up\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)

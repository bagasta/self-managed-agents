"""PII redaction for log output."""
from __future__ import annotations

import re

_PII_PATTERNS = {
    "phone": r'\+?628\d{8,12}',
    "ktp": r'\b\d{16}\b',
    "email": r'\b[\w.\-]+@[\w.\-]+\.\w{2,}\b',
    "credit_card": r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
}


def redact_pii(text: str) -> str:
    for label, pattern in _PII_PATTERNS.items():
        text = re.sub(pattern, f"[REDACTED_{label.upper()}]", text)
    return text

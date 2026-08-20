"""Shared confirmation semantics for Arthur's deterministic build gates."""
from __future__ import annotations

import re
from typing import Any


def normalize_build_confirmation(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^\w+]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def is_explicit_build_confirmation(value: Any) -> bool:
    """Recognize natural approval without accepting a correction or rejection."""
    normalized = normalize_build_confirmation(value)
    if not normalized:
        return False
    if re.search(
        r"\b(?:belum|tidak|nggak|gak|jangan|bukan|not|don t|isn t|isnt)\b"
        r"(?:\s+\w+){0,7}\s+"
        r"\b(?:sesuai|setuju|benar|buat|buatkan|correct|right|approve|approved|"
        r"agree|good|proceed|create)\b",
        normalized,
    ):
        return False

    short_confirmations = {
        "ok",
        "oke",
        "okay",
        "sudah",
        "udah",
        "sesuai",
        "setuju",
        "buat",
        "buatkan",
        "buat agentnya",
        "approved",
        "approve",
        "agreed",
        "confirmed",
        "proceed",
        "go ahead",
        "looks good",
        "that is correct",
        "that s correct",
    }
    if normalized in short_confirmations:
        return True

    conversational = re.fullmatch(
        r"(?:(?:ya+|iya|sip|siap|ok|oke|okay|okey|mantap|gas)\s+)*"
        r"(?:(?:sudah|udah|semuanya|saya)\s+)?(?:sesuai|benar|setuju)"
        r"(?:\s+(?:ya+|nih|dong))?",
        normalized,
    )
    if conversational:
        return True

    markers = (
        "sudah sesuai",
        "udah sesuai",
        "sudah benar",
        "udah benar",
        "semuanya sesuai",
        "saya setuju",
        "setuju dibuat",
        "lanjut buat",
        "lanjutkan buat",
        "langsung buat",
        "langsung saja buat",
        "oke buat",
        "buat sekarang",
        "everything looks good",
        "all looks good",
        "i agree",
        "i approve",
        "go ahead and create",
        "proceed with creation",
    )
    return any(marker in normalized for marker in markers) or bool(
        re.search(
            r"\b(?:langsung|lanjut|lanjutkan|bisa|boleh|please|go ahead)\b"
            r"(?:\s+\w+){0,7}\s+\b(?:di)?buat(?:kan)?\b"
            r"(?:\s+\w+){0,5}\s+\bagent(?:nya)?\b",
            normalized,
        )
    )


def is_safe_build_confirmation_continuation(value: Any) -> bool:
    normalized = normalize_build_confirmation(value)
    return normalized in {
        "ok",
        "oke",
        "okay",
        "setuju",
        "agreed",
        "lanjut",
        "lanjutkan",
        "proceed",
        "go ahead",
        "buat",
        "buatkan",
        "langsung buat",
        "buat agentnya",
        "langsung buat agentnya",
        "ok langsung buat agentnya",
        "oke langsung buat agentnya",
    }

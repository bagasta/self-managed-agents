"""Small, deterministic checks for product-source claims in team results."""
from __future__ import annotations

import re
from urllib.parse import urlparse


_URL = re.compile(r"https?://[^\s<>|)\]]+", re.IGNORECASE)
_SEARCH_PATHS = {"", "/", "/find", "/search", "/search_product", "/search?", "/find?"}


def product_source_urls(text: str) -> set[str]:
    """Exclude marketplace search/home pages; keep concrete candidate pages."""
    sources: set[str] = set()
    for match in _URL.findall(text or ""):
        url = match.rstrip(".,;:!*_\"'}")
        parsed = urlparse(url)
        path = parsed.path.rstrip("/").casefold()
        if not parsed.hostname or path in _SEARCH_PATHS:
            continue
        if path.startswith(("/find/", "/search/", "/search?")):
            continue
        sources.add(url)
    return sources


def requested_product_count(text: str, default: int = 2) -> int:
    match = re.search(r"\b(\d{1,2})\s+produk\b", text or "", re.IGNORECASE)
    return max(1, min(int(match.group(1)), 20)) if match else default


def research_result_has_sources(result: str, request: str, source_text: str | None = None) -> bool:
    if re.search(r"\b(?:dari riset sebelumnya|harga lama|data lama)\b", result, re.IGNORECASE):
        return False
    required = max(requested_product_count(request), requested_product_count(result, default=0))
    result_urls = product_source_urls(result)
    if len(result_urls) < required:
        return False
    return source_text is None or result_urls.issubset(product_source_urls(source_text))


def looks_truncated(text: str) -> bool:
    """Catch obvious unfinished model output; not a substitute for provider finish reasons."""
    value = (text or "").strip()
    return bool(
        not value
        or value.count("**") % 2
        or value.count("```") % 2
        or re.search(r"\b(?:sekarang|selanjutnya|berikutnya|lalu|dan|yang|dengan|untuk)\s*$", value, re.IGNORECASE)
    )

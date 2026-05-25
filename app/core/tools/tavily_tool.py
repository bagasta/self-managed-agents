"""Tavily browsing tools for agent web search and page extraction."""
from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.tools import tool

from app.config import get_settings


TAVILY_BASE_URL = "https://api.tavily.com"


def _api_key() -> str:
    return (get_settings().tavily_api_key or "").strip()


def _client_transport(force_ipv4: bool) -> httpx.AsyncHTTPTransport | None:
    if not force_ipv4:
        return None
    return httpx.AsyncHTTPTransport(local_address="0.0.0.0")


def _parse_csv_or_json_list(value: str) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if isinstance(parsed, str):
            raw = parsed
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def _trim(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit].rstrip() + "\n...[truncated]"
    return value


def _format_search_response(data: dict[str, Any], *, max_content_chars: int = 900) -> str:
    results = []
    for item in data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": _trim(item.get("content") or "", max_content_chars),
                "score": item.get("score"),
            }
        )
    payload = {
        "query": data.get("query"),
        "answer": data.get("answer"),
        "results": results,
        "usage": data.get("usage"),
        "request_id": data.get("request_id"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_extract_response(data: dict[str, Any], *, max_chars: int) -> str:
    results = []
    per_result_limit = max(1000, min(max_chars, 12000))
    for item in data.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "url": item.get("url"),
                "raw_content": _trim(item.get("raw_content") or "", per_result_limit),
                "favicon": item.get("favicon"),
            }
        )
    payload = {
        "results": results,
        "failed_results": data.get("failed_results") or [],
        "usage": data.get("usage"),
        "request_id": data.get("request_id"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_tavily_tools(tools_config: dict[str, Any] | None = None) -> list:
    """Return Tavily search/extract tools.

    ``tools_config["tavily"]`` may be bool or dict. Supported dict keys:
    timeout_seconds, max_results, search_depth, include_answer.
    """
    raw_cfg = (tools_config or {}).get("tavily", {})
    cfg: dict[str, Any] = raw_cfg if isinstance(raw_cfg, dict) else {}
    timeout = float(cfg.get("timeout_seconds", 45))
    default_max_results = int(cfg.get("max_results", 5))
    default_search_depth = str(cfg.get("search_depth", "basic"))
    default_include_answer = cfg.get("include_answer", False)
    settings = get_settings()
    force_ipv4 = bool(cfg.get("force_ipv4", getattr(settings, "tavily_force_ipv4", True)))

    @tool
    async def tavily_search(
        query: str,
        max_results: int = default_max_results,
        topic: str = "general",
        search_depth: str = default_search_depth,
        time_range: str = "",
        include_domains: str = "",
        exclude_domains: str = "",
    ) -> str:
        """Search the web using Tavily and return concise sourced results.

        Use this for current facts, recommendations, news, pricing, references,
        and general web browsing. Args: query, max_results 1-10, topic
        (general/news/finance), search_depth (basic/fast/ultra-fast/advanced),
        optional time_range (day/week/month/year), include_domains/exclude_domains
        as comma-separated or JSON lists.
        """
        key = _api_key()
        if not key:
            return "[error] TAVILY_API_KEY belum dikonfigurasi di environment."
        if not query.strip():
            return "[error] query wajib diisi."
        safe_max_results = max(1, min(int(max_results or default_max_results), 10))
        payload: dict[str, Any] = {
            "query": query,
            "max_results": safe_max_results,
            "topic": topic if topic in {"general", "news", "finance"} else "general",
            "search_depth": search_depth if search_depth in {"basic", "fast", "ultra-fast", "advanced"} else "basic",
            "include_answer": default_include_answer,
            "include_raw_content": False,
            "include_usage": True,
        }
        if time_range:
            payload["time_range"] = time_range
        include = _parse_csv_or_json_list(include_domains)
        exclude = _parse_csv_or_json_list(exclude_domains)
        if include:
            payload["include_domains"] = include
        if exclude:
            payload["exclude_domains"] = exclude
        async def _post_search(search_payload: dict[str, Any]) -> httpx.Response:
            transport = _client_transport(force_ipv4)
            async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                resp = await client.post(
                    f"{TAVILY_BASE_URL}/search",
                    json=search_payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
            return resp

        try:
            resp = await _post_search(payload)
            if resp.status_code >= 400:
                return f"[error] Tavily search failed: HTTP {resp.status_code} {resp.text[:1000]}"
            return _format_search_response(resp.json())
        except httpx.TimeoutException:
            fallback_payload = dict(payload)
            fallback_payload["search_depth"] = "ultra-fast"
            fallback_payload["include_answer"] = False
            fallback_payload["max_results"] = min(safe_max_results, 5)
            try:
                resp = await _post_search(fallback_payload)
                if resp.status_code >= 400:
                    return f"[error] Tavily search fallback failed: HTTP {resp.status_code} {resp.text[:1000]}"
                data = resp.json()
                data["answer"] = data.get("answer") or "Fallback ultra-fast search after Tavily timeout."
                return _format_search_response(data)
            except Exception as fallback_exc:
                return (
                    "[error] Tavily search timed out, and fallback failed: "
                    f"{type(fallback_exc).__name__}: {fallback_exc!s}"
                )
        except Exception as exc:
            return f"[error] Tavily search failed: {type(exc).__name__}: {exc!s}"

    @tool
    async def tavily_extract(
        urls: str,
        query: str = "",
        max_chars: int = 8000,
        extract_depth: str = "basic",
    ) -> str:
        """Extract readable content from one or more URLs using Tavily.

        Use this after tavily_search when the user needs details from a source.
        Args: urls as a single URL, comma-separated URLs, or JSON list; optional
        query reranks chunks toward the user intent; max_chars limits returned text.
        """
        key = _api_key()
        if not key:
            return "[error] TAVILY_API_KEY belum dikonfigurasi di environment."
        url_list = _parse_csv_or_json_list(urls)
        if not url_list:
            return "[error] urls wajib diisi."
        payload: dict[str, Any] = {
            "urls": url_list[0] if len(url_list) == 1 else url_list[:5],
            "extract_depth": extract_depth if extract_depth in {"basic", "advanced"} else "basic",
            "format": "markdown",
            "include_usage": True,
        }
        if query.strip():
            payload["query"] = query
            payload["chunks_per_source"] = 3
        try:
            transport = _client_transport(force_ipv4)
            async with httpx.AsyncClient(timeout=max(timeout, 30.0), transport=transport) as client:
                resp = await client.post(
                    f"{TAVILY_BASE_URL}/extract",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
            if resp.status_code >= 400:
                return f"[error] Tavily extract failed: HTTP {resp.status_code} {resp.text[:1000]}"
            return _format_extract_response(resp.json(), max_chars=max(1000, min(int(max_chars or 8000), 20000)))
        except Exception as exc:
            return f"[error] Tavily extract failed: {type(exc).__name__}: {exc!s}"

    return [tavily_search, tavily_extract]

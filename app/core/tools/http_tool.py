"""
HTTP tool — lets the agent make GET/POST requests to external or internal APIs.

Configured via tools_config["http"]:
  enabled          bool   — must be true for this tool to be registered
  timeout_seconds  int    — per-request timeout (default 30)
  allowed_hosts    list   — if non-empty, only these hostnames are allowed (e.g. ["api.internal"])
                            pass [] or omit to allow any host

Example tools_config entry:
{
  "http": {
    "enabled": true,
    "timeout_seconds": 30,
    "allowed_hosts": []
  }
}
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool


def _check_host(url: str, allowed_hosts: list[str]) -> str | None:
    """Return an error string if url is not allowed, else None."""
    if not allowed_hosts:
        return None
    host = urlparse(url).hostname or ""
    if host not in allowed_hosts:
        return f"Host '{host}' is not in allowed_hosts: {allowed_hosts}"
    return None


def build_http_tools(tools_config: dict[str, Any]) -> list:
    """Return LangChain HTTP tools according to tools_config["http"]."""
    _raw = tools_config.get("http", {})
    cfg: dict[str, Any] = _raw if isinstance(_raw, dict) else {}
    timeout: int = int(cfg.get("timeout_seconds", 30))
    allowed_hosts: list[str] = cfg.get("allowed_hosts", [])

    @tool
    async def http_get(url: str, params: str = "{}", headers: str = "{}") -> str:
        """Make an HTTP GET request.
        Args:
          url     — full URL to request
          params  — JSON string of query parameters, e.g. '{"page": 1}'
          headers — JSON string of extra request headers
        Returns the response body (first 4000 chars) or an error message."""
        err = _check_host(url, allowed_hosts)
        if err:
            return f"[error] {err}"
        try:
            q_params = json.loads(params)
            h_extra = json.loads(headers)
        except json.JSONDecodeError as e:
            return f"[error] Invalid JSON in params or headers: {e}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params=q_params, headers=h_extra)
            return _format_response(resp)
        except Exception as exc:
            return f"[error] HTTP GET failed: {exc}"

    @tool
    async def http_post(url: str, body: str = "{}", headers: str = "{}") -> str:
        """Make an HTTP POST request with a JSON body.
        Args:
          url     — full URL to request
          body    — JSON string for the request body, e.g. '{"key": "value"}'
          headers — JSON string of extra request headers
        Returns the response body (first 4000 chars) or an error message."""
        err = _check_host(url, allowed_hosts)
        if err:
            return f"[error] {err}"
        try:
            payload = json.loads(body)
            h_extra = json.loads(headers)
        except json.JSONDecodeError as e:
            return f"[error] Invalid JSON in body or headers: {e}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=h_extra)
            return _format_response(resp)
        except Exception as exc:
            return f"[error] HTTP POST failed: {exc}"

    @tool
    async def http_patch(url: str, body: str = "{}", headers: str = "{}") -> str:
        """Make an HTTP PATCH request with a JSON body. Use this to update/edit existing resources.
        Args:
          url     — full URL to request
          body    — JSON string for the request body, e.g. '{"name": "new name"}'
          headers — JSON string of extra request headers
        Returns the response body (first 4000 chars) or an error message."""
        err = _check_host(url, allowed_hosts)
        if err:
            return f"[error] {err}"
        try:
            payload = json.loads(body)
            h_extra = json.loads(headers)
        except json.JSONDecodeError as e:
            return f"[error] Invalid JSON in body or headers: {e}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.patch(url, json=payload, headers=h_extra)
            return _format_response(resp)
        except Exception as exc:
            return f"[error] HTTP PATCH failed: {exc}"

    @tool
    async def http_delete(url: str, headers: str = "{}") -> str:
        """Make an HTTP DELETE request.
        Args:
          url     — full URL to request
          headers — JSON string of extra request headers
        Returns the response body (first 4000 chars) or an error message."""
        err = _check_host(url, allowed_hosts)
        if err:
            return f"[error] {err}"
        try:
            h_extra = json.loads(headers)
        except json.JSONDecodeError as e:
            return f"[error] Invalid JSON in headers: {e}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.delete(url, headers=h_extra)
            return _format_response(resp)
        except Exception as exc:
            return f"[error] HTTP DELETE failed: {exc}"

    return [http_get, http_post, http_patch, http_delete]


def _format_response(resp: httpx.Response) -> str:
    """Format an httpx response into a readable string."""
    content_type = resp.headers.get("content-type", "")
    body = resp.text[:4000]
    return (
        f"Status: {resp.status_code}\n"
        f"Content-Type: {content_type}\n"
        f"Body:\n{body}"
    )

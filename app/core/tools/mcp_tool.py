"""
MCP (Model Context Protocol) tool integration.

Reads `tools_config.mcp.servers` and loads all tools from the configured
MCP servers as LangChain-compatible tools via langchain-mcp-adapters.

IMPORTANT: The MCP client must stay alive for the entire duration of the agent
run. Use `mcp_client_context()` as an async context manager that yields the
loaded tools; do NOT call `build_mcp_tools()` and then close the context before
the agent finishes — the tool calls will fail.

tools_config shape
------------------
{
  "mcp": {
    "enabled": true,
    "servers": {
      "<server_name>": {
        # stdio transport (local process):
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {}         # optional extra env vars

        # OR streamable HTTP / SSE transport (remote server):
        # "url": "http://localhost:8080/mcp",
        # "transport": "streamable_http"   # or "sse"
      }
    }
  }
}

Yields a tuple: (tools: list[BaseTool], errors: dict[str, str])
  - tools: loaded tools (empty if connection failed)
  - errors: {server_name: error_message} for servers that failed to connect
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from langchain_core.tools import BaseTool

logger = structlog.get_logger(__name__)


def _get_cfg_value(key: str, default: str = "") -> str:
    """Read config from process env, fallback to app settings (.env-backed)."""
    val = os.getenv(key)
    if val is not None and str(val).strip() != "":
        return str(val).strip()

    try:
        from app.config import get_settings

        settings = get_settings()
        mapping = {
            "WORKSPACE_MCP_RUNTIME_URL": getattr(settings, "workspace_mcp_runtime_url", ""),
            "WORKSPACE_MCP_URL_LOCAL": getattr(settings, "workspace_mcp_url_local", ""),
            "WORKSPACE_MCP_PREFER_LOCAL": getattr(settings, "workspace_mcp_prefer_local", ""),
            "WORKSPACE_MCP_URL": getattr(settings, "workspace_mcp_url", ""),
            "WORKSPACE_MCP_TOKEN": getattr(settings, "workspace_mcp_token", ""),
        }
        candidate = mapping.get(key, "")
        return str(candidate).strip() if candidate is not None else default
    except Exception:
        return default


def _normalize_mcp_config(raw: dict | None) -> tuple[bool, dict]:
    """Support both legacy and current MCP config shapes."""
    mcp_cfg: dict = raw if isinstance(raw, dict) else {}
    if not mcp_cfg:
        return False, {}

    has_wrapper = "enabled" in mcp_cfg or "servers" in mcp_cfg
    if has_wrapper:
        enabled = bool(mcp_cfg.get("enabled", bool(mcp_cfg.get("servers"))))
        servers = mcp_cfg.get("servers", {})
        normalized = servers if isinstance(servers, dict) else {}
        if enabled and not normalized:
            default_url = _get_cfg_value("WORKSPACE_MCP_URL", "")
            if default_url:
                normalized = {
                    "google_workspace": {
                        "url": default_url,
                        "transport": "streamable_http",
                    }
                }
        return enabled, normalized

    legacy_servers = {
        name: cfg
        for name, cfg in mcp_cfg.items()
        if isinstance(cfg, dict) and ("url" in cfg or "command" in cfg)
    }
    if not legacy_servers:
        default_url = _get_cfg_value("WORKSPACE_MCP_URL", "")
        if default_url:
            return True, {
                "google_workspace": {
                    "url": default_url,
                    "transport": "streamable_http",
                }
            }
    return bool(legacy_servers), legacy_servers



def _build_server_map(servers: dict) -> dict:
    server_map: dict = {}
    runtime_url = _get_cfg_value("WORKSPACE_MCP_RUNTIME_URL", "")
    local_url = _get_cfg_value("WORKSPACE_MCP_URL_LOCAL", "")
    # IMPORTANT:
    # Default must keep configured MCP URL (often public tunnel), because
    # Google Workspace JWT audience is bound to that URL.
    # Forcing localhost by default can cause 401 Unauthorized (aud mismatch).
    prefer_local = _get_cfg_value("WORKSPACE_MCP_PREFER_LOCAL", "false").lower() in {"1", "true", "yes", "on"}

    for name, cfg in servers.items():
        if "url" in cfg:
            url = cfg["url"]
            if name == "google_workspace":
                # Safety-first for local deployment:
                # if runtime/local override is configured, prefer it to avoid
                # flaky public tunnel timeouts during tool calls.
                _override_url = runtime_url or local_url
                if _override_url and (prefer_local or "devtunnels.ms" in str(url)):
                    url = _override_url
            entry: dict = {
                "url": url,
                "transport": cfg.get("transport", "streamable_http"),
            }
            headers = dict(cfg.get("headers", {}))
            if "Authorization" not in headers:
                token = _get_cfg_value("WORKSPACE_MCP_TOKEN", "")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            # Required by workspace-mcp — without this header → 406 Not Acceptable
            headers.setdefault("Accept", "application/json, text/event-stream")
            entry["headers"] = headers
            server_map[name] = entry
            logger.info(
                "mcp_tools.server_selected",
                server=name,
                url=url,
                transport=entry["transport"],
                prefer_local=prefer_local,
                runtime_url=runtime_url or None,
                local_url=local_url or None,
                auth_header_present="Authorization" in headers,
            )
        elif "command" in cfg:
            entry: dict = {
                "command": cfg["command"],
                "args": cfg.get("args", []),
                "transport": "stdio",
            }
            if cfg.get("env"):
                entry["env"] = cfg["env"]
            server_map[name] = entry
        else:
            logger.warning("mcp_tools.invalid_server_config", server=name)
    return server_map


@asynccontextmanager
async def mcp_client_context(
    tools_config: dict,
) -> AsyncIterator[tuple[list[BaseTool], dict[str, str]]]:
    """
    Async context manager that opens MCP connections and yields:
      (tools, errors)
    where errors is a dict {server_name: error_message} for failed servers.

    Always yields — never raises. On failure, yields ([], {server: error}).
    """
    mcp_enabled, servers = _normalize_mcp_config(tools_config.get("mcp", {}))
    if not mcp_enabled:
        yield [], {}
        return

    if not servers:
        logger.warning("mcp_tools.no_servers_configured")
        yield [], {}
        return

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.error(
            "mcp_tools.import_error",
            hint="Run: pip install langchain-mcp-adapters mcp",
        )
        yield [], {s: "langchain-mcp-adapters not installed" for s in servers}
        return

    server_map = _build_server_map(servers)
    if not server_map:
        yield [], {}
        return

    client = MultiServerMCPClient(server_map)
    try:
        tools: list[BaseTool] = await client.get_tools()
        logger.info("mcp_tools.loaded", count=len(tools), servers=list(server_map))
    except BaseException as exc:
        errors: dict[str, str] = {}
        if hasattr(exc, "exceptions"):
            for sub in exc.exceptions:
                logger.error("mcp_tools.connection_failed", error=str(sub), type=type(sub).__name__)
            # Attribute all failures to all configured servers
            err_msg = "; ".join(str(s) for s in exc.exceptions)
            errors = {name: err_msg for name in server_map}
        else:
            logger.error("mcp_tools.connection_failed", error=str(exc), type=type(exc).__name__)
            errors = {name: str(exc) for name in server_map}
        yield [], errors
        return

    try:
        yield tools, {}
    except BaseException:
        pass

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
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from langchain_core.tools import BaseTool

logger = structlog.get_logger(__name__)


def _build_server_map(servers: dict) -> dict:
    server_map: dict = {}
    for name, cfg in servers.items():
        if "url" in cfg:
            server_map[name] = {
                "url": cfg["url"],
                "transport": cfg.get("transport", "streamable_http"),
            }
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
async def mcp_client_context(tools_config: dict) -> AsyncIterator[list[BaseTool]]:
    """
    Async context manager that opens MCP connections and yields a list of
    LangChain tools. Connections are kept alive until the `async with` block
    exits — the agent run MUST happen inside this block.

    Yields an empty list if MCP is disabled, misconfigured, or unavailable.
    """
    raw = tools_config.get("mcp", {})
    mcp_cfg: dict = raw if isinstance(raw, dict) else {}
    if not mcp_cfg.get("enabled", False):
        yield []
        return

    servers: dict = mcp_cfg.get("servers", {})
    if not servers:
        logger.warning("mcp_tools.no_servers_configured")
        yield []
        return

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.error(
            "mcp_tools.import_error",
            hint="Run: pip install langchain-mcp-adapters mcp",
        )
        yield []
        return

    server_map = _build_server_map(servers)
    if not server_map:
        yield []
        return

    try:
        async with MultiServerMCPClient(server_map) as client:
            tools: list[BaseTool] = client.get_tools()
            logger.info("mcp_tools.loaded", count=len(tools), servers=list(server_map))
            yield tools
    except Exception as exc:
        logger.error("mcp_tools.connection_failed", error=str(exc))
        yield []

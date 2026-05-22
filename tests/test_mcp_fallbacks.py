import asyncio

import pytest

from app.core.engine.agent_runner import _candidate_external_user_ids
from app.core.tools.mcp_tool import _normalize_mcp_config, mcp_client_context


def test_candidate_external_user_ids_generates_common_variants() -> None:
    vals = _candidate_external_user_ids('62895619356936', None)
    assert '62895619356936' in vals
    assert '+62895619356936' in vals
    assert '0895619356936' in vals


def test_candidate_external_user_ids_strips_whatsapp_suffix() -> None:
    vals = _candidate_external_user_ids('62895619356936@s.whatsapp.net', None)
    assert '62895619356936@s.whatsapp.net' in vals
    assert '62895619356936' in vals


def test_normalize_mcp_config_uses_workspace_env_when_enabled_no_servers(monkeypatch) -> None:
    monkeypatch.setenv('WORKSPACE_MCP_URL', 'https://example.com/mcp')
    enabled, servers = _normalize_mcp_config({'enabled': True, 'servers': {}})
    assert enabled is True
    assert servers['google_workspace']['url'] == 'https://example.com/mcp'


@pytest.mark.asyncio
async def test_mcp_context_propagates_body_exceptions(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, server_map):
            self.server_map = server_map

        async def get_tools(self):
            return []

        async def aclose(self):
            return None

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    cfg = {"mcp": {"enabled": True, "servers": {"demo": {"url": "http://localhost/mcp"}}}}
    with pytest.raises(RuntimeError, match="boom"):
        async with mcp_client_context(cfg):
            raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_mcp_context_does_not_swallow_cancellation(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, server_map):
            self.server_map = server_map

        async def get_tools(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient)

    cfg = {"mcp": {"enabled": True, "servers": {"demo": {"url": "http://localhost/mcp"}}}}
    with pytest.raises(asyncio.CancelledError):
        async with mcp_client_context(cfg):
            pass

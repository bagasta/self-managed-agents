import asyncio

import httpx
import pytest

from arthur_v2.google_oauth import start_google_oauth
from arthur_v2.plugin import _with_google_workspace_mcp


def test_start_google_oauth_accepts_existing_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", str(args[0]))
            return httpx.Response(200, json={"connected": True, "auth_url": "", "email": "owner@example.com"}, request=request)

    monkeypatch.setattr("arthur_v2.google_oauth.httpx.AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr("arthur_v2.google_oauth.get_settings", lambda: type("Settings", (), {"google_integration_service_url": "http://integration", "api_key": "test"})())

    result = asyncio.run(start_google_oauth(external_user_id="owner", agent_id="agent", scopes=[]))

    assert result.connected is True
    assert result.auth_url is None
    assert result.email == "owner@example.com"


def test_start_google_oauth_returns_link_for_new_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            request = httpx.Request("POST", str(args[0]))
            return httpx.Response(200, json={"connected": False, "auth_url": "https://oauth.example/start"}, request=request)

    monkeypatch.setattr("arthur_v2.google_oauth.httpx.AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr("arthur_v2.google_oauth.get_settings", lambda: type("Settings", (), {"google_integration_service_url": "http://integration", "api_key": "test"})())

    result = asyncio.run(start_google_oauth(external_user_id="owner", agent_id="agent", scopes=[]))

    assert result.connected is False
    assert result.auth_url == "https://oauth.example/start"


def test_google_mcp_config_preserves_existing_servers_and_statuses() -> None:
    config = _with_google_workspace_mcp(
        {
            "mcp": {"enabled": True, "servers": {"other": {"url": "https://other.example/mcp"}}},
            "integration_status": {"slack": "connected"},
        },
        mcp_url="http://google-workspace-mcp:8000",
        integration_status="connected",
    )

    assert config["mcp"]["enabled"] is True
    assert config["mcp"]["servers"]["other"]["url"] == "https://other.example/mcp"
    assert config["mcp"]["servers"]["google_workspace"] == {
        "url": "http://google-workspace-mcp:8000",
        "transport": "streamable_http",
    }
    assert config["integration_status"] == {"slack": "connected", "google_workspace": "connected"}

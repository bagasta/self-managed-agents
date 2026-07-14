from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.tools.builder_connector_tools import build_builder_connector_tools


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_builder_google_auth_uses_stable_owner_and_global_connection(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", kwargs))
            return FakeResponse(200, {"connected": False})

        async def post(self, url, **kwargs):
            calls.append(("POST", kwargs))
            return FakeResponse(200, {"auth_url": "https://auth.example/start?t=owner"})

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    tool = build_builder_connector_tools(
        get_settings=lambda: SimpleNamespace(
            google_integration_service_url="https://integration.example",
            api_key="test-key",
        ),
        owner_external_id="+628111111111",
    )["generate_google_auth_link"]

    result = json.loads(
        await tool.ainvoke(
            {
                "agent_id": "00000000-0000-0000-0000-000000000123",
                "external_user_id": "customer-session-id",
            }
        )
    )

    post_payload = next(kwargs["json"] for method, kwargs in calls if method == "POST")
    assert post_payload == {"external_user_id": "+628111111111", "agent_id": None}
    assert result["connected"] is False
    assert result["connection_scope"] == "owner"


@pytest.mark.asyncio
async def test_builder_google_auth_reuses_existing_global_connection(monkeypatch) -> None:
    posts = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            if kwargs.get("params", {}).get("agent_id"):
                return FakeResponse(200, {"connected": False})
            return FakeResponse(200, {"connected": True, "email": "owner@example.com"})

        async def post(self, url, **kwargs):
            posts.append(kwargs)
            return FakeResponse(500, {})

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    tool = build_builder_connector_tools(
        get_settings=lambda: SimpleNamespace(
            google_integration_service_url="https://integration.example",
            api_key="test-key",
        ),
        owner_external_id="+628111111111",
    )["generate_google_auth_link"]

    result = json.loads(
        await tool.ainvoke({"agent_id": "00000000-0000-0000-0000-000000000123"})
    )

    assert result["connected"] is True
    assert result["already_connected"] is True
    assert result["google_email"] == "owner@example.com"
    assert posts == []

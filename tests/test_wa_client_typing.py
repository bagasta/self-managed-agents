from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_demo_typing_uses_wa_dev_keepalive_endpoint(monkeypatch, respx_mock):
    from app.core.infra import wa_client

    requests: list[httpx.Request] = []
    monkeypatch.setattr(wa_client, "_wa_dev_base_url", lambda: "http://wa-dev-service:8081")
    route = respx_mock.post("http://wa-dev-service:8081/typing/start").mock(
        side_effect=lambda request: (
            requests.append(request)
            or httpx.Response(200, json={"status": "typing"})
        )
    )

    await wa_client.start_wa_typing("wadev_agent-id", "74350933852232@lid")

    assert route.return_value is None
    assert requests[0].content == b'{"to":"74350933852232@lid"}'


@pytest.mark.asyncio
async def test_demo_typing_stop_uses_wa_dev_keepalive_endpoint(monkeypatch, respx_mock):
    from app.core.infra import wa_client

    requests: list[httpx.Request] = []
    monkeypatch.setattr(wa_client, "_wa_dev_base_url", lambda: "http://wa-dev-service:8081")
    route = respx_mock.post("http://wa-dev-service:8081/typing/stop").mock(
        side_effect=lambda request: (
            requests.append(request)
            or httpx.Response(200, json={"status": "paused"})
        )
    )

    await wa_client.stop_wa_typing("wadev_agent-id", "74350933852232@lid")

    assert route.return_value is None
    assert requests[0].content == b'{"to":"74350933852232@lid"}'

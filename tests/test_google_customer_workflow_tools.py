from types import SimpleNamespace

import pytest

from app.core.engine.agent_google_routing import prepare_agent_google_workspace_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["personal", "business", "customer"])
@pytest.mark.parametrize("delegation", [None, False, True])
async def test_non_owner_keeps_configured_google_tools_with_owner_credentials(monkeypatch, kind, delegation):
    requests = []

    class Response:
        status_code = 200
        text = "{}"
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            subject = kwargs.get("params", {}).get("external_user_id")
            requests.append(subject)
            assert subject == "628111"
            return Response({"connected": True} if url.endswith("/status") else {"bearer_token": "owner-token"})

    monkeypatch.setattr("httpx.AsyncClient", Client)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(google_integration_service_url="https://devtunnel.example"))
    server = {"url": "http://google-workspace-mcp:8000/mcp", "transport": "streamable_http", "allowed_services": ["sheets"], "allowed_operations": {"sheets": ["read", "write"]}}
    if delegation is not None:
        server["delegated_runtime_access"] = delegation
    config = {"assistant_profile": {"kind": kind}, "mcp": {"enabled": True, "servers": {"google_workspace": server}}}
    agent = SimpleNamespace(owner_external_id="628111", operator_ids=["628222"], tools_config=config)
    session = SimpleNamespace(channel_type="whatsapp", external_user_id="628999", channel_config={"user_phone": "628999@s.whatsapp.net"})
    read_tool = SimpleNamespace(name="read_sheet_values")
    write_tool = SimpleNamespace(name="modify_sheet_values")
    tools = [read_tool, write_tool, SimpleNamespace(name="get_google_workspace_auth_link")]
    runtime = await prepare_agent_google_workspace_runtime(
        agent_model=agent, session=session, tools_config=config, tools=tools,
        active_groups=[], agent_id="00000000-0000-0000-0000-000000000001",
        memory_scope="628999", api_key="test", user_message="catat jawaban saya",
        system_prompt="", log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    assert runtime.enabled
    assert runtime.connected_user_id == "628111"
    assert server["headers"]["Authorization"] == "Bearer owner-token"
    assert runtime.preflight_error is None
    assert tools == [read_tool, write_tool]
    assert server["allowed_services"] == ["sheets"]
    assert server["allowed_operations"] == {"sheets": ["read", "write"]}
    assert requests and set(requests) == {"628111"}

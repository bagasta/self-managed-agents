import asyncio
from types import SimpleNamespace

import httpx
import pytest

from arthur_v2.google_oauth import start_google_oauth
from arthur_v2.plugin import (
    ARTHUR_V2_CODING_DEPLOY_MAX_TOKENS,
    _build_target_tool_usage,
    _capability_context_for_memory,
    _cloud_assistant_whatsapp_status,
    _google_spreadsheet_id_from_url,
    _needs_scheduler,
    _without_google_workspace_mcp,
    _with_google_workspace_mcp,
    build_arthur_v2_system_prompt,
    build_arthur_v2_tools,
)


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


def test_arthur_v2_does_not_receive_google_oauth_tools() -> None:
    tools = build_arthur_v2_tools(
        db_factory=None,
        owner_phone="628123456789",
        self_agent_id=None,
    )

    names = {tool.name for tool in tools}

    assert "start_google_mcp_oauth" not in names
    assert "get_google_mcp_oauth_status" not in names
    assert "refresh_assistant_whatsapp_qr" not in names
    assert "connect_assistant_whatsapp" not in names
    assert "connect_assistant_whatsapp_cloud" in names


def test_arthur_v2_setup_contract_uses_pairing_and_persists_scheduler_context() -> None:
    assert _needs_scheduler("asisten pribadi", "kirim reminder WhatsApp", "") is True
    assert _needs_scheduler("asisten tanya jawab", "jawab FAQ", "") is False
    memory = _capability_context_for_memory(scheduler=True, google_services=["calendar", "gmail"])
    assert "Scheduler aktif" in memory
    assert "calendar, gmail" in memory
    assert "OAuth" in memory

    prompt = build_arthur_v2_system_prompt()
    assert "connect_assistant_whatsapp_cloud" in prompt
    assert "returned signup_url verbatim on its own line as a bare URL" in prompt
    assert "Never wrap it in Markdown, parentheses, angle brackets, quotes, or trailing" in prompt
    assert "never offer, generate, or send a WhatsApp QR" in prompt
    assert "returned link to the owner verbatim" in prompt
    assert "explicit 8192-token output budget" in prompt
    assert ARTHUR_V2_CODING_DEPLOY_MAX_TOKENS == 8192


def test_target_google_sheets_contract_defines_safe_tool_sequence() -> None:
    contract = _build_target_tool_usage(google_services=["sheets"])

    assert "Runtime Tool Contract" in contract
    assert "read_sheet_values" in contract
    assert "append_table_rows" in contract
    assert "modify_sheet_values" in contract
    assert "get_google_workspace_auth_link" in contract


def test_google_spreadsheet_url_requires_concrete_google_sheet() -> None:
    assert _google_spreadsheet_id_from_url(
        "https://docs.google.com/spreadsheets/d/1c55zOY_R6fq3dHxV2m-jLGni4hl6kunA57mAETCAoIA/edit?usp=sharing"
    ) == "1c55zOY_R6fq3dHxV2m-jLGni4hl6kunA57mAETCAoIA"
    with pytest.raises(ValueError):
        _google_spreadsheet_id_from_url("https://example.com/not-a-sheet")


def test_google_mcp_removal_preserves_other_runtime_configuration() -> None:
    config = _without_google_workspace_mcp(
        {
            "mcp": {
                "enabled": True,
                "servers": {
                    "google_workspace": {"url": "http://google-workspace-mcp:8000/mcp"},
                    "other": {"url": "https://other.example/mcp"},
                },
            },
            "integration_status": {"google_workspace": "auth_pending", "slack": "connected"},
            "deploy": True,
        }
    )

    assert config["mcp"] == {"enabled": True, "servers": {"other": {"url": "https://other.example/mcp"}}}
    assert config["integration_status"] == {"slack": "connected"}
    assert config["deploy"] is True


def test_arthur_prompt_requires_runtime_configuration_before_existing_agent_oauth() -> None:
    prompt = build_arthur_v2_system_prompt()

    assert "configure_assistant_runtime" in prompt
    assert "Editing instructions never" in prompt
    assert "installs a connector" in prompt


def test_arthur_status_recognizes_embedded_signup_coexistence_without_legacy_device() -> None:
    status = _cloud_assistant_whatsapp_status(
        SimpleNamespace(
            wa_connection_type="cloud_api",
            wa_phone_number_id="phone-id",
            wa_waba_id="waba-id",
            wa_access_token_encrypted="enc:credential",
            wa_cloud_api_mode="coexistence",
            wa_display_phone="+62 823-1790-6045",
            wa_business_name="Coding Boo",
            wa_device_id=None,
        )
    )

    assert status == {
        "ok": True,
        "status": "connected",
        "connection_type": "cloud_api",
        "cloud_api_mode": "coexistence",
        "phone_number": "+62 823-1790-6045",
        "business_name": "Coding Boo",
    }

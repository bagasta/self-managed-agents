from app.core.tools.mcp_tool import _build_server_map


def test_build_server_map_keeps_configured_google_url_by_default(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_MCP_PREFER_LOCAL", "false")
    monkeypatch.delenv("WORKSPACE_MCP_RUNTIME_URL", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_URL_LOCAL", raising=False)

    server_map = _build_server_map(
        {
            "google_workspace": {
                "url": "https://example-tunnel.com/mcp",
                "transport": "streamable_http",
            }
        }
    )

    assert server_map["google_workspace"]["url"] == "https://example-tunnel.com/mcp"


def test_build_server_map_uses_local_override_only_when_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_MCP_PREFER_LOCAL", "true")
    monkeypatch.setenv("WORKSPACE_MCP_URL_LOCAL", "http://localhost:8002/mcp")
    monkeypatch.delenv("WORKSPACE_MCP_RUNTIME_URL", raising=False)

    server_map = _build_server_map(
        {
            "google_workspace": {
                "url": "https://example-tunnel.com/mcp",
                "transport": "streamable_http",
            }
        }
    )

    assert server_map["google_workspace"]["url"] == "http://localhost:8002/mcp"


def test_build_server_map_does_not_apply_global_token_to_google_workspace(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_MCP_TOKEN", "stale-global-token")
    monkeypatch.setenv("WORKSPACE_MCP_PREFER_LOCAL", "false")
    monkeypatch.delenv("WORKSPACE_MCP_RUNTIME_URL", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_URL_LOCAL", raising=False)

    server_map = _build_server_map(
        {
            "google_workspace": {
                "url": "http://localhost:8002/mcp",
                "transport": "streamable_http",
            }
        }
    )

    assert "Authorization" not in server_map["google_workspace"]["headers"]


def test_build_server_map_preserves_google_workspace_per_user_token(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_MCP_TOKEN", "stale-global-token")
    monkeypatch.setenv("WORKSPACE_MCP_PREFER_LOCAL", "false")
    monkeypatch.delenv("WORKSPACE_MCP_RUNTIME_URL", raising=False)
    monkeypatch.delenv("WORKSPACE_MCP_URL_LOCAL", raising=False)

    server_map = _build_server_map(
        {
            "google_workspace": {
                "url": "http://localhost:8002/mcp",
                "transport": "streamable_http",
                "headers": {"Authorization": "Bearer per-user-token"},
            }
        }
    )

    assert server_map["google_workspace"]["headers"]["Authorization"] == "Bearer per-user-token"


def test_build_server_map_still_applies_global_token_to_non_google_mcp(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_MCP_TOKEN", "shared-token")

    server_map = _build_server_map({"demo": {"url": "http://localhost:9999/mcp"}})

    assert server_map["demo"]["headers"]["Authorization"] == "Bearer shared-token"

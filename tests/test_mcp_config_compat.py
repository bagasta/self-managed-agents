from app.core.config_schema import ToolsConfig
from app.core.tools.mcp_tool import _normalize_mcp_config


def test_tools_config_accepts_legacy_mcp_shape() -> None:
    cfg = {
        "memory": True,
        "mcp": {
            "google_workspace": {
                "url": "https://example.com/mcp",
                "transport": "streamable_http",
            }
        },
    }
    model = ToolsConfig.model_validate(cfg)
    assert "google_workspace" in model.mcp


def test_tools_config_accepts_wrapped_mcp_shape() -> None:
    cfg = {
        "memory": True,
        "mcp": {
            "enabled": True,
            "servers": {
                "google_workspace": {
                    "url": "https://example.com/mcp",
                    "transport": "streamable_http",
                }
            },
        },
    }
    model = ToolsConfig.model_validate(cfg)
    assert model.mcp.get("enabled") is True


def test_normalize_mcp_config_legacy() -> None:
    enabled, servers = _normalize_mcp_config(
        {"google_workspace": {"url": "https://example.com/mcp"}}
    )
    assert enabled is True
    assert "google_workspace" in servers


def test_normalize_mcp_config_wrapped() -> None:
    enabled, servers = _normalize_mcp_config(
        {
            "enabled": True,
            "servers": {"google_workspace": {"url": "https://example.com/mcp"}},
        }
    )
    assert enabled is True
    assert "google_workspace" in servers

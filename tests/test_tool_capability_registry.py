from app.core.engine.tool_capability_registry import (
    build_runtime_tool_contract_text,
    disabled_capability_claims,
    is_capability_enabled,
)


def test_registry_detects_google_workspace_from_mcp_config():
    tools_config = {
        "mcp": {
            "enabled": True,
            "servers": {"google_workspace": {"url": "http://localhost:8002/mcp"}},
        }
    }

    assert is_capability_enabled("google_workspace", tools_config=tools_config, active_groups=["mcp"])


def test_registry_detects_subagents_from_runtime_group():
    assert is_capability_enabled("subagents", tools_config={}, active_groups=["subagents(2)"])


def test_runtime_contract_uses_registry_disabled_reasons():
    text = build_runtime_tool_contract_text(
        tools_config={"memory": True},
        active_groups=["memory"],
    )

    assert "Memory: aktif" in text
    assert "WhatsApp Media: tidak aktif/tersedia pada run ini; pengiriman media WhatsApp tidak tersedia" in text
    assert "Sandbox: tidak aktif/tersedia pada run ini; sandbox tidak tersedia" in text
    assert "jangan klaim bisa memakainya" in text


def test_disabled_capability_claims_detects_whatsapp_media_claim():
    claims = disabled_capability_claims(
        "File PDF sudah saya kirim ke WhatsApp.",
        tools_config={"memory": True, "whatsapp_media": False},
        active_groups=["memory"],
    )

    assert [claim.key for claim in claims] == ["whatsapp_media"]


def test_disabled_capability_claims_ignores_enabled_whatsapp_media():
    claims = disabled_capability_claims(
        "File PDF sudah saya kirim ke WhatsApp.",
        tools_config={"memory": True, "whatsapp_media": True},
        active_groups=["memory", "whatsapp_media"],
    )

    assert claims == []


def test_disabled_capability_claims_does_not_block_honest_limitation():
    claims = disabled_capability_claims(
        "Saya belum bisa mengirim file lewat WhatsApp sampai Owner mengaktifkan media.",
        tools_config={"memory": True, "whatsapp_media": False},
        active_groups=["memory"],
    )

    assert claims == []

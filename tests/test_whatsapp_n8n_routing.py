from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import agents


@pytest.mark.asyncio
async def test_existing_agent_routing_defaults_to_ai_staff():
    agent = SimpleNamespace(
        wa_inbound_route=None,
        wa_n8n_webhook_url_encrypted=None,
    )

    response = agents._routing_response(agent)

    assert response.target == "ai_staff"
    assert response.n8n_configured is False


@pytest.mark.asyncio
async def test_n8n_routing_requires_completed_embedded_signup(monkeypatch):
    agent = SimpleNamespace(
        wa_connection_type=None,
        wa_phone_number_id=None,
        wa_waba_id=None,
        wa_access_token_encrypted=None,
        wa_n8n_webhook_url_encrypted=None,
        wa_n8n_webhook_secret_encrypted=None,
        wa_inbound_route="ai_staff",
        version=1,
    )
    monkeypatch.setattr(agents, "_get_active_agent", AsyncMock(return_value=agent))

    with pytest.raises(agents.HTTPException) as error:
        await agents.update_whatsapp_routing(
            "agent-id",
            agents.WhatsAppRoutingUpdate(
                target="n8n",
                n8n_webhook_url="https://n8n.example.com/webhook/sales",
            ),
            SimpleNamespace(),
            "api-key",
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_n8n_routing_encrypts_configuration_and_switches_owner(monkeypatch):
    agent = SimpleNamespace(
        wa_connection_type="cloud_api",
        wa_phone_number_id="phone-id",
        wa_waba_id="waba-id",
        wa_access_token_encrypted="enc:meta-token",
        wa_n8n_webhook_url_encrypted=None,
        wa_n8n_webhook_secret_encrypted=None,
        wa_inbound_route="ai_staff",
        version=1,
    )
    db = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(agents, "_get_active_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(agents, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(agents, "decrypt_value", lambda value: value.removeprefix("enc:"))

    response = await agents.update_whatsapp_routing(
        "agent-id",
        agents.WhatsAppRoutingUpdate(
            target="n8n",
            n8n_webhook_url="https://n8n.example.com/webhook/sales",
            n8n_webhook_secret="shared-secret",
        ),
        db,
        "api-key",
    )

    assert agent.wa_inbound_route == "n8n"
    assert agent.wa_n8n_webhook_url_encrypted.startswith("enc:https://n8n.example.com")
    assert agent.wa_n8n_webhook_secret_encrypted == "enc:shared-secret"
    assert response.target == "n8n"
    assert response.n8n_configured is True
    assert response.n8n_webhook_host == "n8n.example.com"
    db.flush.assert_awaited_once()

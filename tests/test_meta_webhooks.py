from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import meta_webhooks


@pytest.mark.asyncio
async def test_cloud_webhook_reply_uses_cloud_channel_config(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr("app.core.infra.channel_service.send_message", sent)
    session = SimpleNamespace(
        ai_disabled=False,
        channel_config={
            "user_phone": "628123",
            "meta_phone_number_id": "phone-id",
            "meta_access_token": "enc:credential",
        },
    )

    await meta_webhooks._send_cloud_reply(session, "Halo dari Arthur")

    sent.assert_awaited_once_with(
        channel_type="whatsapp",
        channel_config=session.channel_config,
        text="Halo dari Arthur",
    )


@pytest.mark.asyncio
async def test_cloud_webhook_does_not_send_when_ai_is_disabled(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr("app.core.infra.channel_service.send_message", sent)
    session = SimpleNamespace(ai_disabled=True, channel_config={})

    await meta_webhooks._send_cloud_reply(session, "Halo")

    sent.assert_not_awaited()


def test_agent_runner_skips_legacy_typing_for_cloud_api_sessions():
    from app.core.engine import agent_runner

    source = inspect.getsource(agent_runner.run_agent)

    assert "_is_cloud_api_session" in source
    assert "and not _is_cloud_api_session" in source

def test_hosted_signup_account_update_is_recognized_without_logging_customer_ids():
    summary = meta_webhooks._account_update_summary(
        {
            "field": "account_update",
            "value": {
                "event": "PARTNER_ADDED",
                "waba_info": {"waba_id": "customer-waba", "owner_business_id": "customer-business"},
            },
        }
    )

    assert summary == {
        "account_event": "PARTNER_ADDED",
        "has_waba": True,
        "has_business_portfolio": True,
    }
    assert "customer-waba" not in repr(summary)
    assert meta_webhooks._account_update_summary({"field": "messages", "value": {}}) is None

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
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


def test_cloud_webhook_routes_supported_media_through_the_existing_media_pipeline():
    source = inspect.getsource(meta_webhooks.receive_meta_webhook)
    processor = inspect.getsource(meta_webhooks._process)

    assert 'message.get("type") in {"text", "image", "document", "audio"}' in source
    assert "download_media(media_id, token)" in processor
    assert "process_wa_media(" in processor
    assert 'processing_type = "ptt"' in processor
    assert 'media.get("voice")' in processor
    assert "media_image_b64=media_image_b64" in processor
    assert "current_attachment_name=current_attachment_name" in processor


def test_n8n_event_payload_is_scoped_to_one_agent_without_meta_credentials():
    agent = SimpleNamespace(
        id="agent-id",
        name="Sales n8n",
        wa_display_phone="+628123",
        wa_cloud_api_mode="coexistence",
        wa_access_token_encrypted="enc:must-not-leak",
        wa_n8n_webhook_url_encrypted="enc:must-not-leak",
    )

    payload = meta_webhooks._n8n_event_payload(
        agent,
        "phone-number-id",
        {
            "id": "wamid.123",
            "from": "628999",
            "timestamp": "1234567890",
            "type": "text",
            "text": {"body": "Halo n8n"},
        },
        "Budi",
    )

    assert payload["agent"] == {"id": "agent-id", "name": "Sales n8n"}
    assert payload["whatsapp"]["phone_number_id"] == "phone-number-id"
    assert payload["contact"] == {"phone": "628999", "name": "Budi"}
    assert payload["message"]["text"] == "Halo n8n"
    assert "must-not-leak" not in repr(payload)


def test_n8n_reply_accepts_common_ai_workflow_response_shapes():
    assert meta_webhooks._n8n_reply({"reply": "Jawaban"}) == "Jawaban"
    assert meta_webhooks._n8n_reply({"output": "Output agent"}) == "Output agent"
    assert meta_webhooks._n8n_reply([{"text": "Teks workflow"}]) == "Teks workflow"
    assert meta_webhooks._n8n_reply({"ok": True}) == ""


def test_n8n_route_is_exclusive_and_never_falls_back_to_ai_staff():
    receiver = inspect.getsource(meta_webhooks.receive_meta_webhook)
    processor = inspect.getsource(meta_webhooks._process_n8n)

    assert '_process_n8n if str(agent.wa_inbound_route or "ai_staff") == "n8n" else _process' in receiver
    assert "do not fall back to AI Staff" in processor


def test_internal_ai_staff_forward_cannot_reprocess_an_n8n_number():
    receiver = inspect.getsource(meta_webhooks.receive_meta_webhook)

    assert 'forced_route == "ai_staff"' in receiver
    assert '!= "ai_staff"' in receiver


@pytest.mark.asyncio
async def test_n8n_processor_sends_workflow_reply_without_exposing_meta_token(monkeypatch):
    agent = SimpleNamespace(
        id="agent-id",
        name="Sales n8n",
        wa_inbound_route="n8n",
        wa_n8n_webhook_url_encrypted="enc:https://n8n.example.com/webhook/sales",
        wa_n8n_webhook_secret_encrypted="enc:n8n-secret",
        wa_access_token_encrypted="enc:meta-token",
        wa_display_phone="+628123",
        wa_cloud_api_mode="coexistence",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=agent))

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    posted = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"reply": "Balasan n8n"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            posted.update(url=url, payload=json, headers=headers)
            return FakeResponse()

    typing = AsyncMock()
    sent = AsyncMock()
    monkeypatch.setattr(meta_webhooks, "AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr(meta_webhooks.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr("app.core.infra.channel_service.decrypt_value", lambda value: value.removeprefix("enc:"))
    monkeypatch.setattr("app.core.infra.wa_cloud_client.send_typing_indicator", typing)
    monkeypatch.setattr("app.core.infra.wa_cloud_client.send_text_message", sent)

    await meta_webhooks._process_n8n(
        "agent-id",
        "phone-id",
        {"id": "wamid.123", "from": "628999", "type": "text", "text": {"body": "Halo"}},
        "Budi",
    )

    assert posted["url"] == "https://n8n.example.com/webhook/sales"
    assert posted["headers"]["Authorization"] == "Bearer n8n-secret"
    assert "meta-token" not in repr(posted["payload"])
    typing.assert_awaited_once_with("phone-id", "wamid.123", "meta-token")
    sent.assert_awaited_once_with("phone-id", "628999", "Balasan n8n", "meta-token")


@pytest.mark.asyncio
async def test_n8n_voice_note_payload_contains_base64_but_not_meta_token(monkeypatch):
    downloaded = AsyncMock(return_value=(b"voice-bytes", "audio/ogg"))
    monkeypatch.setattr("app.core.infra.wa_cloud_client.download_media", downloaded)

    prepared = await meta_webhooks._prepare_n8n_audio(
        {
            "id": "wamid.voice",
            "from": "628999",
            "type": "audio",
            "audio": {"id": "media-id", "mime_type": "audio/ogg", "voice": True},
        },
        "meta-token",
    )

    downloaded.assert_awaited_once_with(
        "media-id",
        "meta-token",
        max_bytes=meta_webhooks.N8N_INLINE_AUDIO_MAX_BYTES,
    )
    assert prepared["audio"]["voice"] is True
    assert prepared["audio"]["download_status"] == "succeeded"
    assert prepared["audio"]["data_base64"] == "dm9pY2UtYnl0ZXM="
    assert "meta-token" not in repr(prepared)


@pytest.mark.asyncio
async def test_cloud_typing_indicator_uses_official_meta_payload(monkeypatch, respx_mock):
    from app.core.infra.wa_cloud_client import send_typing_indicator

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(meta_graph_api_version="v26.0"),
    )
    captured = {}

    def capture(request):
        captured["request"] = request
        return httpx.Response(200, json={"success": True})

    respx_mock.post(
        "https://graph.facebook.com/v26.0/phone-id/messages"
    ).mock(side_effect=capture)

    await send_typing_indicator("phone-id", "wamid.123", "meta-token")

    request = captured["request"]
    assert request.headers["Authorization"] == "Bearer meta-token"
    assert request.content == (
        b'{"messaging_product":"whatsapp","status":"read",'
        b'"message_id":"wamid.123","typing_indicator":{"type":"text"}}'
    )


@pytest.mark.asyncio
async def test_ai_staff_voice_note_uses_existing_transcription_pipeline(monkeypatch):
    agent = SimpleNamespace(
        id="agent-id",
        wa_access_token_encrypted="enc:meta-token",
        is_arthur=False,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=agent), commit=AsyncMock())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        channel_config={},
        ai_disabled=False,
    )

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    find_session = AsyncMock(return_value=(session, True))
    typing = AsyncMock()
    download = AsyncMock(return_value=(b"voice-bytes", "audio/ogg; codecs=opus"))
    process_media = AsyncMock(return_value=("\nTranskripsi: halo", None, None, {"filename": "incoming_audio.ogg"}))
    run_agent = AsyncMock(return_value={"reply": "Halo juga"})
    send_reply = AsyncMock()

    monkeypatch.setattr(meta_webhooks, "AsyncSessionLocal", lambda: SessionContext())
    monkeypatch.setattr("app.api.wa_helpers.find_or_create_wa_session", find_session)
    monkeypatch.setattr("app.api.wa_helpers.process_wa_media", process_media)
    monkeypatch.setattr("app.core.engine.agent_runner.run_agent", run_agent)
    monkeypatch.setattr("app.core.infra.channel_service.decrypt_value", lambda value: value.removeprefix("enc:"))
    monkeypatch.setattr("app.core.infra.wa_cloud_client.send_typing_indicator", typing)
    monkeypatch.setattr("app.core.infra.wa_cloud_client.download_media", download)
    monkeypatch.setattr(meta_webhooks, "_send_cloud_reply", send_reply)

    await meta_webhooks._process(
        "agent-id",
        "phone-id",
        {
            "id": "wamid.voice",
            "from": "628999",
            "type": "audio",
            "audio": {"id": "media-id", "mime_type": "audio/ogg", "voice": True},
        },
        "Budi",
    )

    typing.assert_awaited_once_with("phone-id", "wamid.voice", "meta-token")
    download.assert_awaited_once_with("media-id", "meta-token")
    assert process_media.await_args.kwargs["media_type"] == "ptt"
    assert process_media.await_args.kwargs["media_filename"] == "incoming_audio.ogg"
    assert run_agent.await_args.kwargs["user_message"].endswith("Transkripsi: halo")
    send_reply.assert_awaited_once_with(session, "Halo juga")
    db.commit.assert_awaited_once()

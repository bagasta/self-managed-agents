import uuid
from types import SimpleNamespace

import pytest

from app.api.integrations import (
    GoogleOAuthSuccessEvent,
    _deliver_google_oauth_success_whatsapp,
    _google_oauth_success_message,
    _mark_google_workspace_connected,
    _oauth_identity_candidates,
)


def _agent(*, builder: bool, wa_device_id: str, agent_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=agent_id or uuid.uuid4(),
        capabilities=["builder"] if builder else [],
        tools_config={"builder": builder},
        wa_device_id=wa_device_id,
        owner_external_id="628111",
    )


def _session(*, agent_id: uuid.UUID, external_user_id: str, device_id: str = ""):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        external_user_id=external_user_id,
        channel_type="whatsapp",
        channel_config={
            "device_id": device_id,
            "phone_number": external_user_id,
            "user_phone": f"{external_user_id}@s.whatsapp.net",
        },
    )


def test_oauth_identity_candidates_cover_whatsapp_variants() -> None:
    assert _oauth_identity_candidates("+628111@s.whatsapp.net") == [
        "+628111@s.whatsapp.net",
        "+628111",
        "628111",
        "628111@s.whatsapp.net",
        "08111",
    ]


def test_google_oauth_success_message_includes_email_without_token_details() -> None:
    message = _google_oauth_success_message("owner@example.com")

    assert "Autentikasi Google berhasil" in message
    assert "owner@example.com" in message
    assert "access_token" not in message
    assert "refresh_token" not in message


@pytest.mark.asyncio
async def test_oauth_success_prefers_builder_session_and_sends_from_its_device(monkeypatch) -> None:
    target_agent_id = uuid.uuid4()
    operational_agent = _agent(builder=False, wa_device_id="operational-device", agent_id=target_agent_id)
    builder_agent = _agent(builder=True, wa_device_id="arthur-device")
    operational_session = _session(agent_id=operational_agent.id, external_user_id="628111")
    builder_session = _session(agent_id=builder_agent.id, external_user_id="628111")

    class FakeResult:
        def all(self):
            return [
                (operational_session, operational_agent),
                (builder_session, builder_agent),
            ]

    class FakeDB:
        async def execute(self, statement):
            return FakeResult()

    sent = []

    async def fake_send_message(**kwargs):
        sent.append(kwargs)
        return {"message_id": "msg-1"}

    monkeypatch.setattr("app.core.infra.channel_service.send_message", fake_send_message)

    payload, status_code = await _deliver_google_oauth_success_whatsapp(
        db=FakeDB(),
        event=GoogleOAuthSuccessEvent(
            external_user_id="628111",
            agent_id=str(target_agent_id),
            google_email="owner@example.com",
        ),
    )

    assert status_code == 200
    assert payload == {"notified": True}
    assert sent == [
        {
            "channel_type": "whatsapp",
            "channel_config": {
                "device_id": "arthur-device",
                "phone_number": "628111",
                "user_phone": "628111@s.whatsapp.net",
            },
            "text": _google_oauth_success_message("owner@example.com"),
            "to_override": "628111",
        }
    ]


@pytest.mark.asyncio
async def test_oauth_success_reports_when_no_whatsapp_session_exists() -> None:
    class FakeResult:
        def all(self):
            return []

    class FakeDB:
        async def execute(self, statement):
            return FakeResult()

    payload, status_code = await _deliver_google_oauth_success_whatsapp(
        db=FakeDB(),
        event=GoogleOAuthSuccessEvent(external_user_id="628111"),
    )

    assert status_code == 404
    assert payload == {"notified": False, "reason": "whatsapp_session_not_found"}


@pytest.mark.asyncio
async def test_oauth_success_marks_the_owned_target_agent_connected() -> None:
    agent = _agent(builder=False, wa_device_id="device", agent_id=uuid.uuid4())
    agent.tools_config = {
        "mcp": {"enabled": True, "servers": {"google_workspace": {"url": "http://mcp"}}},
        "integration_status": {"google_workspace": "auth_pending"},
    }
    agent.version = 4

    class FakeDB:
        committed = False

        async def get(self, model, target_id):
            assert model is not None
            assert str(target_id) == str(agent.id)
            return agent

        async def commit(self):
            self.committed = True

    db = FakeDB()
    synced = await _mark_google_workspace_connected(
        db=db,
        event=GoogleOAuthSuccessEvent(external_user_id="+628111@s.whatsapp.net", agent_id=str(agent.id)),
    )

    assert synced is True
    assert db.committed is True
    assert agent.tools_config["mcp"]["enabled"] is True
    assert agent.tools_config["integration_status"]["google_workspace"] == "connected"
    assert agent.version == 5

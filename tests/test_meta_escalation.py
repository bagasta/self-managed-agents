import uuid
from types import SimpleNamespace

import pytest

from app.core.infra.wa_cloud_client import send_text_message
from app.core.tools.escalation_tool import _normalize_jid, build_escalation_tools


def test_send_to_number_normalizes_indonesian_local_phone_formats():
    assert _normalize_jid("0895619356936") == "62895619356936"
    assert _normalize_jid("895619356936") == "62895619356936"
    assert _normalize_jid("+62895619356936@s.whatsapp.net") == "62895619356936"


@pytest.mark.asyncio
async def test_escalation_keeps_session_meta_credentials_for_owner(monkeypatch):
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        channel_type="whatsapp",
        channel_config={
            "meta_phone_number_id": "phone-id",
            "meta_access_token": "encrypted-token",
            "device_id": "legacy-device",
            "user_phone": "628customer",
        },
        external_user_id="628customer",
        metadata_={},
    )
    agent = SimpleNamespace(
        id=agent_id,
        escalation_config={"operator_phone": "628owner"},
        owner_external_id="628owner",
        operator_ids=[],
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class InitialDB:
        def __init__(self):
            self.values = iter((session, agent))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            return Result(next(self.values))

        def add(self, *_args):
            pass

        async def commit(self):
            pass

    class PersistDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            return Result(session)

        def add(self, *_args):
            pass

        async def commit(self):
            pass

    databases = iter((InitialDB(), PersistDB()))
    sent = {}

    async def send_message(**kwargs):
        sent.update(kwargs)
        return {"message_id": "wamid.owner-notification"}

    monkeypatch.setattr("app.core.infra.channel_service.send_message", send_message)

    tools = build_escalation_tools(session_id, agent_id, lambda: next(databases))
    escalate = next(tool for tool in tools if tool.name == "escalate_to_human")

    result = await escalate.ainvoke({"reason": "needs approval", "summary": "Customer needs review"})

    assert not result.startswith("[error]")
    assert sent["channel_type"] == "whatsapp"
    assert sent["channel_config"]["user_phone"] == "628owner"
    assert sent["channel_config"]["meta_phone_number_id"] == "phone-id"
    assert sent["channel_config"]["meta_access_token"] == "encrypted-token"
    assert session.metadata_["escalation_message_ids"] == ["wamid.owner-notification"]


@pytest.mark.asyncio
async def test_escalation_returns_error_when_whatsapp_send_fails(monkeypatch):
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=session_id,
        channel_type="whatsapp",
        channel_config={"meta_phone_number_id": "phone-id", "meta_access_token": "encrypted-token"},
        external_user_id="628customer",
        metadata_={},
    )
    agent = SimpleNamespace(
        id=agent_id,
        escalation_config={"operator_phone": "628owner"},
        owner_external_id="628owner",
        operator_ids=[],
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self):
            self.values = iter((session, agent))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            return Result(next(self.values))

        def add(self, *_args):
            pass

        async def commit(self):
            pass

    async def send_message(**_kwargs):
        return None

    monkeypatch.setattr("app.core.infra.channel_service.send_message", send_message)
    tools = build_escalation_tools(session_id, agent_id, lambda: DB())
    escalate = next(tool for tool in tools if tool.name == "escalate_to_human")

    result = await escalate.ainvoke({"reason": "needs approval"})

    assert result.startswith("[error]")
    assert "gagal dikirim" in result


@pytest.mark.asyncio
async def test_cloud_send_normalizes_meta_message_id(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"messages": [{"id": "wamid.cloud-message"}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("app.core.infra.wa_cloud_client.httpx.AsyncClient", lambda **_kwargs: Client())

    result = await send_text_message("phone-id", "628owner", "Need approval", "token")

    assert result["message_id"] == "wamid.cloud-message"

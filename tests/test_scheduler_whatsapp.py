from types import SimpleNamespace

import pytest

from app.core.engine.agent_tool_setup import _should_self_heal_whatsapp_scheduler
from app.core.workers.scheduler_service import (
    _scheduled_channel_config,
    _send_scheduled_channel_message,
    _tick_with_lock,
)


def test_whatsapp_reminder_request_self_heals_scheduler_when_disabled() -> None:
    session = SimpleNamespace(channel_type="whatsapp")

    assert _should_self_heal_whatsapp_scheduler(
        session,
        "ingetin saya follow-up customer besok jam 9",
        {"scheduler": False},
    )


def test_non_reminder_whatsapp_request_does_not_self_heal_scheduler() -> None:
    session = SimpleNamespace(channel_type="whatsapp")

    assert not _should_self_heal_whatsapp_scheduler(
        session,
        "jadwal kelas holiday class ada apa saja?",
        {"scheduler": False},
    )


def test_scheduled_whatsapp_config_falls_back_to_agent_device_and_session_user() -> None:
    session = SimpleNamespace(
        channel_type="whatsapp",
        channel_config={},
        external_user_id="628111",
        agent_id="agent-1",
    )
    agent = SimpleNamespace(id="agent-1", wa_device_id="prod-device")

    cfg = _scheduled_channel_config(session, agent)

    assert cfg["device_id"] == "prod-device"
    assert cfg["user_phone"] == "628111"


def test_scheduled_whatsapp_config_falls_back_to_wadev_device() -> None:
    session = SimpleNamespace(
        channel_type="whatsapp",
        channel_config={},
        external_user_id="628111",
        agent_id="agent-1",
    )
    agent = SimpleNamespace(id="agent-1", wa_device_id="")

    cfg = _scheduled_channel_config(session, agent)

    assert cfg["device_id"] == "wadev_agent-1"
    assert cfg["user_phone"] == "628111"


@pytest.mark.asyncio
async def test_scheduled_whatsapp_send_raises_when_channel_returns_none(monkeypatch) -> None:
    session = SimpleNamespace(
        channel_type="whatsapp",
        channel_config={"device_id": "dev-1", "user_phone": "628111"},
    )
    agent = SimpleNamespace(id="agent-1", wa_device_id="")
    log = SimpleNamespace(info=lambda *args, **kwargs: None)

    async def fake_send_message(**kwargs):
        return None

    monkeypatch.setattr("app.core.infra.channel_service.send_message", fake_send_message)

    with pytest.raises(RuntimeError, match="WhatsApp reminder send returned no result"):
        await _send_scheduled_channel_message(session, agent, "halo", log)


@pytest.mark.asyncio
async def test_scheduler_tick_lock_and_unlock_share_one_db_session(monkeypatch) -> None:
    sessions = []
    tick_calls = []

    class FakeResult:
        def scalar(self):
            return True

    class FakeSession:
        def __init__(self):
            self.queries = []
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            self.queries.append(str(statement))
            return FakeResult()

        async def commit(self):
            self.commits += 1

    def fake_session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    async def fake_tick():
        tick_calls.append("tick")

    monkeypatch.setattr("app.database.AsyncSessionLocal", fake_session_factory)
    monkeypatch.setattr("app.core.workers.scheduler_service._tick", fake_tick)

    await _tick_with_lock()

    assert len(sessions) == 1
    assert tick_calls == ["tick"]
    assert sessions[0].queries == [
        "SELECT pg_try_advisory_lock(12345)",
        "SELECT pg_advisory_unlock(12345)",
    ]
    assert sessions[0].commits == 1

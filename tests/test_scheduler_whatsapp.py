from types import SimpleNamespace

import pytest

from app.core.engine.agent_tool_setup import _should_self_heal_whatsapp_scheduler
from app.core.workers.scheduler_service import (
    _scheduled_channel_config,
    _send_scheduled_channel_message,
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


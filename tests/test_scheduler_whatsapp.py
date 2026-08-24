import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.engine.agent_tool_setup import _should_self_heal_whatsapp_scheduler
from app.core.tools.scheduler_tool import build_scheduler_tools
from app.core.workers.scheduler_service import (
    _SCHEDULER_HEARTBEAT_KEY,
    _run_heartbeat_job,
    _scheduled_channel_config,
    _send_scheduled_channel_message,
    _tick_with_lock,
    _update_job_after_delivery,
    get_external_scheduler_health,
    publish_scheduler_heartbeat,
)
from app.models.scheduled_job import ScheduledJob


@pytest.mark.asyncio
async def test_scheduler_heartbeat_proves_external_worker_liveness(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.values = {}

        async def set(self, key, value, ex):
            self.values[key] = value
            assert ex >= 30

        async def get(self, key):
            return self.values.get(key)

    redis = FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(
        "app.core.workers.scheduler_service.get_redis",
        fake_get_redis,
    )

    assert await get_external_scheduler_health() == "stopped"
    assert await publish_scheduler_heartbeat() is True
    assert _SCHEDULER_HEARTBEAT_KEY in redis.values
    assert await get_external_scheduler_health() == "ok"


@pytest.mark.asyncio
async def test_heartbeat_charges_the_final_aggregated_run_usage_once(monkeypatch) -> None:
    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        async def execute(self, _statement):
            return _Result(SimpleNamespace(id=uuid.uuid4(), channel_type=None))

    memory_calls = 0

    async def fake_get_memory(*_args, **_kwargs):
        nonlocal memory_calls
        memory_calls += 1
        if memory_calls == 1:
            return SimpleNamespace(value_data='{"quiet_start":"00:00","quiet_end":"00:01"}')
        return None

    async def fake_run_agent(**_kwargs):
        return {"reply": "HEARTBEAT_OK", "tokens_used": 1234}

    charges = []

    async def fake_record(agent, tokens_used, db):
        charges.append((agent, tokens_used, db))

    monkeypatch.setattr("app.core.domain.memory_service.get_memory", fake_get_memory)
    monkeypatch.setattr("app.core.engine.agent_runner.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "app.core.domain.agent_quota_service.record_agent_token_usage", fake_record,
    )

    agent = SimpleNamespace(id="agent-1")
    job = SimpleNamespace(id="job-1", agent_id="agent-1", label="heartbeat:_global")
    db = _DB()

    await _run_heartbeat_job(job, agent, db)

    assert charges == [(agent, 1234, db)]


@pytest.mark.asyncio
async def test_set_reminder_with_runtime_contract_persists_scheduled_job() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(ScheduledJob.__table__.create)

        tools = build_scheduler_tools(session_id, agent_id, session_factory)
        set_reminder = next(tool for tool in tools if tool.name == "set_reminder")

        result = await set_reminder.ainvoke({
            "label": "followup_customer",
            "message": "Saatnya follow-up customer.",
            "schedule": "in 2m",
        })

        assert "berhasil di-set" in result
        async with session_factory() as db:
            job = (
                await db.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.session_id == session_id,
                        ScheduledJob.label == "followup_customer",
                    )
                )
            ).scalar_one()

        assert job.agent_id == agent_id
        assert job.payload == "Saatnya follow-up customer."
        assert job.status == "active"
        assert job.run_once_at is not None
        assert job.next_run_at == job.run_once_at
    finally:
        await engine.dispose()


def test_whatsapp_reminder_request_self_heals_scheduler_when_disabled() -> None:
    session = SimpleNamespace(channel_type="whatsapp")

    assert _should_self_heal_whatsapp_scheduler(
        session,
        "ingetin saya follow-up customer besok jam 9",
        {"scheduler": False},
    )


def test_natural_whatsapp_reminder_request_self_heals_scheduler_when_disabled() -> None:
    session = SimpleNamespace(channel_type="whatsapp")

    assert _should_self_heal_whatsapp_scheduler(
        session,
        "nanti jam 5 kabarin saya buat follow-up customer",
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


def test_failed_reminder_delivery_is_rescheduled_without_datetime_shadowing() -> None:
    now = datetime(2026, 7, 28, 13, 6, 52, tzinfo=timezone.utc)
    job = SimpleNamespace(status="running", next_run_at=None, cron_expr=None)

    _update_job_after_delivery(job, now=now, delivery_failed=True)

    assert job.status == "active"
    assert job.next_run_at == now + timedelta(minutes=1)


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

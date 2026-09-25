import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.domain.outbound_queue_service import (
    cancel_queued_outbound_for_target,
    enqueue_outbound_message,
)
from app.core.engine.wa_outbound_guard import (
    check_wa_outbound_direct_window,
    clear_wa_outbound_direct_memory,
)
from app.models.outbound_message import OutboundMessage


@pytest.mark.asyncio
async def test_newer_queued_message_supersedes_older_recipient_message() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    agent_id, session_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(OutboundMessage.__table__.create)
        async with session_factory() as db:
            first = await enqueue_outbound_message(
                db, agent_id=agent_id, session_id=session_id,
                target="+628120000001", text="pesan lama", source_device_id="wadev_a",
            )
            second = await enqueue_outbound_message(
                db, agent_id=agent_id, session_id=session_id,
                target="628120000001", text="pesan terbaru", source_device_id="wadev_a",
            )
            await db.commit()

        async with session_factory() as db:
            rows = (await db.execute(select(OutboundMessage).order_by(OutboundMessage.created_at))).scalars().all()
            assert len(rows) == 2
            assert first.id != second.id
            assert rows[0].status == "cancelled"
            assert rows[1].status == "queued"
            assert rows[1].text == "pesan terbaru"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_customer_turn_cancels_pending_outbound_message() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    agent_id, session_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(OutboundMessage.__table__.create)
        async with session_factory() as db:
            item = await enqueue_outbound_message(
                db, agent_id=agent_id, session_id=session_id,
                target="628120000001", text="jawaban lama",
            )
            assert await cancel_queued_outbound_for_target(
                db, agent_id=agent_id, target="+628120000001",
            ) == 1
            await db.commit()

        async with session_factory() as db:
            persisted = await db.get(OutboundMessage, item.id)
            assert persisted.status == "cancelled"
            assert persisted.last_error == "superseded_by_newer_customer_message"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_rate_limit_attempt_does_not_extend_window(monkeypatch) -> None:
    clear_wa_outbound_direct_memory()

    async def no_redis():
        return None

    monkeypatch.setattr("app.core.engine.wa_outbound_guard.get_redis", no_redis)
    for _ in range(3):
        assert (await check_wa_outbound_direct_window(device_id="dev", target="628120000001"))[0]
    allowed, count = await check_wa_outbound_direct_window(device_id="dev", target="628120000001")
    assert allowed is False
    assert count == 3

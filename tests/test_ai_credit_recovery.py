import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.domain.ai_credit_recovery import (
    backfill_credit_recovery,
    enqueue_credit_recovery,
    is_credit_exhaustion_error,
)
from app.core.workers import ai_credit_recovery as worker
from app.models.agent import Agent
from app.models.message import Message
from app.models.outbound_message import OutboundMessage
from app.models.pending_ai_reply import PendingAIReply
from app.models.run import Run
from app.models.session import Session

ERROR = "Error code: 402 - This request requires more credits, or fewer max_tokens."


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return "JSON"


@pytest_asyncio.fixture
async def database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        for model in (Agent, Session, Run, Message, PendingAIReply, OutboundMessage):
            await connection.run_sync(model.__table__.create)
    monkeypatch.setattr(worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(worker, "_execute_request", AsyncMock(side_effect=AssertionError("Unexpected model call")))
    yield factory
    await engine.dispose()


async def seed(database, *, queue=True):
    now = datetime.now(timezone.utc)
    agent = Agent(id=uuid.uuid4(), name="LapiBot", instructions="Test", wa_connection_type="cloud_api",
                  wa_phone_number_id="phone-id", wa_access_token_encrypted="private-test-token")
    session = Session(id=uuid.uuid4(), agent_id=agent.id, channel_type="whatsapp", external_user_id="628120000001")
    run = Run(id=uuid.uuid4(), session_id=session.id, status="failed", tokens_used=0,
              created_at=now - timedelta(minutes=5), error_message=ERROR)
    message = Message(id=uuid.uuid4(), session_id=session.id, run_id=run.id, role="user",
                      content="Tolong lanjutkan pendaftaran saya", timestamp=now - timedelta(minutes=5))
    async with database() as db:
        db.add_all([agent, session, run, message])
        await db.commit()
        if queue:
            item = await enqueue_credit_recovery(db, run=run)
            item.available_at = now - timedelta(seconds=1)
            await db.commit()
    return agent, session, run, message


@pytest.mark.parametrize("error,expected", [
    (ERROR, True), ("insufficient_quota", True), ("insufficient credits", True),
    ("429 rate limit exceeded", False), ("Google storage quota exceeded", False),
    ("401 invalid API key", False), ("402 unrelated tool result", False),
])
def test_credit_error_classification(error, expected):
    assert is_credit_exhaustion_error(error) is expected


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_keeps_attachment_payload(database):
    _, _, source, _ = await seed(database, queue=False)
    async with database() as db:
        run = await db.get(Run, source.id)
        item = await enqueue_credit_recovery(db, run=run, payload={"media_image_b64": "image-data"})
        same = await enqueue_credit_recovery(db, run=run)
        assert same.id == item.id
        assert same.payload["media_image_b64"] == "image-data"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_output,tokens", [(True, 0), (False, 100)])
async def test_partial_execution_is_never_replayed(database, has_output, tokens):
    _, session, source, _ = await seed(database, queue=False)
    async with database() as db:
        run = await db.get(Run, source.id)
        run.tokens_used = tokens
        if has_output:
            db.add(Message(session_id=session.id, run_id=run.id, role="tool", content="external write"))
        await db.flush()
        assert await enqueue_credit_recovery(db, run=run) is None


@pytest.mark.asyncio
async def test_backfill_only_latest_unanswered_request_and_repeat_is_safe(database):
    agent, session, _source, message = await seed(database, queue=False)
    async with database() as db:
        older = Run(id=uuid.uuid4(), session_id=session.id, status="failed", tokens_used=0,
                    error_message=ERROR, created_at=message.timestamp - timedelta(minutes=1))
        db.add(older)
        db.add(Message(session_id=session.id, run_id=older.id, role="user", content="older request",
                       timestamp=message.timestamp - timedelta(minutes=1)))
        await db.commit()
        since = datetime.now(timezone.utc) - timedelta(hours=1)
        assert await backfill_credit_recovery(db, agent_id=agent.id, since=since) == {
            "eligible": 1, "enqueued": 0, "expired": 0,
        }
        result = await backfill_credit_recovery(db, agent_id=agent.id, since=since, apply=True)
        assert result["enqueued"] == 1
        assert (await backfill_credit_recovery(db, agent_id=agent.id, since=since, apply=True))["enqueued"] == 0


@pytest.mark.asyncio
async def test_due_claim_is_not_claimed_twice(database):
    _, _, source, _ = await seed(database)
    assert await worker.claim_due_replies() == [source.id]
    assert await worker.claim_due_replies() == []


@pytest.mark.asyncio
async def test_answer_in_another_session_for_same_recipient_cancels_recovery(database):
    agent, original_session, source, _ = await seed(database)
    async with database() as db:
        other = Session(id=uuid.uuid4(), agent_id=agent.id, channel_type="whatsapp",
                        external_user_id=original_session.external_user_id)
        db.add(other)
        db.add(Message(session_id=other.id, role="agent", content="Sudah terjawab",
                       timestamp=datetime.now(timezone.utc)))
        await db.commit()
    await worker.claim_due_replies()
    await worker.run_pending_reply(source.id)
    async with database() as db:
        assert (await db.get(PendingAIReply, source.id)).status == "cancelled"


async def fake_model(database, *, credit=False, after=None, **kwargs):
    db = kwargs["db"]
    run = Run(id=kwargs["retry_run_id"], session_id=kwargs["session"].id,
              status="failed" if credit else "completed", tokens_used=0,
              error_message=ERROR if credit else None)
    db.add(run)
    if not credit:
        db.add(Message(session_id=run.session_id, run_id=run.id, role="agent",
                       content="Pendaftaran Anda sudah saya lanjutkan.", step_index=1))
    await db.flush()
    if after:
        await after(db, run.session_id)
    return {"reply": "" if credit else "Pendaftaran Anda sudah saya lanjutkan.", "steps": [], "tokens_used": 0}


@pytest.mark.asyncio
async def test_outage_defers_without_sending_or_creating_duplicate_inbound(database, monkeypatch):
    _, _, source, _ = await seed(database)
    async def model(**kwargs):
        return await fake_model(database, credit=True, **kwargs)
    monkeypatch.setattr(worker, "_execute_request", model)
    await worker.claim_due_replies()
    await worker.run_pending_reply(source.id)
    async with database() as db:
        item = await db.get(PendingAIReply, source.id)
        assert item.status == "waiting" and item.attempts == 1
        assert item.retry_run_id is None
        assert item.available_at > datetime.now(timezone.utc).replace(tzinfo=None)
        assert len(list((await db.execute(select(Message))).scalars())) == 1
        assert list((await db.execute(select(OutboundMessage))).scalars()) == []


@pytest.mark.asyncio
async def test_credit_restored_hands_off_one_reply_without_retrigger(database, monkeypatch):
    _, _, source, original = await seed(database)
    async def model(**kwargs):
        return await fake_model(database, **kwargs)
    model_mock = AsyncMock(side_effect=model)
    monkeypatch.setattr(worker, "_execute_request", model_mock)
    await worker.claim_due_replies()
    await worker.run_pending_reply(source.id)
    await worker.run_pending_reply(source.id)
    async with database() as db:
        item = await db.get(PendingAIReply, source.id)
        assert item.status == "completed"
        rows = list((await db.execute(select(OutboundMessage))).scalars())
        assert len(rows) == 1 and rows[0].kind == "ai_credit_recovery"
        assert rows[0].target == "628120000001"
        assert (await db.get(Message, original.id)).run_id == item.retry_run_id
        users = list((await db.execute(select(Message).where(Message.role == "user"))).scalars())
        assert len(users) == 1
    model_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivery_failure_retries_saved_text_without_rerunning_model(database, monkeypatch):
    from app.core.workers import scheduler_service
    _, _, source, _ = await seed(database)
    async def model(**kwargs):
        return await fake_model(database, **kwargs)
    model_mock = AsyncMock(side_effect=model)
    monkeypatch.setattr(worker, "_execute_request", model_mock)
    monkeypatch.setattr(scheduler_service, "AsyncSessionLocal", database, raising=False)
    monkeypatch.setattr("app.database.AsyncSessionLocal", database)
    monkeypatch.setattr("app.core.engine.wa_outbound_guard.check_wa_outbound_direct_window", AsyncMock(return_value=(True, 0)))
    send = AsyncMock(side_effect=[None, {"messages": [{"id": "sent-id"}]}])
    monkeypatch.setattr("app.core.infra.channel_service.send_message", send)
    await worker.claim_due_replies()
    await worker.run_pending_reply(source.id)
    async with database() as db:
        item = await db.get(PendingAIReply, source.id)
        outbound_id = item.outbound_message_id
        (await db.get(OutboundMessage, outbound_id)).status = "sending"
        await db.commit()
    await scheduler_service._run_outbound_message(outbound_id)
    async with database() as db:
        outbound = await db.get(OutboundMessage, outbound_id)
        assert outbound.status == "queued"
        outbound.status = "sending"
        await db.commit()
    await scheduler_service._run_outbound_message(outbound_id)
    async with database() as db:
        assert (await db.get(OutboundMessage, outbound_id)).status == "sent"
    model_mock.assert_awaited_once()
    assert send.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("during_run", [False, True])
async def test_new_customer_turn_supersedes_recovery(database, monkeypatch, during_run):
    _, session, source, _ = await seed(database)
    async def newer(db, session_id):
        db.add(Message(session_id=session_id, role="user", content="Sudah selesai, terima kasih",
                       timestamp=datetime.now(timezone.utc) + timedelta(seconds=1)))
        await db.flush()
    if during_run:
        async def model(**kwargs):
            return await fake_model(database, after=newer, **kwargs)
        monkeypatch.setattr(worker, "_execute_request", model)
    else:
        async with database() as db:
            await newer(db, session.id)
            await db.commit()
    await worker.claim_due_replies()
    await worker.run_pending_reply(source.id)
    async with database() as db:
        assert (await db.get(PendingAIReply, source.id)).status == "cancelled"
        assert list((await db.execute(select(OutboundMessage))).scalars()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["disabled", "deleted", "expired"])
async def test_recovery_respects_current_service_state(database, block):
    agent, session, source, _ = await seed(database)
    async with database() as db:
        if block == "disabled":
            (await db.get(Session, session.id)).ai_disabled = True
        elif block == "deleted":
            (await db.get(Agent, agent.id)).is_deleted = True
        else:
            (await db.get(PendingAIReply, source.id)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
    await worker.claim_due_replies()
    await worker.run_pending_reply(source.id)
    async with database() as db:
        assert (await db.get(PendingAIReply, source.id)).status == ("expired" if block == "expired" else "cancelled")


@pytest.mark.asyncio
async def test_restart_does_not_repeat_a_run_that_may_have_written_to_tools(database):
    _, session, source, _ = await seed(database)
    async with database() as db:
        item = await db.get(PendingAIReply, source.id)
        item.status = "processing"
        item.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        item.retry_run_id = uuid.uuid4()
        db.add(Run(id=item.retry_run_id, session_id=session.id, status="running"))
        await db.commit()
    assert await worker.claim_due_replies() == [source.id]
    await worker.run_pending_reply(source.id)
    async with database() as db:
        item = await db.get(PendingAIReply, source.id)
        assert item.status == "failed" and item.last_error == "interrupted_retry_requires_review"

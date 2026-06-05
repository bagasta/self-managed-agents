"""Regression test for silent loss of the user's inbound message.

Incident (2026-06-05): user sent a dimsum-website task at night. The run took
long (progress notice "Masih saya proses ya" was sent over WhatsApp) and then
failed/timed-out. The WA caller (channels.py) does `db.rollback()` on
cancel/timeout/error. Because the inbound user message was only flushed (not
committed), the rollback wiped it — the task vanished from history and the next
morning's "lanjut yg pembuatan web" had nothing to continue.

Fix: persist the inbound user message in its own committed transaction BEFORE
the agent graph runs, so a later rollback cannot erase it.
"""
import os
import uuid

import pytest
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

load_dotenv()

from app.core.engine.agent_runner import _persist_inbound_user_message
from app.models.agent import Agent
from app.models.message import Message
from app.models.session import Session

_engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_inbound_user_message_survives_caller_rollback():
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    marker = f"dimsum-{uuid.uuid4().hex[:8]}"

    # Seed agent + session.
    async with _Session() as db:
        db.add(Agent(id=agent_id, name="durability-test"))
        db.add(Session(id=session_id, agent_id=agent_id))
        await db.commit()

    try:
        # Persist the inbound message, then simulate the caller rolling back the
        # transaction after the agent run fails (channels.py cancel/timeout path).
        async with _Session() as db:
            await _persist_inbound_user_message(
                db,
                session_id=session_id,
                run_id=run_id,
                content=marker,
                step_index=0,
            )
            await db.rollback()

        # The message must still be there despite the rollback.
        async with _Session() as db:
            rows = (
                await db.execute(
                    select(Message).where(Message.session_id == session_id, Message.role == "user")
                )
            ).scalars().all()
        assert any(m.content == marker for m in rows), "inbound user message was lost on rollback"
    finally:
        async with _Session() as db:
            await db.execute(Message.__table__.delete().where(Message.session_id == session_id))
            await db.execute(Session.__table__.delete().where(Session.id == session_id))
            await db.execute(Agent.__table__.delete().where(Agent.id == agent_id))
            await db.commit()

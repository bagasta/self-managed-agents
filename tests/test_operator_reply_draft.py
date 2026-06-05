"""Regression: operator's quoted escalation reply must be staged as a draft
deterministically (not left to the LLM).

Incident (2026-06-05): operator replied to an escalation ("5 Menit lagi sampai")
but the agent (gpt-4.1-mini) just said "Baik, saya catat" and never drafted/
forwarded the message to the customer. _maybe_stage_operator_text_draft now
stages the operator's literal text as a pending draft (confirm with 'kirim')
whenever the operator quotes an escalation that resolves to a customer session.
"""
import os
import uuid

import pytest
import structlog
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

load_dotenv()

import app.api.channels as channels
from app.api.channels import _maybe_stage_operator_text_draft
from app.models.agent import Agent
from app.models.session import Session

_engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_LOG = structlog.get_logger()


async def _seed(agent_id, cust_id, op_id, case_id):
    async with _Session() as db:
        db.add(Agent(id=agent_id, name="laundry-esc-test"))
        db.add(
            Session(
                id=cust_id,
                agent_id=agent_id,
                external_user_id="6283890930647",
                channel_type="whatsapp",
                channel_config={"user_phone": "135334989922328@lid", "sender_name": "Wira"},
                metadata_={"escalation_case_id": case_id},
            )
        )
        await db.commit()
        db.add(
            Session(
                id=op_id,
                agent_id=agent_id,
                external_user_id="62895619356936",
                channel_type="whatsapp",
                channel_config={},
                metadata_={},
            )
        )
        await db.commit()


async def _cleanup(agent_id, cust_id, op_id):
    async with _Session() as db:
        await db.execute(Session.__table__.delete().where(Session.agent_id == agent_id))
        await db.execute(Agent.__table__.delete().where(Agent.id == agent_id))
        await db.commit()


@pytest.mark.asyncio
async def test_quoted_escalation_reply_staged_as_draft(monkeypatch):
    sent = []

    async def _fake_send(device_id, target, text):
        sent.append((target, text))
        return "msgid"

    monkeypatch.setattr(channels, "send_wa_message", _fake_send)

    agent_id, cust_id, op_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    case_id = f"esc_1780629788_{uuid.uuid4().hex[:6]}"
    await _seed(agent_id, cust_id, op_id, case_id)

    try:
        async with _Session() as db:
            agent = await db.get(Agent, agent_id)
            op_sess = await db.get(Session, op_id)
            res = await _maybe_stage_operator_text_draft(
                agent=agent,
                operator_session=op_sess,
                quoted_text=f"ESKALASI PESAN DARI CUSTOMER\nID Kasus: {case_id}\nNomor customer: 6283890930647",
                quoted_stanza_id=None,
                operator_message="5 Menit lagi sampai",
                device_id="dev",
                operator_reply_target="62895619356936",
                db=db,
                log=_LOG,
            )
        assert res is not None and res["status"] == "ok"

        async with _Session() as db:
            op_sess = await db.get(Session, op_id)
            pending = (op_sess.metadata_ or {}).get("pending_operator_text_reply")
        assert pending is not None, "operator reply was not staged as a pending draft"
        assert pending["message"] == "5 Menit lagi sampai"
        assert pending["target"] == "135334989922328@lid"
        assert any("5 Menit lagi sampai" in t for _, t in sent), "draft preview not sent to operator"
    finally:
        await _cleanup(agent_id, cust_id, op_id)


@pytest.mark.asyncio
async def test_no_quoted_target_returns_none(monkeypatch):
    monkeypatch.setattr(channels, "send_wa_message", lambda *a, **k: None)
    agent_id, cust_id, op_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed(agent_id, cust_id, op_id, f"esc_1_{uuid.uuid4().hex[:6]}")
    try:
        async with _Session() as db:
            agent = await db.get(Agent, agent_id)
            op_sess = await db.get(Session, op_id)
            # No quote → must defer to the normal agent turn.
            res = await _maybe_stage_operator_text_draft(
                agent=agent,
                operator_session=op_sess,
                quoted_text=None,
                quoted_stanza_id=None,
                operator_message="halo agent",
                device_id="dev",
                operator_reply_target="62895619356936",
                db=db,
                log=_LOG,
            )
        assert res is None
    finally:
        await _cleanup(agent_id, cust_id, op_id)

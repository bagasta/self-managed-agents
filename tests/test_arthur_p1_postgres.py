"""Run only against the explicitly named disposable PostgreSQL container."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models.agent import Agent
from app.models.session import Session
from app.models.run import Run
from app.models.memory import Memory
from app.models.scheduled_job import ScheduledJob
from app.models.outbound_message import OutboundMessage
from arthur_v2.operations import build_operations_tools


@pytest.mark.asyncio
async def test_concurrent_transfers_keep_one_owner_of_the_phone():
    url = os.environ.get("ARTHUR_P1_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("Requires the isolated P1 test database")
    assert "@arthur-p1-test-postgres:5432/arthur_p1_test" in url, "Never run this test against production"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Agent.__table__, Session.__table__, Run.__table__, Memory.__table__, ScheduledJob.__table__, OutboundMessage.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = "6281234567890"
    agents = [Agent(id=uuid.uuid4(), name=f"Isolated {i}", instructions="Original SOP", owner_external_id=owner,
                    wa_inbound_route="ai_staff", version=1, is_deleted=False) for i in range(3)]
    source, target_a, target_b = agents
    phone = "isolated-" + uuid.uuid4().hex
    source.wa_phone_number_id = phone
    source.wa_access_token_encrypted = "test-credential"
    source.wa_connection_type = "cloud_api"
    async with factory() as db:
        db.add_all(agents)
        await db.commit()
    transfer = next(t for t in build_operations_tools(db_factory=factory, owner_phone=owner, self_agent_id=None) if t.name == "transfer_assistant_whatsapp")
    results = await asyncio.gather(*[transfer.ainvoke({"source_agent_id": str(source.id), "target_agent_id": str(target.id), "confirmed": True}) for target in [target_a, target_b]])
    assert sum(bool(result.get("ok")) for result in results) == 1
    async with factory() as db:
        owners = (await db.execute(select(Agent.id).where(Agent.wa_phone_number_id == phone))).scalars().all()
        assert len(owners) == 1 and owners[0] in {target_a.id, target_b.id}
        assert (await db.get(Agent, source.id)).wa_phone_number_id is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_configuration_updates_have_monotonic_revisions():
    url = os.environ.get("ARTHUR_P1_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("Requires isolated PostgreSQL")
    assert "@arthur-p1-test-postgres:5432/arthur_p1_test" in url
    from app.core.domain.agent_revision_service import ADMIN_SCOPE, decode_archive
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine,expire_on_commit=False)
    agent=Agent(id=uuid.uuid4(),name="Before",instructions="Before SOP",owner_external_id="6281234567890",version=1)
    async with factory() as db:
        db.add(agent);await db.commit()
    loaded=asyncio.Event()
    arrivals=0
    async def change(field,value):
        nonlocal arrivals
        async with factory() as db:
            row=await db.get(Agent,agent.id)
            arrivals+=1
            if arrivals==2:loaded.set()
            await loaded.wait()
            setattr(row,field,value)
            row.version+=1
            await db.commit()
    await asyncio.gather(change("name","After"),change("instructions","After SOP"))
    async with factory() as db:
        row=await db.get(Agent,agent.id)
        assert row.version==3 and row.name=="After" and row.instructions=="After SOP"
        records=(await db.execute(select(Memory).where(Memory.agent_id==agent.id,Memory.scope==ADMIN_SCOPE))).scalars().all()
        revisions=[decode_archive(r.value_data) for r in records]
        latest=next(r for r in revisions if r["stage"]=="after" and r["version"]==3)
        assert latest["snapshot"]["name"]=="After" and latest["snapshot"]["instructions"]=="After SOP"
    await engine.dispose()

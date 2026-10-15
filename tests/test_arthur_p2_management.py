import copy
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from test_arthur_p1_operations import database, agent_row, conversation, OWNER
from app.models.agent import Agent
from app.models.session import Session
from app.models.memory import Memory
from app.models.message import Message
from app.models.scheduled_job import ScheduledJob
from app.models.outbound_message import OutboundMessage
from app.core.domain.agent_service_controls import CONTROL_KEY, service_block_reason, is_agent_paused
from app.core.domain.agent_revision_service import ADMIN_SCOPE, decode_archive
from app.core.engine.context_service import load_history
from arthur_v2 import service_management as service, memory_revision_tools as memory_tools
from arthur_v2.plugin import build_arthur_v2_tools

CUSTOMER = "6282222222222"
OTHER = "6283333333333"


@pytest.fixture(autouse=True)
def archive_test_key(monkeypatch):
    monkeypatch.setenv("CHANNEL_SECRET_KEY", "isolated-p2-test-encryption-key")


def tools(factory, owner=OWNER):
    return {t.name: t for t in build_arthur_v2_tools(db_factory=factory, owner_phone=owner, self_agent_id=None)}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("set_assistant_paused", {"agent_id": str(uuid.uuid4()), "paused": True}),
    ("configure_assistant_access", {"agent_id": str(uuid.uuid4()), "customer_access": "all"}),
    ("manage_assistant_google_connection", {"agent_id": str(uuid.uuid4()), "action": "disconnect"}),
    ("set_assistant_google_spreadsheet", {"agent_id": str(uuid.uuid4()), "spreadsheet_id": "new_sheet_identifier", "expected_version": 1}),
    ("edit_assistant_memory", {"agent_id": str(uuid.uuid4()), "memory_id": str(uuid.uuid4()), "expected_etag": "x", "action": "delete"}),
    ("reset_assistant_context", {"agent_id": str(uuid.uuid4()), "scope": CUSTOMER, "expected_count": 1}),
])
async def test_new_mutations_require_confirmation_without_db(name, args):
    result = await tools(None)[name].ainvoke(args)
    assert result["needs_confirmation"] and not result["ok"]


@pytest.mark.asyncio
async def test_pause_preserves_connections_and_only_resumes_service(database):
    agent = await agent_row(database, cloud=True)
    session, _ = await conversation(database, agent)
    async with database() as db:
        db.add(ScheduledJob(agent_id=agent.id, session_id=session.id, label="Schedule", payload="Original", status="active"))
        db.add(OutboundMessage(agent_id=agent.id, session_id=session.id, target=CUSTOMER, text="queued", status="queued", available_at=datetime.now(timezone.utc)))
        await db.commit()
    op = tools(database)["set_assistant_paused"]
    result = await op.ainvoke({"agent_id": str(agent.id), "paused": True, "confirmed": True})
    assert result["ok"] and result["scheduled_jobs_paused"] == 1 and result["queued_messages_cancelled"] == 1
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        assert saved.wa_phone_number_id == "phone-1" and saved.instructions == "Existing SOP" and is_agent_paused(saved)
    result = await op.ainvoke({"agent_id": str(agent.id), "paused": False, "confirmed": True})
    assert result["ok"] and result["scheduled_jobs_resumed"] == 0
    async with database() as db:
        assert not is_agent_paused(await db.get(Agent, agent.id))
        assert (await db.execute(select(ScheduledJob.status))).scalar_one() == "paused"
        assert (await db.execute(select(OutboundMessage.status))).scalar_one() == "cancelled"


@pytest.mark.asyncio
async def test_pause_rejects_busy_agent(database):
    agent = await agent_row(database)
    await conversation(database, agent, running=True)
    result = await tools(database)["set_assistant_paused"].ainvoke({"agent_id": str(agent.id), "paused": True, "confirmed": True})
    assert not result["ok"]
    async with database() as db:
        assert not is_agent_paused(await db.get(Agent, agent.id))


@pytest.mark.asyncio
async def test_runtime_paused_guard_has_no_llm_or_db_calls():
    from app.core.engine.agent_runner import run_agent
    agent = SimpleNamespace(tools_config={CONTROL_KEY: {"paused": True}})
    session = SimpleNamespace(agent_id=uuid.uuid4(), channel_type="whatsapp", external_user_id=CUSTOMER)
    result = await run_agent(agent_model=agent, session=session, user_message="Test", db=None)
    assert result["status"] == "agent_paused" and result["tokens_used"] == 0 and result["reply"] == ""


@pytest.mark.parametrize("allowed,sender,expected", [(None,CUSTOMER,None),([CUSTOMER],CUSTOMER,None),([CUSTOMER],OTHER,"sender_not_allowed"),([],CUSTOMER,"sender_not_allowed"),([],OWNER,None)])
def test_managed_admission_does_not_restrict_google_tools_by_role(allowed,sender,expected):
    agent = SimpleNamespace(tools_config={CONTROL_KEY: {"managed_access": True}}, owner_external_id=OWNER, operator_ids=[OWNER], escalation_config={}, allowed_senders=allowed)
    assert service_block_reason(agent, sender, whatsapp=True) == expected


def test_legacy_agents_keep_existing_admission_behavior():
    agent = SimpleNamespace(tools_config={}, allowed_senders=[], owner_external_id=OWNER, operator_ids=[])
    assert service_block_reason(agent, CUSTOMER, whatsapp=True) is None


@pytest.mark.asyncio
async def test_access_all_preserves_workflow_and_owner_and_removes_legacy_operator(database):
    agent = await agent_row(database)
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        saved.operator_ids = [OWNER, OTHER]
        saved.escalation_config = {"operator_phone": OTHER, "keep": "config"}
        await db.commit()
    result = await tools(database)["configure_assistant_access"].ainvoke({"agent_id": str(agent.id), "customer_access": "all", "remove_operators": [OTHER], "expected_version": 8, "confirmed": True})
    assert result["ok"] and result["operators"] == [OWNER] and result["allowed_customers"] is None
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        assert service_block_reason(saved, CUSTOMER, whatsapp=True) is None
        assert saved.tools_config["mcp"] == agent.tools_config["mcp"]
        assert saved.escalation_config == {"keep": "config"}


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"remove_operators": [OWNER]}, {"customer_access": "allowlist", "allowed_customers": []}, {"add_operators": ["not-a-phone"]}, {"expected_version": 6, "customer_access": "all"}])
async def test_access_invalid_change_preserves_current_state(database, args):
    agent = await agent_row(database)
    payload = {"agent_id": str(agent.id), "expected_version": 7, "confirmed": True, **args}
    result = await tools(database)["configure_assistant_access"].ainvoke(payload)
    assert not result["ok"]
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        assert saved.operator_ids == [OWNER] and saved.version == 7


@pytest.mark.asyncio
async def test_scoped_memory_edit_etag_and_admin_archive_protection(database):
    agent = await agent_row(database)
    async with database() as db:
        row = Memory(agent_id=agent.id, scope=CUSTOMER, key="project", value_data="Wrong fact")
        other = Memory(agent_id=agent.id, scope=OTHER, key="project", value_data="Other customer's fact")
        db.add_all([row,other]);await db.commit()
    ops = tools(database)
    inspected = await ops["inspect_assistant_memory"].ainvoke({"agent_id": str(agent.id), "scope": CUSTOMER})
    assert inspected["total"] == 1
    result = await ops["edit_assistant_memory"].ainvoke({"agent_id": str(agent.id), "memory_id": str(row.id), "expected_etag": "wrong", "action": "delete", "confirmed": True})
    assert not result["ok"]
    result = await ops["edit_assistant_memory"].ainvoke({"agent_id": str(agent.id), "memory_id": str(row.id), "expected_etag": inspected["memories"][0]["etag"], "action": "set", "value": "Correct fact", "confirmed": True})
    assert result["ok"]
    async with database() as db:
        assert (await db.get(Memory, row.id)).value_data == "Correct fact"
        assert (await db.get(Memory, other.id)).value_data == "Other customer's fact"
        archive = (await db.execute(select(Memory).where(Memory.scope == ADMIN_SCOPE))).scalar_one()
        assert decode_archive(archive.value_data)["value"] == "Wrong fact"
        aid = str(archive.id)
    result = await ops["edit_assistant_memory"].ainvoke({"agent_id": str(agent.id), "memory_id": aid, "expected_etag": "x", "action": "delete", "confirmed": True})
    assert not result["ok"]


@pytest.mark.asyncio
async def test_reset_keeps_other_customers_global_memory_and_chat_archive(database):
    agent = await agent_row(database)
    session, _ = await conversation(database, agent)
    async with database() as db:
        s = await db.get(Session, session.id);s.external_user_id = CUSTOMER;s.metadata_ = {"context_summary":"old", "keep":"metadata"}
        db.add_all([Memory(agent_id=agent.id, scope=CUSTOMER, key="longterm", value_data="old project"),
                    Memory(agent_id=agent.id, scope=OTHER, key="longterm", value_data="other project"),
                    Memory(agent_id=agent.id, scope=None, key="soul", value_data="identity")])
        await db.commit()
    result = await tools(database)["reset_assistant_context"].ainvoke({"agent_id": str(agent.id), "scope": CUSTOMER, "expected_count": 1, "confirmed": True})
    assert result["ok"] and result["sessions_reset"] == 1 and not result["chat_history_deleted"]
    async with database() as db:
        rows = (await db.execute(select(Memory).where(Memory.scope != ADMIN_SCOPE))).scalars().all()
        assert any(m.scope == OTHER for m in rows) and not any(m.scope == CUSTOMER for m in rows)
        assert (await db.execute(select(Memory.value_data).where(Memory.scope.is_(None)))).scalar_one() == "identity"
        s = await db.get(Session, session.id)
        assert "context_summary" not in s.metadata_ and s.metadata_["keep"] == "metadata"
        assert (await db.execute(select(Message.content))).scalar_one() == "Jawaban asli peserta"


@pytest.mark.asyncio
async def test_history_reset_filters_before_limit_and_does_not_resurrect_old_steps(database):
    agent = await agent_row(database)
    session, _ = await conversation(database, agent)
    boundary = datetime.now(timezone.utc)
    async with database() as db:
        db.add_all([Message(session_id=session.id, role="user", content="Old high step", step_index=1000, timestamp=boundary-timedelta(minutes=1)),
                    Message(session_id=session.id, role="user", content="New project", step_index=0, timestamp=boundary+timedelta(seconds=1))])
        await db.commit()
        rows = await load_history(session.id, db, max_turns=1, after=boundary)
        assert [m.content for m in rows] == ["New project"]


@pytest.mark.asyncio
async def test_revision_is_atomic_encrypted_and_rollback_is_a_new_version(database):
    agent = await agent_row(database)
    async with database() as db:
        saved = await db.get(Agent, agent.id);saved.instructions="New SOP";saved.version=8;await db.commit()
    ops = tools(database)
    history = await ops["list_assistant_revisions"].ainvoke({"agent_id": str(agent.id)})
    before = next(r for r in history["revisions"] if r["stage"] == "before")
    payload = {"agent_id": str(agent.id), "revision_id": before["revision_id"], "expected_version":8,"fields":["instructions"]}
    preview = await ops["rollback_assistant_revision"].ainvoke(payload)
    assert preview["needs_confirmation"] and preview["changes"]["instructions"]["after"] == "Existing SOP"
    result = await ops["rollback_assistant_revision"].ainvoke({**payload,"confirmed":True})
    assert result["ok"] and result["version"] == 9
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        assert saved.instructions == "Existing SOP" and saved.owner_external_id == OWNER
        revision = await db.get(Memory, uuid.UUID(before["revision_id"]))
        assert revision.scope == ADMIN_SCOPE and revision.value_data.startswith("enc:")


@pytest.mark.asyncio
async def test_quota_updates_do_not_create_revision_noise(database):
    agent = await agent_row(database)
    async with database() as db:
        saved = await db.get(Agent, agent.id);saved.tokens_used=10;await db.commit()
        assert not (await db.execute(select(Memory).where(Memory.scope == ADMIN_SCOPE))).scalars().all()


def test_config_rollback_preserves_live_secrets_pause_and_access_controls():
    previous = {"scheduler":False,"mcp":{"servers":{"google_workspace":{"headers":{"Authorization":"OLD"},"allowed_services":["sheets"]}}}}
    current = {"scheduler":True,CONTROL_KEY:{"paused":True,"managed_access":True},"mcp":{"servers":{"google_workspace":{"headers":{"Authorization":"LIVE"},"allowed_services":["sheets","docs"]}}}}
    restored = memory_tools.restore_config_without_credentials(previous,current)
    assert not restored["scheduler"] and restored[CONTROL_KEY] == current[CONTROL_KEY]
    assert restored["mcp"]["servers"]["google_workspace"]["headers"]["Authorization"] == "LIVE"


@pytest.mark.asyncio
async def test_google_target_changes_metadata_and_known_instruction_reference_only(database, monkeypatch):
    agent = await agent_row(database)
    async with database() as db:
        saved=await db.get(Agent,agent.id);saved.instructions="Write to sheet-owned";await db.commit()
    @asynccontextmanager
    async def fake(*a):
        yield {"get_spreadsheet_info": SimpleNamespace(ainvoke=AsyncMock(return_value='Spreadsheet: "New" (ID: new_sheet_identifier)'))}
    monkeypatch.setattr(service,"google_tools",fake)
    result=await tools(database)["set_assistant_google_spreadsheet"].ainvoke({"agent_id":str(agent.id),"spreadsheet_id":"new_sheet_identifier","expected_version":8,"confirmed":True})
    assert result["ok"] and result["spreadsheet_writes_executed"] == 0
    async with database() as db:
        saved=await db.get(Agent,agent.id)
        assert saved.tools_config["google_workspace_resources"]["default_spreadsheet_id"] == "new_sheet_identifier"
        assert saved.instructions == "Write to new_sheet_identifier"


@pytest.mark.asyncio
async def test_google_disconnect_is_agent_scoped_and_preserves_other_connectors(database, monkeypatch):
    agent=await agent_row(database)
    async with database() as db:
        saved=await db.get(Agent,agent.id);cfg=copy.deepcopy(saved.tools_config);cfg["mcp"]["servers"]["other"]={"url":"https://other.invalid"};saved.tools_config=cfg;await db.commit()
    calls=[]
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def delete(self,url,**kw):
            calls.append((url,kw["params"]))
            return httpx.Response(200,json={"disconnected":True},request=httpx.Request("DELETE",url))
    monkeypatch.setattr(httpx,"AsyncClient",Client)
    result=await tools(database)["manage_assistant_google_connection"].ainvoke({"agent_id":str(agent.id),"action":"disconnect","confirmed":True})
    assert result["ok"] and calls[0][1] == {"external_user_id":OWNER,"agent_id":str(agent.id)}
    async with database() as db:
        saved=await db.get(Agent,agent.id)
        assert "google_workspace" not in saved.tools_config["mcp"]["servers"]
        assert saved.tools_config["mcp"]["servers"]["other"]["url"] == "https://other.invalid"
        backup=(await db.execute(select(Memory).where(Memory.key=="google_connector_backup"))).scalar_one()
        assert backup.scope == ADMIN_SCOPE


@pytest.mark.asyncio
async def test_pending_run_refreshes_pause_state_before_any_model_call(database):
    from app.core.engine.agent_runner import run_agent
    agent=await agent_row(database)
    session,_=await conversation(database,agent)
    async with database() as stale_db:
        stale_agent=await stale_db.get(Agent,agent.id)
        stale_session=await stale_db.get(Session,session.id)
        result=await tools(database)["set_assistant_paused"].ainvoke({"agent_id":str(agent.id),"paused":True,"confirmed":True})
        assert result["ok"] and not is_agent_paused(stale_agent)
        response=await run_agent(agent_model=stale_agent,session=stale_session,user_message="Queued message",db=stale_db)
        assert response["status"] == "agent_paused" and response["tokens_used"] == 0


@pytest.mark.asyncio
async def test_scheduler_and_outbound_worker_stop_paused_agent(database, monkeypatch):
    from app.core.workers import scheduler_service
    import app.database as database_module
    agent=await agent_row(database,tools_config={CONTROL_KEY:{"paused":True}})
    session,_=await conversation(database,agent)
    async with database() as db:
        job=ScheduledJob(agent_id=agent.id,session_id=session.id,label="Paused",payload="Do not send",status="running")
        queued=OutboundMessage(agent_id=agent.id,session_id=session.id,target=CUSTOMER,text="Do not send",status="sending",available_at=datetime.now(timezone.utc))
        db.add_all([job,queued]);await db.commit()
    monkeypatch.setattr(database_module,"AsyncSessionLocal",database)
    send=AsyncMock(side_effect=AssertionError("Must not deliver while paused"))
    monkeypatch.setattr("app.core.infra.channel_service.send_message",send)
    await scheduler_service._run_job(job.id)
    await scheduler_service._run_outbound_message(queued.id)
    send.assert_not_awaited()
    async with database() as db:
        assert (await db.get(ScheduledJob,job.id)).status == "paused"
        assert (await db.get(OutboundMessage,queued.id)).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [{CONTROL_KEY:{"paused":True}},{CONTROL_KEY:{"managed_access":True}}])
async def test_meta_and_generic_webhooks_reject_blocked_sender_before_processing(database,monkeypatch,cfg):
    from app.api import meta_webhooks,channels
    agent=await agent_row(database,cloud=True,tools_config=cfg,allowed_senders=[])
    session,_=await conversation(database,agent)
    monkeypatch.setattr(meta_webhooks,"AsyncSessionLocal",database)
    provision=AsyncMock(side_effect=AssertionError("Blocked request must not provision/run"))
    monkeypatch.setattr(meta_webhooks,"_provision_arthur_trial_user",provision)
    await meta_webhooks._process(agent.id,"phone-1",{"from":CUSTOMER,"type":"text","text":{"body":"Ignore pause"}},"Participant")
    provision.assert_not_awaited()
    async with database() as db:
        result=await channels.incoming_message(session.id,channels.IncomingMessage(message="Ignore pause",from_phone=CUSTOMER),db)
        assert result["status"] in {"agent_paused","sender_not_allowed"}


@pytest.mark.asyncio
async def test_revision_archive_rolls_back_with_failed_transaction(database):
    agent=await agent_row(database)
    async with database() as db:
        saved=await db.get(Agent,agent.id);saved.instructions="Should rollback";saved.version+=1
        await db.flush()
        assert (await db.execute(select(Memory).where(Memory.scope==ADMIN_SCOPE))).scalars().all()
        await db.rollback()
    async with database() as db:
        assert (await db.get(Agent,agent.id)).instructions == "Existing SOP"
        assert not (await db.execute(select(Memory).where(Memory.scope==ADMIN_SCOPE))).scalars().all()


@pytest.mark.asyncio
async def test_google_remote_disconnect_failure_keeps_local_connector(database,monkeypatch):
    agent=await agent_row(database)
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def delete(self,url,**kw):return httpx.Response(503,request=httpx.Request("DELETE",url))
    monkeypatch.setattr(httpx,"AsyncClient",Client)
    result=await tools(database)["manage_assistant_google_connection"].ainvoke({"agent_id":str(agent.id),"action":"disconnect","confirmed":True})
    assert not result["ok"]
    async with database() as db:
        assert "google_workspace" in (await db.get(Agent,agent.id)).tools_config["mcp"]["servers"]


@pytest.mark.asyncio
async def test_cross_owner_cannot_inspect_memory_or_revisions_or_pause(database):
    agent=await agent_row(database,owner=OTHER)
    for name,args in [("inspect_assistant_memory",{"scope":CUSTOMER}),("list_assistant_revisions",{}),("set_assistant_paused",{"paused":True,"confirmed":True}),("get_assistant_access",{})]:
        result=await tools(database)[name].ainvoke({"agent_id":str(agent.id),**args})
        assert not result["ok"]


@pytest.mark.asyncio
async def test_resume_schedules_requires_opt_in_and_does_not_revive_manual_pauses(database):
    agent=await agent_row(database)
    session,_=await conversation(database,agent)
    async with database() as db:
        active=ScheduledJob(agent_id=agent.id,session_id=session.id,label="Recurring",payload="Approved",status="active",cron_expr="*/5 * * * *")
        manual=ScheduledJob(agent_id=agent.id,session_id=session.id,label="Manual",payload="Keep paused",status="paused",cron_expr="*/5 * * * *")
        db.add_all([active,manual]);await db.commit()
    op=tools(database)["set_assistant_paused"]
    assert (await op.ainvoke({"agent_id":str(agent.id),"paused":True,"confirmed":True}))["ok"]
    assert (await op.ainvoke({"agent_id":str(agent.id),"paused":False,"confirmed":True}))["scheduled_jobs_resumed"]==0
    result=await op.ainvoke({"agent_id":str(agent.id),"paused":False,"resume_schedules":True,"confirmed":True})
    assert result["ok"] and result["scheduled_jobs_resumed"]==1
    async with database() as db:
        assert (await db.get(ScheduledJob,active.id)).status=="active"
        assert (await db.get(ScheduledJob,manual.id)).status=="paused"


@pytest.mark.asyncio
async def test_phone_scope_variants_are_one_customer_without_other_scope_leak(database):
    agent=await agent_row(database)
    async with database() as db:
        db.add_all([Memory(agent_id=agent.id,scope=CUSTOMER,key="project",value_data="Canonical"),
                    Memory(agent_id=agent.id,scope="+"+CUSTOMER,key="project",value_data="Legacy"),
                    Memory(agent_id=agent.id,scope=OTHER,key="project",value_data="Other customer")])
        await db.commit()
    result=await tools(database)["inspect_assistant_memory"].ainvoke({"agent_id":str(agent.id),"scope":CUSTOMER})
    assert result["ok"] and result["total"]==2


@pytest.mark.asyncio
async def test_reminders_do_not_bypass_managed_customer_admission(database,monkeypatch):
    from app.core.workers import scheduler_service
    import app.database as database_module
    agent=await agent_row(database,tools_config={CONTROL_KEY:{"managed_access":True}},allowed_senders=[])
    session,_=await conversation(database,agent)
    async with database() as db:
        job=ScheduledJob(agent_id=agent.id,session_id=session.id,label="Blocked customer",payload="Do not deliver",status="running")
        db.add(job);await db.commit()
    monkeypatch.setattr(database_module,"AsyncSessionLocal",database)
    await scheduler_service._run_job(job.id)
    async with database() as db:
        assert (await db.get(ScheduledJob,job.id)).status=="paused"

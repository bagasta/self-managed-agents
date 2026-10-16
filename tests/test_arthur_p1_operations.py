import copy
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent import Agent
from app.models.session import Session
from app.models.run import Run
from app.models.message import Message
from app.models.memory import Memory
from app.models.scheduled_job import ScheduledJob
from app.models.outbound_message import OutboundMessage
from arthur_v2 import plugin, operations, run_recovery
from arthur_v2.operations_common import OperationRejected, range_shape, read_values


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return "JSON"


OWNER = "6281234567890"


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    # Existing business-operation tests are offline. Writer/provider behavior is
    # exercised separately in test_arthur_instruction_writer.py.
    async def offline_writer(**kwargs):
        return kwargs["instructions"]
    monkeypatch.setattr(plugin, "compose_assistant_instructions", offline_writer)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/operations.db")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Agent.__table__, Session.__table__, Run.__table__, Message.__table__, Memory.__table__, ScheduledJob.__table__, OutboundMessage.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def agent_row(factory, *, owner=OWNER, cloud=False, device=None, **extra):
    agent = Agent(id=uuid.uuid4(), name="Agent", instructions="Existing SOP", owner_external_id=owner,
                  operator_ids=[owner], version=7, is_deleted=False, wa_device_id=device,
                  wa_phone_number_id="phone-1" if cloud else None,
                  wa_access_token_encrypted="enc:private" if cloud else None,
                  wa_waba_id="waba" if cloud else None, wa_cloud_api_mode="coexistence" if cloud else None,
                  wa_connection_type="cloud_api" if cloud else "legacy_qr" if device else None,
                  wa_inbound_route="ai_staff", channel_type="whatsapp" if cloud or device else None,
                  tools_config={"custom": {"keep": True}, "mcp": {"servers": {"google_workspace": {"allowed_services": ["sheets"], "allowed_operations": {"sheets": ["read", "write"]}}}}, "google_workspace_resources": {"default_spreadsheet_id": "sheet-owned"}})
    for key, value in extra.items():
        setattr(agent, key, value)
    async with factory() as db:
        db.add(agent)
        await db.commit()
    return agent


def tools(factory, self_id=None):
    return {t.name: t for t in operations.build_operations_tools(db_factory=factory, owner_phone=OWNER, self_agent_id=self_id)}


async def conversation(factory, agent, *, running=False):
    session = Session(id=uuid.uuid4(), agent_id=agent.id, channel_type="whatsapp", external_user_id="customer",
                      channel_config={"device_id": agent.wa_device_id or "old", "meta_access_token": "enc:old", "meta_phone_number_id": "old", "sender_name": "Participant"})
    run = Run(id=uuid.uuid4(), session_id=session.id, status="running" if running else "completed",
              runtime_metadata={"reply_guard_reason": "fallback_empty_reply"}, started_at=datetime.now(timezone.utc))
    async with factory() as db:
        db.add(session)
        await db.flush()
        db.add(run)
        db.add(Message(id=uuid.uuid4(), session_id=session.id, run_id=run.id, role="user", content="Jawaban asli peserta"))
        await db.commit()
    return session, run


@pytest.mark.asyncio
async def test_old_sixteen_tools_preserved_and_six_added():
    names = [t.name for t in plugin.build_arthur_v2_tools(db_factory=None, owner_phone=OWNER, self_agent_id=None)]
    assert names[:16] == ["list_managed_assistants", "get_current_plan", "inspect_managed_assistant", "get_assistant_automation_status", "create_assistant", "configure_assistant_autonomous_run", "stop_assistant_automation", "update_assistant", "add_assistant_knowledge", "delete_assistant", "configure_assistant_runtime", "get_payment_link", "start_assistant_google_oauth", "connect_assistant_whatsapp_cloud", "get_assistant_whatsapp_status", "create_demo_whatsapp_trial"]
    assert names[16:22] == ["disconnect_assistant_whatsapp", "transfer_assistant_whatsapp", "diagnose_assistant", "test_assistant_google_tools", "prepare_assistant_run_recovery", "recover_assistant_run"]
    assert len(names) == len(set(names)) == 35
    assert names[-3:] == ["list_assistant_knowledge", "read_assistant_knowledge", "inspect_assistant_history"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("disconnect_assistant_whatsapp", {"agent_id": str(uuid.uuid4())}),
    ("transfer_assistant_whatsapp", {"source_agent_id": str(uuid.uuid4()), "target_agent_id": str(uuid.uuid4())}),
    ("recover_assistant_run", {"run_id": str(uuid.uuid4()), "plan_token": "unapproved"}),
])
async def test_mutation_requires_confirmation_without_touching_db(name, args):
    result = await tools(None)[name].ainvoke(args)
    assert result["needs_confirmation"] and not result["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [True, False])
async def test_transfer_preserves_agent_configuration_and_exclusive_keys(database, cloud):
    source = await agent_row(database, cloud=cloud, device=None if cloud else "legacy-device")
    target = await agent_row(database)
    before = copy.deepcopy(source.tools_config)
    session, _ = await conversation(database, source)
    async with database() as db:
        db.add(ScheduledJob(agent_id=source.id, session_id=session.id, label="Existing schedule", payload="Original SOP", status="active"))
        db.add(OutboundMessage(agent_id=source.id, session_id=session.id, target="customer", text="Old queued reply", status="queued", available_at=datetime.now(timezone.utc)))
        await db.commit()
    result = await tools(database)["transfer_assistant_whatsapp"].ainvoke({"source_agent_id": str(source.id), "target_agent_id": str(target.id), "confirmed": True})
    assert result["ok"] and result["queued_messages_cancelled"] == 1 and result["scheduled_jobs_paused"] == 1
    async with database() as db:
        old, new = await db.get(Agent, source.id), await db.get(Agent, target.id)
        assert old.wa_phone_number_id is None and old.wa_device_id is None
        assert new.wa_phone_number_id == ("phone-1" if cloud else None)
        assert new.wa_device_id == (None if cloud else "legacy-device")
        assert old.instructions == new.instructions == "Existing SOP"
        assert old.tools_config == new.tools_config == before
        assert new.wa_cloud_api_mode == ("coexistence" if cloud else None)
        s = await db.get(Session, session.id)
        assert s.agent_id == source.id and s.channel_type is None
        assert s.channel_config == {"sender_name": "Participant"}
        assert (await db.execute(select(Message.content))).scalar_one() == "Jawaban asli peserta"
        assert (await db.execute(select(ScheduledJob.status))).scalar_one() == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["foreign_target", "target_connected", "running", "self", "n8n"])
async def test_transfer_rejects_without_releasing_source(database, reason):
    source = await agent_row(database, cloud=True)
    target = await agent_row(database, owner="628999999999" if reason == "foreign_target" else OWNER,
                             device="already-bound" if reason == "target_connected" else None,
                             wa_inbound_route="n8n" if reason == "n8n" else "ai_staff")
    if reason == "running":
        await conversation(database, source, running=True)
    result = await tools(database, str(source.id) if reason == "self" else None)["transfer_assistant_whatsapp"].ainvoke({"source_agent_id": str(source.id), "target_agent_id": str(target.id), "confirmed": True})
    assert not result["ok"]
    async with database() as db:
        assert (await db.get(Agent, source.id)).wa_phone_number_id == "phone-1"


@pytest.mark.asyncio
async def test_failed_legacy_disconnect_retains_binding(database, monkeypatch):
    from app.core.infra import wa_client
    monkeypatch.setattr(wa_client, "delete_wa_device", AsyncMock(side_effect=RuntimeError("secret-must-not-leak")))
    agent = await agent_row(database, device="device")
    result = await tools(database)["disconnect_assistant_whatsapp"].ainvoke({"agent_id": str(agent.id), "confirmed": True})
    assert not result["ok"] and "secret-must-not-leak" not in str(result)
    async with database() as db:
        assert (await db.get(Agent, agent.id)).wa_device_id == "device"


@pytest.mark.asyncio
async def test_cloud_disconnect_idempotent_and_no_meta_deregistration(database):
    agent = await agent_row(database, cloud=True)
    op = tools(database)["disconnect_assistant_whatsapp"]
    first = await op.ainvoke({"agent_id": str(agent.id), "confirmed": True})
    second = await op.ainvoke({"agent_id": str(agent.id), "confirmed": True})
    assert first["ok"] and second["already_disconnected"]
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        assert saved.version == 8 and not saved.is_deleted and saved.instructions == "Existing SOP"


@pytest.mark.asyncio
async def test_transfer_rolls_back_source_when_target_update_fails(database):
    source = await agent_row(database, cloud=True)
    target = await agent_row(database)
    async with database() as db:
        await db.execute(text(f"CREATE TRIGGER reject_target BEFORE UPDATE ON agents WHEN NEW.id = '{target.id.hex}' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"))
        await db.commit()
    result = await tools(database)["transfer_assistant_whatsapp"].ainvoke({"source_agent_id": str(source.id), "target_agent_id": str(target.id), "confirmed": True})
    assert not result["ok"]
    async with database() as db:
        assert (await db.get(Agent, source.id)).wa_phone_number_id == "phone-1"
        assert (await db.get(Agent, target.id)).wa_phone_number_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "sending"])
async def test_disconnect_handles_queue_without_sending(database, state):
    agent = await agent_row(database, cloud=True)
    session, _ = await conversation(database, agent)
    async with database() as db:
        db.add(OutboundMessage(agent_id=agent.id, session_id=session.id, target="recipient", text="Pending", status=state, available_at=datetime.now(timezone.utc)))
        await db.commit()
    result = await tools(database)["disconnect_assistant_whatsapp"].ainvoke({"agent_id": str(agent.id), "confirmed": True})
    assert result["ok"] == (state == "queued")
    async with database() as db:
        assert ((await db.get(Agent, agent.id)).wa_phone_number_id is None) == (state == "queued")


class SheetTools:
    def __init__(self):
        self.cells = {"Jawaban!A2:B2": [["", ""]], "Arthur_Test!A1": [[""]]}
        self.writes = []
        self.fail_after_write = False

    def as_dict(self):
        async def read(args):
            values = self.cells[args["range_name"]]
            return [{"type": "text", "text": "Successfully read " + str(len(values)) + " rows:\n" + "\n".join(f"Row {i+1}: {row!r}" for i, row in enumerate(values))}]
        async def write(args):
            assert args["spreadsheet_id"] == "sheet-owned"
            assert args["value_input_option"] == "RAW"
            self.writes.append(copy.deepcopy(args))
            self.cells[args["range_name"]] = copy.deepcopy(args["values"])
            if self.fail_after_write:
                self.fail_after_write = False
                raise TimeoutError("uncertain-provider-response")
            return "Successfully updated cells"
        return {"read_sheet_values": SimpleNamespace(ainvoke=read), "modify_sheet_values": SimpleNamespace(ainvoke=write)}


@pytest.fixture
def sheets(monkeypatch):
    state = SheetTools()
    @asynccontextmanager
    async def fake(*args):
        yield state.as_dict()
    monkeypatch.setattr(operations, "google_tools", fake)
    monkeypatch.setattr(run_recovery, "google_tools", fake)
    return state


@pytest.mark.asyncio
async def test_probe_default_read_has_no_writes(database, sheets):
    agent = await agent_row(database)
    result = await tools(database)["test_assistant_google_tools"].ainvoke({"agent_id": str(agent.id), "range_name": "Jawaban!A2:B2"})
    assert result["read_verified"] and not result["write_verified"] and "values" not in result
    assert not sheets.writes


@pytest.mark.asyncio
async def test_probe_writes_only_isolated_blank_cell_and_cleans_marker(database, sheets):
    agent = await agent_row(database)
    op = tools(database)["test_assistant_google_tools"]
    for args in [{"confirmed": False, "range_name": "Arthur_Test!A1"}, {"confirmed": True, "range_name": "Jawaban!A2"}]:
        result = await op.ainvoke({"agent_id": str(agent.id), "mode": "write_test", **args})
        assert not result["ok"] and not sheets.writes
    result = await op.ainvoke({"agent_id": str(agent.id), "mode": "write_test", "confirmed": True, "range_name": "Arthur_Test!A1"})
    assert result["ok"] and result["cleanup_verified"]
    assert sheets.cells["Arthur_Test!A1"] == [[""]] and len(sheets.writes) == 2


@pytest.mark.asyncio
async def test_probe_does_not_overwrite_existing_test_data(database, sheets):
    agent = await agent_row(database)
    sheets.cells["Arthur_Test!A1"] = [["Someone else's data"]]
    result = await tools(database)["test_assistant_google_tools"].ainvoke({"agent_id": str(agent.id), "range_name": "Arthur_Test!A1", "mode": "write_test", "confirmed": True})
    assert not result["ok"] and not sheets.writes


@pytest.mark.asyncio
async def test_probe_timeout_cleans_only_own_marker(database, sheets):
    agent = await agent_row(database)
    sheets.fail_after_write = True
    result = await tools(database)["test_assistant_google_tools"].ainvoke({"agent_id": str(agent.id), "range_name": "Arthur_Test!A1", "mode": "write_test", "confirmed": True})
    assert not result["ok"]
    assert sheets.cells["Arthur_Test!A1"] == [[""]]


@pytest.mark.asyncio
async def test_foreign_owner_cannot_probe_or_diagnose(database, sheets):
    agent = await agent_row(database, owner="628999999999")
    for name, args in [("diagnose_assistant", {}), ("test_assistant_google_tools", {"range_name": "Jawaban!A2:B2"})]:
        result = await tools(database)[name].ainvoke({"agent_id": str(agent.id), **args})
        assert not result["ok"] and not sheets.writes


@pytest.mark.asyncio
async def test_readonly_policy_prevents_probe_writes(database, sheets):
    agent = await agent_row(database)
    async with database() as db:
        row = await db.get(Agent, agent.id)
        cfg = copy.deepcopy(row.tools_config)
        cfg["mcp"]["servers"]["google_workspace"]["allowed_operations"]["sheets"] = ["read"]
        row.tools_config = cfg
        await db.commit()
    result = await tools(database)["test_assistant_google_tools"].ainvoke({"agent_id": str(agent.id), "range_name": "Arthur_Test!A1", "mode": "write_test", "confirmed": True})
    assert not result["ok"] and not sheets.writes


async def prepared_plan(database, sheets):
    agent = await agent_row(database)
    session, run = await conversation(database, agent)
    ops = tools(database)
    prepared = await ops["prepare_assistant_run_recovery"].ainvoke({"run_id": str(run.id), "updates": [{"range_name": "Jawaban!A2:B2", "values": [["Bagas", "Original answer"]]}]})
    assert prepared["ok"] and prepared["needs_confirmation"] and not sheets.writes
    return agent, session, run, prepared, ops


@pytest.mark.asyncio
async def test_recovery_writes_verifies_and_repeated_call_is_idempotent(database, sheets):
    agent, session, run, plan, ops = await prepared_plan(database, sheets)
    args = {"run_id": str(run.id), "plan_token": plan["plan_token"], "confirmed": True}
    first = await ops["recover_assistant_run"].ainvoke(args)
    second = await ops["recover_assistant_run"].ainvoke(args)
    assert first["ok"] and first["verified_ranges"] == 1
    assert second["already_recovered"] and len(sheets.writes) == 1
    async with database() as db:
        saved = await db.get(Run, run.id)
        assert saved.status == "completed"  # Original run audit remains truthful.
        assert saved.runtime_metadata["arthur_recovery"]["status"] == "verified"
        assert (await db.get(Agent, agent.id)).version == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["data", "version", "message", "token", "expired"])
async def test_recovery_rejects_stale_or_unapproved_plan(database, sheets, change):
    agent, session, run, plan, ops = await prepared_plan(database, sheets)
    if change == "data":
        sheets.cells["Jawaban!A2:B2"] = [["Newer", "Do not overwrite"]]
    async with database() as db:
        if change == "version":
            (await db.get(Agent, agent.id)).version += 1
        elif change == "message":
            db.add(Message(session_id=session.id, role="user", content="New turn", timestamp=datetime.now(timezone.utc)+timedelta(seconds=1)))
        elif change == "expired":
            row = await db.get(Run, run.id)
            meta = copy.deepcopy(row.runtime_metadata)
            meta["arthur_recovery"]["expires_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
            row.runtime_metadata = meta
        await db.commit()
    result = await ops["recover_assistant_run"].ainvoke({"run_id": str(run.id), "plan_token": "wrong" if change == "token" else plan["plan_token"], "confirmed": True})
    assert not result["ok"] and not sheets.writes


@pytest.mark.asyncio
async def test_uncertain_write_is_verified_before_retry_not_repeated(database, sheets):
    _, _, run, plan, ops = await prepared_plan(database, sheets)
    sheets.fail_after_write = True
    args = {"run_id": str(run.id), "plan_token": plan["plan_token"], "confirmed": True}
    first = await ops["recover_assistant_run"].ainvoke(args)
    assert not first["ok"] and len(sheets.writes) == 1
    second = await ops["recover_assistant_run"].ainvoke(args)
    assert second["ok"] and len(sheets.writes) == 1


@pytest.mark.asyncio
async def test_recovery_refuses_another_owner_run(database, sheets):
    agent = await agent_row(database, owner="628999999999")
    _, run = await conversation(database, agent)
    result = await tools(database)["prepare_assistant_run_recovery"].ainvoke({"run_id": str(run.id)})
    assert not result["ok"] and not sheets.writes


@pytest.mark.asyncio
async def test_recovery_after_process_crash_compares_live_values(database, sheets):
    _, _, run, plan, ops = await prepared_plan(database, sheets)
    sheets.cells["Jawaban!A2:B2"] = [["Bagas", "Original answer"]]
    async with database() as db:
        row = await db.get(Run, run.id)
        metadata = copy.deepcopy(row.runtime_metadata)
        metadata["arthur_recovery"]["status"] = "executing"
        metadata["arthur_recovery"]["execution_started_at"] = (datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat()
        row.runtime_metadata = metadata
        await db.commit()
    result = await ops["recover_assistant_run"].ainvoke({"run_id": str(run.id), "plan_token": plan["plan_token"], "confirmed": True})
    assert result["ok"] and not sheets.writes


@pytest.mark.parametrize("updates", [
    [{"range_name": "Jawaban!A1:B1", "values": [["missing-cell"]]}],
    [{"range_name": "Jawaban!A1", "values": [["a"]]}, {"range_name": "'Jawaban'!A1", "values": [["b"]]}],
])
def test_recovery_rejects_malformed_or_overlapping_updates(updates):
    with pytest.raises(OperationRejected):
        run_recovery.check_updates(updates)


@pytest.mark.parametrize("name", ["Sheet1!A1:ZZ9999", "A1", "Sheet1!B2:A1", "Sheet1!A:A", "Sheet1!A0"])
def test_unbounded_or_invalid_ranges_rejected(name):
    with pytest.raises(OperationRejected):
        range_shape(name)


def test_truncated_sheet_result_never_treated_as_blank():
    with pytest.raises(OperationRejected):
        read_values("Successfully read 3 rows:\nRow 1: ['header']", "Sheet1!A1:A3")

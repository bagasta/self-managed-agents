"""Behavioral regressions for the failures observed in QA Batch 1–10."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_arthur_p1_operations import database, agent_row, conversation, OWNER
from arthur_v2 import plugin, operations, google_oauth
from arthur_v2.memory_revision_tools import scope_value
from arthur_v2.operations_common import OperationRejected
from app.core.engine.agent_whatsapp_guards import (
    _extract_direct_whatsapp_confirmation_payload,
    _is_direct_whatsapp_text_send_context,
)
from app.core.engine.tool_builder import build_heartbeat_tools
from app.core.tools.escalation_tool import build_escalation_tools
from app.models.scheduled_job import ScheduledJob
from app.models.agent import Agent
from app.models.document import Document
from app.models.memory import Memory
from app.models.message import Message
from app.models.session import Session
from app.models.subscription import UserSubscription
from app.core.domain.document_service import search_documents_keyword
from app.core.engine.agent_identity import _is_customer_whatsapp_session
from app.core.engine.agent_google_routing import _is_google_workspace_owner_or_operator, guard_customer_auth_links
from app.core.engine.prompt_builder import build_agent_context_block
from arthur_v2.operations_common import read_values
from sqlalchemy.ext.compiler import compiles
from pgvector.sqlalchemy import Vector
from datetime import datetime, timezone, timedelta
from arthur_v2.confirmation_gate import apply_confirmation_gate
from langchain_core.tools import tool
from langchain_core.messages import ToolMessage
from app.core.engine.agent_whatsapp_guards import _direct_whatsapp_send_guard_reply
from arthur_v2.permission_policy import setup_google_permissions
from app.core.tools.scheduler_tool import validate_autonomous_cadence
from app.core.engine.google_mcp_support import reconcile_google_mcp_tools_config, filter_google_mcp_tools_by_services
from unittest.mock import MagicMock


@compiles(Vector, "sqlite")
def _vector_sqlite(element, compiler, **kw):
    return "TEXT"


@pytest.mark.parametrize("cohort", [False, True])
def test_expiry_rollout_blocks_only_reviewed_users(monkeypatch, cohort):
    user_id = uuid.uuid4()
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(
        subscription_expiry_enforcement_enabled=False,
        subscription_expiry_enforcement_user_ids=[str(user_id)] if cohort else [],
    ))
    sub = UserSubscription(user_id=user_id, status="trial", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert sub.term_is_valid() is False
    assert sub.is_usable is (not cohort)
    sub.status = "grace_period"
    sub.grace_until = datetime.now(timezone.utc) + timedelta(days=1)
    assert sub.is_usable is True
    sub.grace_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert sub.is_usable is (not cohort)


@pytest.mark.parametrize("preview", [False, True])
def test_demo_customer_preview_does_not_change_owner_access_elsewhere(preview):
    agent = SimpleNamespace(id=uuid.uuid4(), name="CS", model="test-model", instructions="Panggil Kak", safety_policy=None,
        owner_external_id=OWNER, operator_ids=[OWNER], escalation_config={}, capabilities=[],
        tools_config={"demo_customer_preview": preview})
    session = SimpleNamespace(id=uuid.uuid4(), agent_id=agent.id, channel_type="whatsapp",
        external_user_id=OWNER, channel_config={"device_id": "wadev_test", "phone_number": OWNER})
    assert _is_customer_whatsapp_session(session, agent) is preview
    assert _is_google_workspace_owner_or_operator(session, agent) is (not preview)
    reply = "Silakan login https://auth.example/v1/integrations/google/start?state=private"
    guarded = guard_customer_auth_links(reply, session, agent)
    assert ("private" not in guarded) is preview
    assert guard_customer_auth_links("Link menu https://docs.google.com/spreadsheets/d/public", session, agent).endswith("/public")
    context = build_agent_context_block(agent, session, [], [], [])
    if preview:
        assert "Current User Role: OPERATOR" not in context
    session.channel_config["device_id"] = "owned-business-device"
    assert _is_google_workspace_owner_or_operator(session, agent)


@pytest.mark.asyncio
async def test_stored_knowledge_can_be_listed_read_and_retrieved_without_envelope_terms(database):
    agent = await agent_row(database, tools_config={"rag": True})
    other = await agent_row(database, owner="6289999999999")
    async with database() as db:
        async with db.bind.begin() as connection:
            await connection.run_sync(Document.__table__.create)
        doc = Document(agent_id=agent.id, title="Harga", content="Nastar Keju Rp140.000", source="harga.pdf", doc_metadata={})
        foreign = Document(agent_id=other.id, title="Private", content="Other customer data", doc_metadata={})
        db.add_all([doc, foreign])
        await db.commit()
        did, foreign_id = str(doc.id), str(foreign.id)
    tools = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone=OWNER, self_agent_id=None)}
    listing = await tools["list_assistant_knowledge"].ainvoke({"agent_id": str(agent.id)})
    assert listing["total_chunks"] == 1 and listing["documents"][0]["source"] == "harga.pdf"
    read = await tools["read_assistant_knowledge"].ainvoke({"agent_id": str(agent.id), "document_id": did})
    assert read["content"] == "Nastar Keju Rp140.000"
    assert not (await tools["read_assistant_knowledge"].ainvoke({"agent_id": str(agent.id), "document_id": foreign_id}))["ok"]
    async with database() as db:
        found = await search_documents_keyword(agent.id, "<OWNER> Name WA: Agent No: 6281 Pesan: Harga nastar keju berapa, Kak?", db)
        assert [str(d.id) for d in found] == [did]


@pytest.mark.asyncio
async def test_deleted_brief_and_deployment_receipt_are_evidence_not_restoration(database):
    agent = await agent_row(database, name="CS Project", is_deleted=True, tools_config={})
    async with database() as db:
        db.add(Memory(agent_id=agent.id, key="deploy_url", scope=OWNER, value_data="https://preview.example/"))
        await db.commit()
    tools = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone=OWNER, self_agent_id=None)}
    result = await tools["inspect_assistant_history"].ainvoke({"name": "CS Project"})
    assert result["deleted"] and result["instructions"] == "Existing SOP"
    assert result["deployment_urls"] == ["https://preview.example/"]
    async with database() as db:
        assert (await db.get(Agent, agent.id)).is_deleted
    foreign = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone="6289999999999", self_agent_id=None)}
    assert not (await foreign["inspect_assistant_history"].ainvoke({"agent_id": str(agent.id)}))["ok"]


@pytest.mark.parametrize("result", [
    {"values": [["A"]], "range": "Data!A1"},
    {"structuredContent": {"values": [["A"]], "range": "'Data'!A1"}},
    '{"values":[["A"]],"range":"Data!A1"}',
])
def test_sheet_probe_accepts_bounded_structured_values(result):
    assert read_values(result, "Data!A1") == [["A"]]


@pytest.mark.parametrize("result", [
    {"values": [], "error": "permission_denied"},
    {"values": [["A"]], "range": "Other!A1"},
    {"values": ["not-a-row"]},
    {"values": [["A"]], "isError": True},
])
def test_sheet_probe_never_treats_error_or_wrong_range_as_blank(result):
    with pytest.raises(OperationRejected):
        read_values(result, "Data!A1")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,purpose", [("busines", "FAQ"), ("personal", "Mencatat pesanan pelanggan toko")])
async def test_mislabeled_business_cannot_bypass_workflow_validation(kind, purpose):
    tools = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=None, owner_phone=OWNER, self_agent_id=None)}
    result = await tools["create_assistant"].ainvoke({"name": "Admin Order", "purpose": purpose,
        "instructions": "Baca data yang tersedia", "assistant_kind": kind, "confirmed": True})
    assert not result["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "payload", "intervening_turn"])
async def test_confirmation_binds_exact_payload_next_user_turn_and_receipt(database, monkeypatch, change):
    agent = await agent_row(database, tools_config={})
    session, _ = await conversation(database, agent)
    calls = []

    @tool("update_assistant")
    async def mutate(agent_id: str, instructions: str, confirmed: bool = False, name: str = "") -> dict:
        """Save the exact reviewed instructions."""
        calls.append(instructions)
        return {"ok": True, "agent_id": agent_id}

    monkeypatch.setattr("arthur_v2.confirmation_gate.get_settings", lambda: SimpleNamespace(
        arthur_confirmation_owner_ids=[OWNER]))
    gated = apply_confirmation_gate([mutate], db_factory=database, owner_phone=OWNER,
        session_id=str(session.id), self_agent_id=str(agent.id))[0]
    start = datetime.now(timezone.utc)
    async with database() as db:
        row = await db.get(Session, session.id)
        row.external_user_id = OWNER
        db.add(Message(session_id=session.id, role="user", content="Ubah tone jadi santai.", timestamp=start))
        await db.commit()
    payload = {"agent_id": str(agent.id), "instructions": "Santai", "confirmed": True}
    preview = await gated.ainvoke(payload)
    assert preview["needs_confirmation"] and not calls
    assert (await gated.ainvoke(payload))["needs_confirmation"] and not calls
    if change == "intervening_turn":
        async with database() as db:
            db.add(Message(session_id=session.id, role="user", content="Berapa slot saya?", timestamp=start + timedelta(seconds=1)))
            await db.commit()
    async with database() as db:
        db.add(Message(session_id=session.id, role="user", content="<OWNER>\nPesan: ya", timestamp=start + timedelta(seconds=2)))
        await db.commit()
    if change == "payload":
        payload["instructions"] = "Instruksi lain"
    result = await gated.ainvoke(payload)
    if change != "none":
        assert result["needs_confirmation"] and not calls
    else:
        assert result["ok"] and calls == ["Santai"]
        again = await gated.ainvoke({**payload, "name": ""})
        assert again["already_applied"] and calls == ["Santai"]


@pytest.mark.asyncio
async def test_confirmation_cannot_replace_an_action_with_an_uncertain_outcome(database, monkeypatch):
    agent = await agent_row(database)
    session, _ = await conversation(database, agent)
    calls = []

    @tool("update_assistant")
    async def mutate(instructions: str, confirmed: bool = False) -> dict:
        """Update instructions after review."""
        calls.append(instructions)
        return {"ok": True}

    monkeypatch.setattr("arthur_v2.confirmation_gate.get_settings", lambda: SimpleNamespace(
        arthur_confirmation_owner_ids=[OWNER]))
    pending = {"digest": "in-flight-change", "status": "executing", "approved_message_id": "previous-turn"}
    async with database() as db:
        row = await db.get(Session, session.id)
        row.external_user_id = OWNER
        row.metadata_ = {"arthur_reviewed_change": pending}
        db.add(Message(session_id=session.id, role="user", content="Ubah yang lain"))
        await db.commit()
    gated = apply_confirmation_gate([mutate], db_factory=database, owner_phone=OWNER,
        session_id=str(session.id), self_agent_id=str(agent.id))[0]
    result = await gated.ainvoke({"instructions": "New change", "confirmed": True})
    assert result["outcome_uncertain"] and not calls
    async with database() as db:
        assert (await db.get(Session, session.id)).metadata_["arthur_reviewed_change"] == pending


@pytest.mark.parametrize("cron", ["* * * * *", "*/1 * * * *", "0,1 * * * *", "0,59 * * * *"])
def test_equivalent_one_minute_autonomous_cadences_are_rejected(cron):
    with pytest.raises(ValueError):
        validate_autonomous_cadence(cron)


@pytest.mark.parametrize("cron", ["*/2 * * * *", "0 * * * *", "0,59 */2 * * *"])
def test_supported_autonomous_cadences_stay_supported(cron):
    validate_autonomous_cadence(cron)


def test_google_service_expansion_preserves_existing_permissions_and_requires_email_intent():
    agent = SimpleNamespace(instructions="Balas customer WhatsApp. # ATURAN PENGGUNAAN TOOLS\nKirim email jika tersedia",
        tools_config={"mcp": {"servers": {"google_workspace": {"allowed_operations": {"sheets": ["read"]}}}}})
    assert setup_google_permissions(agent, ["sheets", "calendar"]) == {"sheets": ["read"], "calendar": ["read"]}
    with pytest.raises(ValueError):
        setup_google_permissions(agent, ["sheets", "gmail"])
    assert setup_google_permissions(agent, ["gmail"], {"gmail": ["send"]}) == {"gmail": ["send"]}


@pytest.mark.asyncio
async def test_runtime_options_preserve_unmentioned_config_and_do_not_disable_active_jobs(database):
    config = {"rag": True, "scheduler": False, "existing_marker": {"keep": True}}
    agent = await agent_row(database, tools_config=config)
    session, _ = await conversation(database, agent)
    toolset = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone=OWNER, self_agent_id=None)}
    configure = toolset["configure_assistant_runtime"]
    result = await configure.ainvoke({"agent_id": str(agent.id), "enable_scheduler": True,
        "demo_customer_preview": True, "confirmed": True})
    assert result["ok"]
    async with database() as db:
        saved = await db.get(Agent, agent.id)
        assert saved.tools_config["existing_marker"] == {"keep": True}
        assert saved.tools_config["rag"] and saved.tools_config["scheduler"] and saved.tools_config["demo_customer_preview"]
        db.add(ScheduledJob(agent_id=agent.id, session_id=session.id, label="Active QA job", cron_expr="0 * * * *",
            payload="Review pending items", status="active", next_run_at=datetime.now(timezone.utc)))
        await db.commit()
    result = await configure.ainvoke({"agent_id": str(agent.id), "enable_scheduler": False, "confirmed": True})
    assert not result["ok"]
    async with database() as db:
        assert (await db.get(Agent, agent.id)).tools_config["scheduler"] is True


def test_explicit_google_policy_cannot_be_widened_by_runtime_prose():
    google = {"allowed_services": ["sheets", "calendar", "gmail"],
        "allowed_operations": {"sheets": ["read"], "calendar": ["read"], "gmail": ["send"]},
        "operation_policy_explicit": True}
    config = {"mcp": {"enabled": True, "servers": {"google_workspace": google}}}
    result = reconcile_google_mcp_tools_config(config, requirement_text="Kirim, ubah dan hapus semua data.")
    assert result["mcp"]["servers"]["google_workspace"]["allowed_operations"] == google["allowed_operations"]
    tools = [SimpleNamespace(name=n) for n in [
        "read_sheet_values", "modify_sheet_values", "get_calendar_events", "manage_event",
        "send_gmail_message", "get_gmail_messages"]]
    kept = filter_google_mcp_tools_by_services(tools, tools_config=result, requirement_text="", log=MagicMock())
    names = {t.name for t in kept}
    assert "read_sheet_values" in names
    assert "modify_sheet_values" not in names
    assert "manage_event" not in names
    assert "get_gmail_messages" not in names


@pytest.mark.asyncio
async def test_knowledge_import_requires_file_selection_when_two_files_exist(database, tmp_path, monkeypatch):
    agent = await agent_row(database)
    root = tmp_path / "shared" / "current_input"
    root.mkdir(parents=True)
    (root / "harga.txt").write_text("Nastar Rp140000")
    (root / "faq.txt").write_text("Informasi refund")
    monkeypatch.setattr("app.core.infra.sandbox.get_workspace_dir", lambda _: tmp_path)
    extract = AsyncMock(side_effect=AssertionError("An ambiguous import must not extract a chosen file"))
    monkeypatch.setattr("app.core.domain.file_processor.extract_text", extract)
    tools = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone=OWNER,
        self_agent_id=None, session_id=str(uuid.uuid4()))}
    result = await tools["add_assistant_knowledge"].ainvoke({"agent_id": str(agent.id), "confirmed": True})
    assert result["needs_file_selection"] and result["filenames"] == ["faq.txt", "harga.txt"]
    extract.assert_not_called()


@pytest.mark.parametrize("quote", ["ya, advance", "ya, rollback", "Halo supplier, kirim besok"])
@pytest.mark.parametrize("confirmation", ["ya", "ok", "yes"])
def test_generic_confirmation_never_replays_a_phone_and_quote(quote, confirmation):
    history = [SimpleNamespace(role="user", content=f"Nomor owner {OWNER}"),
               SimpleNamespace(role="agent", content=f'Draft konfigurasi. Balas "{quote}"')]
    assert not _is_direct_whatsapp_text_send_context(confirmation, history)
    assert _extract_direct_whatsapp_confirmation_payload(confirmation, history) is None
    assert _is_direct_whatsapp_text_send_context(f"Kirim pesan ke {OWNER}: Halo", history)


@pytest.mark.parametrize("tool_name", ["send_to_number", "reply_to_user"])
@pytest.mark.parametrize("operator", [False, True])
def test_failed_tool_call_is_not_delivery_evidence_in_current_or_previous_turn(tool_name, operator):
    request = "<OPERATOR>\nPesan: apakah tadi sudah terkirim?" if operator else "apakah tadi sudah terkirim?"
    result = _direct_whatsapp_send_guard_reply("Pesan WhatsApp sudah saya kirim ke supplier.",
        [{"tool": tool_name, "result": "[error] Provider timeout"}], request,
        [ToolMessage(name=tool_name, content="[error] Provider timeout", tool_call_id="failed-call")])
    assert result.startswith("Belum saya kirim.")


@pytest.mark.asyncio
async def test_all_status_reads_report_pause_and_resume(database, monkeypatch):
    agent = await agent_row(database, tools_config={})
    status = AsyncMock(return_value={"connected": False})
    monkeypatch.setattr(plugin, "get_google_oauth_status", status)
    monkeypatch.setattr(google_oauth, "get_google_oauth_status", status)
    tools = {t.name: t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone=OWNER, self_agent_id=None)}
    for paused in (True, False):
        changed = await tools["set_assistant_paused"].ainvoke({"agent_id": str(agent.id), "paused": paused, "confirmed": True})
        assert changed["ok"]
        for name in ("inspect_managed_assistant", "get_assistant_access", "diagnose_assistant"):
            result = await tools[name].ainvoke({"agent_id": str(agent.id)})
            view = result.get("assistant", result)
            assert view["paused"] is paused
            assert view["service_status"] == ("paused" if paused else "active")


@pytest.mark.asyncio
async def test_hourly_heartbeat_commits_exact_schedule(database):
    agent = await agent_row(database)
    session, _ = await conversation(database, agent)
    tools = {t.name: t for t in build_heartbeat_tools(agent.id, session.id, database, scope=OWNER)}
    result = await tools["enable_heartbeat"].ainvoke({"interval_minutes": 60, "quiet_start": "22:00", "quiet_end": "06:00"})
    assert not result.startswith("[error]")
    async with database() as db:
        job = (await db.execute(select(ScheduledJob))).scalar_one()
        assert job.status == "active" and job.cron_expr == "0 */1 * * *"
        assert job.next_run_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"interval_minutes": 0}, {"interval_minutes": -5},
    {"interval_minutes": 90}, {"interval_minutes": 61}, {"quiet_start": "25:00"}, {"quiet_end": "06:99"}])
async def test_invalid_heartbeat_has_no_database_side_effects(args):
    tools = {t.name: t for t in build_heartbeat_tools(uuid.uuid4(), uuid.uuid4(), None)}
    assert (await tools["enable_heartbeat"].ainvoke(args)).startswith("[error]")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "missing_receipt"])
async def test_escalation_never_claims_success_without_delivery(database, monkeypatch, failure):
    agent = await agent_row(database)
    session, _ = await conversation(database, agent)
    send = AsyncMock(side_effect=RuntimeError("private provider detail")) if failure == "exception" else AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr("app.core.infra.channel_service.send_message", send)
    tools = {t.name: t for t in build_escalation_tools(session.id, agent.id, database)}
    for name in ("escalate_to_human", "notify_owner"):
        result = await tools[name].ainvoke({"reason": "Komplain"})
        assert result.startswith("[error]")
        assert "private provider detail" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_channel", [None, {"device_id": "wadev_wrong"},
    {"device_id": "arthur_verified", "user_phone": OWNER}])
async def test_demo_escalation_requires_a_separate_verified_owner_channel(database, monkeypatch, owner_channel):
    agent = await agent_row(database, device="wadev_preview")
    session, _ = await conversation(database, agent)
    send = AsyncMock(return_value={"message_id": "owner-receipt"})
    monkeypatch.setattr("app.core.infra.channel_service.send_message", send)
    monkeypatch.setattr("app.core.engine.agent_google_routing._arthur_owner_notification_channel_config",
        AsyncMock(return_value=owner_channel))
    toolset = {t.name: t for t in build_escalation_tools(session.id, agent.id, database)}
    result = await toolset["escalate_to_human"].ainvoke({"reason": "Perlu review"})
    if owner_channel and owner_channel["device_id"] == "arthur_verified":
        assert not result.startswith("[error]")
        assert send.call_args.kwargs["channel_config"] == owner_channel
        async with database() as db:
            assert (await db.get(Session, session.id)).metadata_["escalation_message_id"] == "owner-receipt"
    else:
        assert result.startswith("[error]")
        send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["send_to_number", "reply_to_user"])
@pytest.mark.parametrize("failure", ["exception", "missing_receipt"])
async def test_outbound_tools_never_claim_delivery_when_provider_did_not_confirm(database, monkeypatch, name, failure):
    agent = await agent_row(database)
    session, _ = await conversation(database, agent)
    send = AsyncMock(side_effect=RuntimeError("private provider detail")) if failure == "exception" else AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr("app.core.infra.channel_service.send_message", send)
    monkeypatch.setattr("app.core.engine.wa_outbound_guard.check_wa_outbound_direct_window", AsyncMock(return_value=(True, 0)))
    toolset = {t.name: t for t in build_escalation_tools(session.id, agent.id, database, user_jid="6281111111111")}
    args = {"message": "Halo supplier"}
    if name == "send_to_number":
        args["phone_or_target"] = "6281111111111"
    result = await toolset[name].ainvoke(args)
    assert result.startswith("[error]") and "private provider detail" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["send_to_number", "reply_to_user"])
async def test_account_links_cannot_bypass_customer_guard_through_outbound_tools(monkeypatch, name):
    send = AsyncMock()
    monkeypatch.setattr("app.core.infra.channel_service.send_message", send)
    toolset = {t.name: t for t in build_escalation_tools(uuid.uuid4(), uuid.uuid4(), lambda: None,
        user_jid="6281111111111")}
    args = {"message": "Login https://auth.example/v1/integrations/google/start?t=private"}
    if name == "send_to_number":
        args["phone_or_target"] = "6281111111111"
    assert (await toolset[name].ainvoke(args)).startswith("[error]")
    send.assert_not_called()


@pytest.mark.asyncio
async def test_paused_demo_sends_notice_without_running_agent_or_duplicate_gateway_fallback(monkeypatch):
    from app.api import channels
    body = channels.WAIncomingMessage(device_id="wadev_preview", from_="6281111111111", message="Halo")
    from app.core.domain.agent_service_controls import CONTROL_KEY
    agent = SimpleNamespace(capabilities=[], tools_config={CONTROL_KEY: {"paused": True}})
    monkeypatch.setattr(channels, "_resolve_wa_incoming_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(channels, "_is_arthur_system_agent", lambda _: False)
    send = AsyncMock(return_value={"message_id": "pause-notice"})
    monkeypatch.setattr(channels, "send_wa_message", send)
    result = await channels.wa_incoming(body, db=None)
    assert result == {"status": "agent_paused", "reply": "", "notice_sent": True}
    assert send.call_args.args[:2] == ("wadev_preview", "6281111111111")


@pytest.mark.parametrize("scope", [OWNER, "+" + OWNER, "phone:" + OWNER, OWNER + "@s.whatsapp.net"])
def test_memory_scope_accepts_unambiguous_phone_aliases(scope):
    assert scope_value(scope) == OWNER


def test_memory_scope_never_guesses_an_opaque_lid():
    with pytest.raises(OperationRejected):
        scope_value("123456789012345678@lid")

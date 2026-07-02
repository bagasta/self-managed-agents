from __future__ import annotations

from datetime import datetime
import uuid
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _FakeDB:
    def __init__(self, result=None):
        self.results = list(result) if isinstance(result, list) else [result]
        self.get_results = {}
        self.added = []
        self.commits = 0
        self.executed = 0
        self.exec_calls = []

    async def execute(self, _stmt, params=None):
        self.executed += 1
        self.exec_calls.append((_stmt, params))
        if self.results:
            return _ScalarResult(self.results.pop(0))
        return _ScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def get(self, _model, key):
        return self.get_results.get(key)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj, attribute_names=None):
        return None


def _agent():
    return SimpleNamespace(id=uuid.uuid4(), escalation_config={"operator_phone": "628operator"})


def _session(**overrides):
    values = {
        "id": uuid.uuid4(),
        "external_user_id": "628customer",
        "channel_config": {
            "user_phone": "628customer@s.whatsapp.net",
            "phone_number": "628customer",
            "device_id": "dev-1",
        },
        "metadata_": {"escalation_case_id": "esc_123456_ab12cd"},
        "ai_disabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_owner_is_operator_identity():
    from app.api.wa_helpers import is_operator_message

    agent = SimpleNamespace(
        escalation_config={},
        owner_external_id="628owner",
        operator_ids=["628owner"],
    )

    assert is_operator_message("628owner", "628owner@s.whatsapp.net", agent) is True


def test_configured_operator_phone_is_escalation_operator_even_if_owner_differs():
    from app.api.wa_helpers import is_operator_message

    agent = SimpleNamespace(
        escalation_config={"operator_phone": "628operator"},
        owner_external_id="628owner",
        operator_ids=["628owner"],
    )

    assert is_operator_message("628operator", "628operator@s.whatsapp.net", agent) is True


def test_non_owner_operator_ids_still_work_for_legacy_extra_operator():
    from app.api.wa_helpers import is_operator_message

    agent = SimpleNamespace(
        escalation_config={},
        owner_external_id="628owner",
        operator_ids=["628owner", "628operator2"],
    )

    assert is_operator_message("628operator2", "628operator2@s.whatsapp.net", agent) is True


@pytest.mark.asyncio
async def test_wa_incoming_media_metadata_without_payload_short_circuits(monkeypatch):
    from app.api.channels import WAIncomingMessage, wa_incoming

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Baas",
        allowed_senders=None,
        escalation_config={},
        owner_external_id="628owner",
        operator_ids=["628owner"],
    )
    session = _session(ai_disabled=False, metadata_={})
    db = _FakeDB()
    send_msg = AsyncMock()
    process_media = AsyncMock()
    stop_typing = AsyncMock()

    monkeypatch.setattr("app.api.channels.find_agent_by_device", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.api.channels.is_duplicate_message", AsyncMock(return_value=False))
    monkeypatch.setattr("app.api.channels.find_or_create_wa_session", AsyncMock(return_value=(session, False)))
    monkeypatch.setattr("app.api.channels.check_agent_quota", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr("app.api.channels.check_wa_spam_window", AsyncMock(return_value=(False, 0)))
    monkeypatch.setattr("app.api.channels.process_wa_media", process_media)
    monkeypatch.setattr("app.api.channels._stop_customer_typing", stop_typing)
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)

    body = WAIncomingMessage(
        device_id=f"wadev_{agent.id}",
        **{"from": "123456789012345678@lid"},
        chat_id="123456789012345678@lid",
        message="Buatkan visualisasi berdasarkan data ini",
        message_id="MSG-MEDIA-1",
        timestamp=1,
        media_type="document",
        media_filename="titanic.txt",
        media_mimetype="text/plain",
    )

    result = await wa_incoming(body, db=db)

    assert result["status"] == "media_payload_missing"
    assert "titanic.txt" in result["reply"]
    assert "kirim ulang" in result["reply"]
    process_media.assert_not_awaited()
    stop_typing.assert_awaited_once()
    send_msg.assert_awaited_once_with(
        f"wadev_{agent.id}",
        "123456789012345678@lid",
        result["reply"],
    )


@pytest.mark.asyncio
async def test_wa_incoming_stale_turn_does_not_run_agent(monkeypatch):
    from app.api.channels import WAIncomingMessage, wa_incoming

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Baas",
        allowed_senders=None,
        escalation_config={},
        owner_external_id="628owner",
        operator_ids=["628owner"],
        tools_config={},
    )
    session = _session(ai_disabled=False, metadata_={})
    db = _FakeDB()
    run_agent = AsyncMock()

    monkeypatch.setattr("app.api.channels._resolve_wa_incoming_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.api.channels.is_duplicate_message", AsyncMock(return_value=False))
    monkeypatch.setattr("app.api.channels.find_or_create_wa_session", AsyncMock(return_value=(session, False)))
    monkeypatch.setattr("app.api.channels.check_agent_quota", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr("app.api.channels.check_wa_spam_window", AsyncMock(return_value=(False, 1)))
    monkeypatch.setattr("app.core.engine.session_lock.mark_latest_session_turn", AsyncMock(return_value=7))
    monkeypatch.setattr("app.core.engine.session_lock.cancel_active_run", AsyncMock(return_value=False))
    monkeypatch.setattr("app.core.engine.session_lock.is_latest_session_turn", AsyncMock(return_value=False))
    monkeypatch.setattr("app.core.engine.agent_runner.run_agent", run_agent)

    body = WAIncomingMessage(
        device_id="dev-1",
        **{"from": "628customer@s.whatsapp.net"},
        phone_from="628customer",
        chat_id="628customer@s.whatsapp.net",
        message="s",
        message_id="MSG-STALE-1",
        timestamp=1,
    )

    result = await wa_incoming(body, db=db)

    assert result["status"] == "stale_ignored"
    run_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_wa_incoming_stale_result_does_not_send_final_reply(monkeypatch):
    from app.api.channels import WAIncomingMessage, wa_incoming

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Baas",
        allowed_senders=None,
        escalation_config={},
        owner_external_id="628owner",
        operator_ids=["628owner"],
        tools_config={},
    )
    session = _session(ai_disabled=False, metadata_={})
    db = _FakeDB()
    run_id = uuid.uuid4()
    send_msg = AsyncMock()

    monkeypatch.setattr("app.api.channels._resolve_wa_incoming_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.api.channels.is_duplicate_message", AsyncMock(return_value=False))
    monkeypatch.setattr("app.api.channels.find_or_create_wa_session", AsyncMock(return_value=(session, False)))
    monkeypatch.setattr("app.api.channels.check_agent_quota", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr("app.api.channels.check_wa_spam_window", AsyncMock(return_value=(False, 1)))
    monkeypatch.setattr("app.core.engine.session_lock.mark_latest_session_turn", AsyncMock(return_value=8))
    monkeypatch.setattr("app.core.engine.session_lock.cancel_active_run", AsyncMock(return_value=False))
    monkeypatch.setattr("app.core.engine.session_lock.is_latest_session_turn", AsyncMock(side_effect=[True, False]))
    monkeypatch.setattr(
        "app.core.engine.agent_runner.run_agent",
        AsyncMock(return_value={"reply": "reply lama", "steps": [], "run_id": run_id, "tokens_used": 0}),
    )
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)

    body = WAIncomingMessage(
        device_id="dev-1",
        **{"from": "628customer@s.whatsapp.net"},
        phone_from="628customer",
        chat_id="628customer@s.whatsapp.net",
        message="buat website lama",
        message_id="MSG-STALE-RESULT-1",
        timestamp=1,
    )

    result = await wa_incoming(body, db=db)

    assert result["status"] == "stale_ignored"
    assert result["reply"] == ""
    assert result["run_id"] == str(run_id)
    send_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_phone_without_escalation_reply_is_customer_turn():
    from app.api.channels import _should_treat_as_operator_turn

    agent = SimpleNamespace(
        id=uuid.uuid4(),
        escalation_config={"operator_phone": "628operator"},
        owner_external_id="628owner",
        operator_ids=["628owner"],
    )
    db = _FakeDB()

    is_operator_turn = await _should_treat_as_operator_turn(
        agent=agent,
        db=db,
        from_phone="628operator",
        reply_target="628operator@s.whatsapp.net",
        message="[Gambar]",
        media_type="image",
        quoted_text=None,
        quoted_stanza_id=None,
    )

    assert is_operator_turn is False
    assert db.executed == 0


@pytest.mark.asyncio
async def test_operator_phone_with_quoted_escalation_is_operator_turn():
    from app.api.channels import _should_treat_as_operator_turn

    agent = _agent()
    target = _session()
    db = _FakeDB(result=target)

    is_operator_turn = await _should_treat_as_operator_turn(
        agent=agent,
        db=db,
        from_phone="628operator",
        reply_target="628operator@s.whatsapp.net",
        message="pembayaran sudah masuk",
        media_type=None,
        quoted_text="ESKALASI PESAN DARI CUSTOMER\nID Kasus: esc_123456_ab12cd\nNomor customer/user: 628customer",
        quoted_stanza_id=None,
    )

    assert is_operator_turn is True
    assert db.executed == 1


@pytest.mark.asyncio
async def test_operator_send_confirmation_uses_pending_draft_without_quote():
    from app.api.channels import _should_treat_as_operator_turn

    agent = _agent()
    operator_session = _session(
        external_user_id="628operator",
        metadata_={
            "pending_operator_text_reply": {
                "target_session_id": str(uuid.uuid4()),
                "message": "Baik, pembayaran sudah diterima.",
                "expires_at": int(time.time()) + 60,
            }
        },
    )
    db = _FakeDB(result=operator_session)

    is_operator_turn = await _should_treat_as_operator_turn(
        agent=agent,
        db=db,
        from_phone="628operator",
        reply_target="628operator@s.whatsapp.net",
        message="kirim",
        media_type=None,
        quoted_text=None,
        quoted_stanza_id=None,
    )

    assert is_operator_turn is True
    assert db.executed == 1


@pytest.mark.asyncio
async def test_operator_revision_uses_pending_draft_without_escalation_quote():
    from app.api.channels import _should_treat_as_operator_turn

    agent = _agent()
    operator_session = _session(
        external_user_id="628operator",
        metadata_={
            "pending_operator_text_reply": {
                "target_session_id": str(uuid.uuid4()),
                "message": "mintain alamat lengkap pengiriman",
                "expires_at": int(time.time()) + 60,
            }
        },
    )
    db = _FakeDB(result=operator_session)

    is_operator_turn = await _should_treat_as_operator_turn(
        agent=agent,
        db=db,
        from_phone="628operator",
        reply_target="628operator@s.whatsapp.net",
        message="buat lebih sopan",
        media_type=None,
        quoted_text="Draft balasan untuk customer: mintain alamat lengkap pengiriman",
        quoted_stanza_id=None,
    )

    assert is_operator_turn is True
    assert db.executed == 1


def test_operator_pending_text_revision_context_targets_current_draft():
    from app.api.channels import _operator_pending_text_revision_context

    operator_session = _session(
        external_user_id="628operator",
        metadata_={
            "pending_operator_text_reply": {
                "target_session_id": str(uuid.uuid4()),
                "target": "628customer@s.whatsapp.net",
                "case_id": "esc_123456_ab12cd",
                "message": "Ongkirnya jadi 120 ribu ya utk 1 kardus",
                "expires_at": int(time.time()) + 60,
            }
        },
    )

    context = _operator_pending_text_revision_context(operator_session, "dibuat lebih sopan")

    assert "[OPERATOR_DRAFT_REVISION]" in context
    assert "Ongkirnya jadi 120 ribu ya utk 1 kardus" in context
    assert "Instruksi revisi operator: dibuat lebih sopan" in context
    assert "Jangan menjawab rekap eskalasi" in context
    assert _operator_pending_text_revision_context(operator_session, "kirim") == ""


def test_escalation_media_forward_requires_current_turn_marker():
    from app.core.tools.escalation_tool import _is_current_turn_media

    media_meta = {
        "workspace_path": "/tmp/bukti-transfer.png",
        "source_message_id": "OLD-MSG",
    }

    assert _is_current_turn_media(media_meta, None) is False
    assert _is_current_turn_media(media_meta, {"workspace_path": "/tmp/other.png", "source_message_id": "OLD-MSG"}) is False
    assert _is_current_turn_media(media_meta, {"workspace_path": "/tmp/bukti-transfer.png", "source_message_id": "NEW-MSG"}) is False
    assert _is_current_turn_media(media_meta, {"workspace_path": "/tmp/bukti-transfer.png", "source_message_id": "OLD-MSG"}) is True


@pytest.mark.asyncio
async def test_operator_escalation_recap_request_is_operator_turn_without_quote():
    from app.api.channels import _should_treat_as_operator_turn

    agent = _agent()
    operator_session = _session(external_user_id="628operator", metadata_={})
    db = _FakeDB(result=operator_session)

    is_operator_turn = await _should_treat_as_operator_turn(
        agent=agent,
        db=db,
        from_phone="628operator",
        reply_target="628operator@s.whatsapp.net",
        message="ada berapa pesan eskalasi hari ini?",
        media_type=None,
        quoted_text=None,
        quoted_stanza_id=None,
    )

    assert is_operator_turn is True
    assert db.executed == 1


def test_format_operator_escalation_recap_counts_today_rows():
    from app.api.channels import _format_operator_escalation_recap

    row = (
        SimpleNamespace(
            content=(
                "ESKALASI PESAN DARI CUSTOMER\n"
                "ID Kasus: esc_123456_ab12cd\n"
                "Nomor customer/user: 628customer\n"
                "Nama customer: Wira\n"
                "Alasan eskalasi: tanya ongkir\n"
                "Pesan: Ongkir JNT ke Jogja berapa?"
            ),
            timestamp=datetime.fromisoformat("2026-06-11T04:19:00+00:00"),
        ),
        SimpleNamespace(external_user_id="628customer"),
    )

    recap = _format_operator_escalation_recap([row])

    assert "Total eskalasi tercatat hari ini: 1." in recap
    assert "esc_123456_ab12cd" in recap
    assert "Wira (628customer)" in recap
    assert "tanya ongkir" in recap


def test_operator_prompt_includes_escalation_recap_context():
    from app.core.engine.prompt_builder import build_system_prompt

    prompt = build_system_prompt(
        agent_model=SimpleNamespace(
            name="Yo Besty",
            model="gpt-4.1-mini",
            instructions="Bantu admin menjawab customer.",
            safety_policy=None,
            tools_config={"memory": True, "escalation": True},
            escalation_config={},
            capabilities=[],
        ),
        session=SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            channel_type="whatsapp",
            channel_config={},
            external_user_id="628operator",
        ),
        active_groups=["memory", "escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory={},
        rag_context="",
        escalation_user_jid=None,
        escalation_context="### Rekap eskalasi hari ini\nTotal eskalasi tercatat hari ini: 1.",
        is_operator_message=True,
        user_message="<OPERATOR>\nPesan: ada berapa pesan eskalasi hari ini?",
    )

    assert "### Konteks admin/operator yang tersedia" in prompt
    assert "Total eskalasi tercatat hari ini: 1." in prompt
    assert "jawab langsung berdasarkan blok konteks admin/operator" in prompt


def test_operator_prompt_prioritizes_pending_draft_revision_block():
    from app.core.engine.prompt_builder import build_system_prompt

    prompt = build_system_prompt(
        agent_model=SimpleNamespace(
            name="Yo Besty",
            model="gpt-4.1-mini",
            instructions="Bantu admin menjawab customer.",
            safety_policy=None,
            tools_config={"memory": True, "escalation": True},
            escalation_config={},
            capabilities=[],
        ),
        session=SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            channel_type="whatsapp",
            channel_config={},
            external_user_id="628operator",
        ),
        active_groups=["memory", "escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory={},
        rag_context="",
        escalation_user_jid="628customer@s.whatsapp.net",
        escalation_context="ROUTING: operator_reply_quoted_escalation",
        is_operator_message=True,
        user_message="<OPERATOR>\nPesan: [OPERATOR_DRAFT_REVISION]\nInstruksi revisi operator: dibuat lebih sopan",
    )

    assert "Jika pesan berisi blok `[OPERATOR_DRAFT_REVISION]`" in prompt
    assert "Revisi HANYA draft di dalam blok itu" in prompt


@pytest.mark.asyncio
async def test_find_or_create_wa_session_takes_advisory_lock_before_insert():
    from app.api.wa_helpers import find_or_create_wa_session

    agent = _agent()
    db = _FakeDB(result=None)
    db.flush = AsyncMock()
    db.refresh = AsyncMock()

    session, was_created = await find_or_create_wa_session(
        agent=agent,
        lookup_user_id="+628123456789",
        effective_reply_target="628123456789@s.whatsapp.net",
        device_id="dev-1",
        db=db,
        is_operator=False,
        phone_number="628123456789",
    )

    assert was_created is True
    assert session.external_user_id == "628123456789"
    assert db.executed == 2
    lock_stmt, lock_params = db.exec_calls[0]
    assert "pg_advisory_xact_lock" in str(lock_stmt)
    assert lock_params == {
        "agent_key": f"wa_session:{agent.id}",
        "user_key": "628123456789",
    }


def test_quoted_reply_context_is_added_to_normal_whatsapp_message():
    from app.api.channels import _append_quoted_reply_context

    result = _append_quoted_reply_context(
        "ini gimana jadinya?",
        "kok koneknya ke CV Maker?",
    )

    assert "[WHATSAPP_REPLY_CONTEXT]" in result
    assert "kok koneknya ke CV Maker?" in result
    assert result.endswith("ini gimana jadinya?")


def test_whatsapp_interrupt_does_not_send_manual_ack_message():
    import inspect
    from app.api import channels

    source = inspect.getsource(channels.wa_incoming)
    assert "Oke, saya stop proses sebelumnya" not in source
    assert "interrupt_ack" not in source


def test_operator_identity_customer_turn_is_not_final_reply_suppressed():
    import inspect
    from app.api import channels

    source = inspect.getsource(channels.wa_incoming)
    assert "operator_identity_treated_as_customer" in source
    assert "normalized_target == normalized_operator and not _operator_identity" in source


@pytest.mark.asyncio
async def test_spam_window_triggers_only_after_limit(monkeypatch):
    from app.api import wa_helpers

    async def no_redis():
        return None

    monkeypatch.setattr("app.core.infra.redis_client.get_redis", no_redis)
    wa_helpers._mem_spam_windows.clear()

    states = []
    for _ in range(6):
        states.append(
            await wa_helpers.check_wa_spam_window(
                agent_id="agent-1",
                session_id="session-1",
                sender_id="628111",
                limit=5,
                window_seconds=60,
            )
        )

    assert [is_spam for is_spam, _ in states] == [False, False, False, False, False, True]
    assert states[-1][1] == 6


@pytest.mark.asyncio
async def test_dedup_uses_message_id_for_same_second_distinct_messages(monkeypatch):
    from app.api import wa_helpers

    async def no_redis():
        return None

    monkeypatch.setattr("app.core.infra.redis_client.get_redis", no_redis)
    wa_helpers._mem_dedup_cache.clear()

    first = await wa_helpers.is_duplicate_message(
        "dev-1",
        "628customer",
        1770000000,
        _FakeDB(),
        message_id="MSG-1",
    )
    second_same_second = await wa_helpers.is_duplicate_message(
        "dev-1",
        "628customer",
        1770000000,
        _FakeDB(),
        message_id="MSG-2",
    )
    duplicate_retry = await wa_helpers.is_duplicate_message(
        "dev-1",
        "628customer",
        1770000000,
        _FakeDB(),
        message_id="MSG-2",
    )

    assert first is False
    assert second_same_second is False
    assert duplicate_retry is True


@pytest.mark.asyncio
async def test_case_lookup_is_strict_without_quote():
    from app.api.wa_helpers import find_session_by_quoted_case

    db = _FakeDB(result=_session())
    session, case_id = await find_session_by_quoted_case(_agent(), db, quoted_text=None)

    assert session is None
    assert case_id is None
    assert db.executed == 0


@pytest.mark.asyncio
async def test_operator_context_without_quote_does_not_fallback_to_latest_escalation():
    from app.api.wa_helpers import find_escalation_context

    agent = SimpleNamespace(id=uuid.uuid4())
    operator = _session(metadata_={})
    latest_escalation_session = _session(agent_id=agent.id)
    db = _FakeDB(result=latest_escalation_session)

    escalation_user_jid, escalation_context = await find_escalation_context(
        agent,
        db,
        quoted_text=None,
        quoted_stanza_id=None,
        operator_session=operator,
    )

    assert escalation_user_jid is None
    assert escalation_context is None
    assert db.executed == 0


@pytest.mark.asyncio
async def test_expired_operator_active_route_is_not_used():
    from app.api.wa_helpers import find_session_by_operator_active_route

    agent = SimpleNamespace(id=uuid.uuid4())
    target = _session(id=uuid.uuid4(), agent_id=agent.id)
    operator = _session(
        metadata_={
            "active_escalation_reply": {
                "target_session_id": str(target.id),
                "target": "628customer@s.whatsapp.net",
                "case_id": "esc_123456_ab12cd",
                "expires_at": int(time.time()) - 1,
            }
        }
    )
    db = _FakeDB()
    db.get_results[target.id] = target

    session, case_id = await find_session_by_operator_active_route(agent, db, operator)

    assert session is None
    assert case_id == "esc_123456_ab12cd"
    assert "active_escalation_reply" not in operator.metadata_
    assert db.commits == 1


def test_case_id_parser_accepts_media_caption_case_id():
    from app.api.wa_helpers import extract_escalation_case_id

    text = "Lampiran dari customer untuk kasus esc_1779679409_690800"

    assert extract_escalation_case_id(text) == "esc_1779679409_690800"


def test_operator_payment_approval_detection_requires_payment_context():
    from app.api.channels import _is_operator_payment_approval

    assert _is_operator_payment_approval("iya pembayaran sudah masuk") is True
    assert _is_operator_payment_approval("transfer sudah valid") is True
    assert _is_operator_payment_approval("pembayaran belum masuk") is False
    assert _is_operator_payment_approval("iya") is False
    assert _is_operator_payment_approval("ok kirim") is False


def test_customer_phone_parser_accepts_escalation_text():
    from app.api.wa_helpers import extract_escalation_customer_phone

    text = "Nomor customer/user: 6283890930647\nNama customer: Wira"

    assert extract_escalation_customer_phone(text) == "6283890930647"


@pytest.mark.asyncio
async def test_quoted_lookup_falls_back_to_customer_phone_when_case_metadata_misses():
    from app.api.wa_helpers import find_session_by_quoted_case

    target = _session(external_user_id="6283890930647")
    db = _FakeDB(result=[None, target])
    text = (
        "ESKALASI PESAN DARI CUSTOMER\n"
        "ID Kasus: esc_1779679918_690800\n"
        "Nomor customer/user: 6283890930647\n"
        "Pesan: ..."
    )

    session, case_id = await find_session_by_quoted_case(_agent(), db, quoted_text=text)

    assert session is target
    assert case_id == "esc_1779679918_690800"
    assert db.executed == 2


@pytest.mark.asyncio
async def test_quoted_message_id_lookup_uses_stanza_id_without_text():
    from app.api.wa_helpers import find_session_by_quoted_message_id

    target = _session(
        metadata_={
            "escalation_case_id": "esc_123456_ab12cd",
            "escalation_message_ids": ["MSG-ESC-1"],
        }
    )
    db = _FakeDB(result=target)

    session, case_id = await find_session_by_quoted_message_id(
        _agent(), db, quoted_stanza_id="MSG-ESC-1"
    )

    assert session is target
    assert case_id == "esc_123456_ab12cd"
    assert db.executed == 1


@pytest.mark.asyncio
async def test_operator_activate_requires_quoted_case(monkeypatch):
    from app.api.channels import _handle_operator_activate_command

    sent = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", sent)

    result = await _handle_operator_activate_command(
        agent=_agent(),
        quoted_text=None,
        device_id="dev-1",
        operator_reply_target="628operator",
        db=_FakeDB(result=_session()),
        log=SimpleNamespace(info=lambda *a, **k: None),
    )

    assert result["status"] == "ok"
    assert "Reply pesan eskalasi/spam" in result["reply"]
    sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_activate_reenables_quoted_customer(monkeypatch):
    from app.api.channels import _handle_operator_activate_command

    target = _session()
    sent = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", sent)
    db = _FakeDB(result=target)

    result = await _handle_operator_activate_command(
        agent=_agent(),
        quoted_text="ESKALASI PESAN DARI CUSTOMER\nID Kasus: esc_123456_ab12cd",
        device_id="dev-1",
        operator_reply_target="628operator",
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None),
    )

    assert result["status"] == "ok"
    assert target.ai_disabled is False
    assert target.metadata_["spam_auto_disabled"] is False
    assert db.commits == 1
    sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_image_forward_uses_quoted_message_id_without_text(monkeypatch):
    from app.api.channels import _forward_operator_media_to_customer

    target = _session(
        metadata_={
            "escalation_case_id": "esc_123456_ab12cd",
            "escalation_message_ids": ["MSG-ESC-1"],
        }
    )
    operator = _session(external_user_id="628operator", metadata_={})
    db = _FakeDB(result=target)
    send_msg = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)
    monkeypatch.setattr(
        "app.api.channels.process_wa_media",
        AsyncMock(return_value=(
            "\n[Sistem: Operator mengirim image]",
            None,
            None,
            {
                "media_type": "image",
                "workspace_path": "/tmp/photo.png",
                "filename": "photo.png",
                "mimetype": "image/png",
            },
        )),
    )

    result = await _forward_operator_media_to_customer(
        agent=_agent(),
        quoted_text=None,
        quoted_stanza_id="MSG-ESC-1",
        device_id="dev-1",
        operator_reply_target="628operator",
        media_type="image",
        media_data="aW1hZ2U=",
        media_filename="photo.png",
        caption="ini bukti",
        operator_session=operator,
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert result["status"] == "queued"
    assert operator.metadata_["pending_operator_media"]["target"] == "628customer@s.whatsapp.net"
    assert operator.metadata_["active_escalation_reply"]["target"] == "628customer@s.whatsapp.net"
    send_msg.assert_not_awaited()
    assert db.executed == 1


@pytest.mark.asyncio
async def test_operator_image_forward_uses_quoted_customer_target(monkeypatch):
    from app.api.channels import _forward_operator_media_to_customer

    target = _session()
    operator = _session(external_user_id="628operator", metadata_={})
    db = _FakeDB(result=target)
    send_msg = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)
    monkeypatch.setattr(
        "app.api.channels.process_wa_media",
        AsyncMock(return_value=(
            "\n[Sistem: Operator mengirim image]",
            None,
            None,
            {
                "media_type": "image",
                "workspace_path": "/tmp/photo.png",
                "filename": "photo.png",
                "mimetype": "image/png",
            },
        )),
    )

    result = await _forward_operator_media_to_customer(
        agent=_agent(),
        quoted_text="ESKALASI PESAN DARI CUSTOMER\nID Kasus: esc_123456_ab12cd",
        device_id="dev-1",
        operator_reply_target="628operator",
        media_type="image",
        media_data="aW1hZ2U=",
        media_filename="photo.png",
        caption="ini bukti",
        operator_session=operator,
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert result["status"] == "queued"
    assert operator.metadata_["pending_operator_media"]["caption_hint"] == "ini bukti"
    assert operator.metadata_["pending_operator_media"]["expires_at"] > int(time.time())
    assert operator.metadata_["active_escalation_reply"]["target"] == "628customer@s.whatsapp.net"
    send_msg.assert_not_awaited()
    assert db.commits == 2


@pytest.mark.asyncio
async def test_operator_image_without_reply_does_not_use_active_route(monkeypatch):
    from app.api.channels import _forward_operator_media_to_customer

    target = _session(id=uuid.uuid4())
    operator = _session(
        external_user_id="628operator",
        metadata_={
            "active_escalation_reply": {
                "target_session_id": str(target.id),
                "target": "628customer@s.whatsapp.net",
                "case_id": "esc_123456_ab12cd",
                "expires_at": int(time.time()) + 60,
            }
        },
    )
    db = _FakeDB()
    db.get_results[target.id] = target
    send_msg = AsyncMock()
    process_media = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)
    monkeypatch.setattr("app.api.channels.process_wa_media", process_media)

    result = await _forward_operator_media_to_customer(
        agent=_agent(),
        quoted_text=None,
        quoted_stanza_id=None,
        device_id="dev-1",
        operator_reply_target="628operator",
        media_type="image",
        media_data="aW1hZ2U=",
        media_filename="photo.png",
        caption="ini bukti",
        operator_session=operator,
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert result["status"] == "ok"
    assert "Reply pesan eskalasi yang benar" in result["reply"]
    send_msg.assert_awaited_once()
    process_media.assert_not_awaited()


def test_operator_turn_detection_accepts_angle_bracket_operator_envelope():
    from app.core.engine.agent_tool_setup import is_operator_turn

    assert is_operator_turn("<OPERATOR>\nPesan: iya pembayaran sudah masuk") is True


def test_operator_tool_setup_skips_subagents_and_business_tools():
    import inspect
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    src = inspect.getsource(build_agent_tool_setup)
    assert "operator_subagents_skipped" in src
    assert "and not operator_turn" in src


def test_extract_operator_text_draft_uses_corrected_separator_block():
    from app.api.channels import _extract_operator_text_draft

    reply = (
        "Draft pesan untuk Wira sudah saya koreksi:\n"
        "----\n"
        "Halo Ka Wira, untuk cek status paket pesanan Ka Wira, silakan kunjungi link berikut: https://jet.co.id/track.\n"
        "Kalau butuh bantuan lain, saya siap membantu ya.\n"
        "----\n"
        "Sudah OK? Ketik 'kirim' untuk saya teruskan ke Ka Wira."
    )

    assert _extract_operator_text_draft(reply).startswith("Halo Ka Wira")
    assert "Julia" not in _extract_operator_text_draft(reply)


def test_extract_operator_text_draft_uses_polite_revision_quote():
    from app.api.channels import _extract_operator_text_draft

    reply = (
        "Berikut draft balasan yang lebih sopan untuk customer Wira - Pendamping Bisnis Sosial Desa:\n\n"
        "\"Terima kasih Bapak/Ibu atas pesan dan pesanan yang telah disampaikan. "
        "Agar kami dapat memproses pengiriman dengan lancar, mohon kesediaannya untuk "
        "menginformasikan alamat lengkap pengiriman. Terima kasih atas kerjasamanya.\"\n\n"
        "Ketik kirim jika sudah sesuai dan ingin saya teruskan ke customer."
    )

    draft = _extract_operator_text_draft(reply)

    assert draft.startswith("Terima kasih Bapak/Ibu")
    assert "Ketik kirim" not in draft


@pytest.mark.asyncio
async def test_pending_operator_text_confirmation_sends_saved_corrected_draft(monkeypatch):
    from app.api.channels import _send_pending_operator_text_reply

    agent_id = uuid.uuid4()
    target = _session(id=uuid.uuid4(), agent_id=agent_id)
    operator = _session(
        id=uuid.uuid4(),
        external_user_id="628operator",
        metadata_={
            "pending_operator_text_reply": {
                "target_session_id": str(target.id),
                "target": "628customer@s.whatsapp.net",
                "case_id": "esc_123456_ab12cd",
                "message": "Halo Ka Wira, silakan cek paket di https://jet.co.id/track.",
            }
        },
    )
    db = _FakeDB()
    db.get_results[target.id] = target
    send_msg = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)

    result = await _send_pending_operator_text_reply(
        operator_session=operator,
        device_id="dev-1",
        operator_reply_target="628operator",
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert result["status"] == "ok"
    assert result["steps"][0]["tool"] == "reply_to_user"
    assert "pending_operator_text_reply" not in operator.metadata_
    assert send_msg.await_args_list[0].args == (
        "dev-1",
        "628customer@s.whatsapp.net",
        "Halo Ka Wira, silakan cek paket di https://jet.co.id/track.",
    )
    assert send_msg.await_args_list[1].args == ("dev-1", "628operator", "Terkirim ✓")


@pytest.mark.asyncio
async def test_expired_pending_operator_text_confirmation_is_cleared(monkeypatch):
    from app.api.channels import _send_pending_operator_text_reply

    target = _session(id=uuid.uuid4())
    operator = _session(
        id=uuid.uuid4(),
        external_user_id="628operator",
        metadata_={
            "pending_operator_text_reply": {
                "target_session_id": str(target.id),
                "target": "628customer@s.whatsapp.net",
                "case_id": "esc_123456_ab12cd",
                "message": "Halo Ka Wira.",
                "expires_at": int(time.time()) - 1,
            }
        },
    )
    db = _FakeDB()
    db.get_results[target.id] = target
    send_msg = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)

    result = await _send_pending_operator_text_reply(
        operator_session=operator,
        device_id="dev-1",
        operator_reply_target="628operator",
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert result["status"] == "error"
    assert "kedaluwarsa" in result["reply"]
    assert "pending_operator_text_reply" not in operator.metadata_
    assert send_msg.await_args.args == ("dev-1", "628operator", result["reply"])


@pytest.mark.asyncio
async def test_operator_confirmation_without_pending_gets_clarifying_reply(monkeypatch):
    from app.api.channels import _reply_no_pending_operator_confirmation

    send_msg = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)

    result = await _reply_no_pending_operator_confirmation(
        device_id="dev-1",
        operator_reply_target="628operator",
    )

    assert result["status"] == "ok"
    assert "Belum ada draft atau lampiran" in result["reply"]
    assert send_msg.await_args.args == ("dev-1", "628operator", result["reply"])


@pytest.mark.asyncio
async def test_operator_payment_approval_resumes_customer_session(monkeypatch):
    from app.api.channels import _resume_customer_workflow_after_operator_approval

    class _Lock:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run_agent(**kwargs):
        assert kwargs["session"] is target
        assert kwargs["user_message"].startswith("[SYSTEM_OPERATOR_APPROVAL]")
        assert "pembayaran customer sudah dikonfirmasi" in kwargs["user_message"]
        return {
            "reply": "CV ATS Anda sedang saya finalkan dan akan saya kirim di sini.",
            "steps": [],
            "run_id": "run-1",
        }

    agent = SimpleNamespace(id=uuid.uuid4(), name="CV Maker")
    target = _session(
        id=uuid.uuid4(),
        channel_config={"user_phone": "628customer@s.whatsapp.net", "device_id": "dev-1"},
        metadata_={},
    )
    db = _FakeDB()
    send_msg = AsyncMock()
    monkeypatch.setattr("app.api.channels.send_wa_message", send_msg)
    monkeypatch.setattr("app.core.engine.agent_runner.run_agent", fake_run_agent)
    monkeypatch.setattr("app.core.engine.session_lock.session_run_lock", lambda _session_id: _Lock())

    result = await _resume_customer_workflow_after_operator_approval(
        agent=agent,
        target_session=target,
        case_id="esc_123456_ab12cd",
        approval_text="iya pembayaran sudah masuk",
        device_id="dev-1",
        operator_reply_target="628operator",
        db=db,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert result["status"] == "ok"
    assert target.metadata_["last_operator_approval"]["type"] == "payment"
    assert send_msg.await_args_list[0].args == (
        "dev-1",
        "628customer@s.whatsapp.net",
        "CV ATS Anda sedang saya finalkan dan akan saya kirim di sini.",
    )
    assert send_msg.await_args_list[1].args == ("dev-1", "628operator", result["reply"])

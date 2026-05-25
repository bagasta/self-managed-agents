from __future__ import annotations

import uuid
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

    async def execute(self, _stmt):
        self.executed += 1
        if self.results:
            return _ScalarResult(self.results.pop(0))
        return _ScalarResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def get(self, _model, key):
        return self.get_results.get(key)

    async def commit(self):
        self.commits += 1


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
async def test_case_lookup_is_strict_without_quote():
    from app.api.wa_helpers import find_session_by_quoted_case

    db = _FakeDB(result=_session())
    session, case_id = await find_session_by_quoted_case(_agent(), db, quoted_text=None)

    assert session is None
    assert case_id is None
    assert db.executed == 0


def test_case_id_parser_accepts_media_caption_case_id():
    from app.api.wa_helpers import extract_escalation_case_id

    text = "Lampiran dari customer untuk kasus esc_1779679409_690800"

    assert extract_escalation_case_id(text) == "esc_1779679409_690800"


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
    assert operator.metadata_["active_escalation_reply"]["target"] == "628customer@s.whatsapp.net"
    send_msg.assert_not_awaited()
    assert db.commits == 2


def test_operator_tools_are_enabled_for_angle_bracket_operator_envelope():
    import inspect
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    src = inspect.getsource(build_agent_tool_setup)
    assert 'user_message.startswith("<OPERATOR>")' in src


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

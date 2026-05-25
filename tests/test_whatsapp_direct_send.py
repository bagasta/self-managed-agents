from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from app.core.engine.agent_runner import (
    _direct_whatsapp_send_guard_reply,
    _extract_direct_whatsapp_confirmation_payload,
    _filter_whatsapp_unsafe_mcp_tools,
    _is_direct_whatsapp_meta_request,
    _is_direct_whatsapp_text_send_context,
    _is_google_chat_intent,
    _is_operator_envelope,
    _prioritize_direct_whatsapp_text_send_tools,
)
from app.core.engine.prompt_builder import build_system_prompt


def _agent(**overrides):
    values = {
        "name": "Yo Besty",
        "model": "gpt-4.1-mini",
        "instructions": "You are helpful.",
        "safety_policy": None,
        "operator_ids": [],
        "escalation_config": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session(**overrides):
    values = {
        "id": "session-1",
        "agent_id": "agent-1",
        "channel_type": "whatsapp",
        "channel_config": {"user_phone": "628111111111"},
        "external_user_id": "628111111111",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _msg(role: str, content: str):
    return SimpleNamespace(role=role, content=content)


def _tool(name: str):
    return SimpleNamespace(name=name)


def test_whatsapp_prompt_allows_user_direct_send_without_unlocking_reply_to_user():
    prompt = build_system_prompt(
        agent_model=_agent(),
        session=_session(),
        active_groups=["escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert "Tool `send_to_number` BOLEH dipakai" in prompt
    assert "langsung panggil `send_to_number` memakai nomor dan draft terakhir dari history" in prompt
    assert "Tool `reply_to_user` HANYA dipakai untuk sesi operator/eskalasi" in prompt
    assert "Tool `reply_to_user` dan `send_to_number` HANYA dipakai saat menerima perintah dari OPERATOR" not in prompt


def test_direct_text_send_context_detects_followup_confirmation_from_history():
    assert _is_direct_whatsapp_text_send_context(
        "yes kirim",
        [
            _msg("user", "tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?"),
            _msg("agent", "Draft untuk Julia: Halo Julia, apakah besok jadi meeting? Ketik kirim untuk mengirim."),
        ],
    )


def test_direct_text_send_context_uses_operator_payload_not_operator_header_phone():
    assert _is_operator_envelope("<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: yes kirim")
    assert not _is_direct_whatsapp_text_send_context(
        "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: halo",
        [],
    )
    assert _is_direct_whatsapp_text_send_context(
        "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: yes kirim",
        [
            _msg("user", "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: tolong kirim pesan ke 6289516247011 tanya besok meeting kah?"),
            _msg("agent", "Draft untuk Julia: Halo Julia, apakah besok jadi meeting? Ketik kirim untuk mengirim."),
        ],
    )


def test_direct_text_send_context_ignores_arthur_repair_request_with_example_phone():
    message = (
        "ada bug serius, gua minta arthur perbaiki agent gua yang gabisa disuruh kirim pesan wa. "
        "Contoh user: tolong kirim pesan wa ke 6289516247011."
    )

    assert _is_direct_whatsapp_meta_request(message)
    assert not _is_direct_whatsapp_text_send_context(message, [])


def test_direct_send_guard_does_not_override_arthur_repair_reply():
    reply = "Saya sudah perbaiki konfigurasi Bas supaya bisa kirim WA ke nomor lain."
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="Arthur, perbaiki agent Bas yang gabisa disuruh kirim pesan WA ke nomor lain.",
    )

    assert guarded == reply


def test_extract_direct_confirmation_payload_from_quoted_draft_history():
    payload = _extract_direct_whatsapp_confirmation_payload(
        "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: yes kirim",
        [
            _msg("user", "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: tolong kirim pesan ke 6289516247011"),
            _msg("agent", 'Draft untuk Julia: "Halo Julia, apakah besok jadi meeting? Mohon konfirmasinya." Ketik kirim.'),
        ],
    )

    assert payload == ("6289516247011", "Halo Julia, apakah besok jadi meeting? Mohon konfirmasinya.")


def test_extract_direct_confirmation_payload_from_unquoted_draft_history():
    payload = _extract_direct_whatsapp_confirmation_payload(
        "kirim",
        [
            _msg("user", "tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?"),
            _msg("agent", "Draft untuk Julia: Halo Julia, apakah besok jadi meeting seperti yang direncanakan? Mohon konfirmasinya. Ketik kirim untuk mengirim."),
        ],
    )

    assert payload == (
        "6289516247011",
        "Halo Julia, apakah besok jadi meeting seperti yang direncanakan? Mohon konfirmasinya.",
    )


def test_extract_direct_confirmation_payload_ignores_non_confirmation():
    assert _extract_direct_whatsapp_confirmation_payload(
        "halo",
        [
            _msg("user", "tolong kirim pesan ke 6289516247011"),
            _msg("agent", 'Draft: "Halo Julia"'),
        ],
    ) is None


def test_direct_text_send_context_does_not_capture_media_requests():
    assert not _is_direct_whatsapp_text_send_context(
        "kirim",
        [
            _msg("user", "tolong kirim gambar ke 6289516247011"),
            _msg("agent", "Siap, ketik kirim."),
        ],
    )


def test_direct_text_send_tool_filter_removes_ambiguous_tools_and_prioritizes_send_to_number():
    tools = [
        _tool("send_message"),
        _tool("send_whatsapp_image"),
        _tool("notify_user"),
        _tool("recall"),
        _tool("send_to_number"),
        _tool("reply_to_user"),
    ]
    filtered = _prioritize_direct_whatsapp_text_send_tools(tools, SimpleNamespace(info=lambda *a, **k: None))
    names = [tool.name for tool in filtered]

    assert names[0] == "send_to_number"
    assert "send_message" not in names
    assert "send_whatsapp_image" not in names
    assert "notify_user" not in names
    assert "reply_to_user" in names


def test_whatsapp_mcp_filter_removes_google_chat_send_message_collision():
    tools = [_tool("send_message"), _tool("create_presentation"), _tool("send_to_number")]
    filtered = _filter_whatsapp_unsafe_mcp_tools(
        tools,
        user_message="<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: kirim pesan ke 6289516247011",
        log=SimpleNamespace(info=lambda *a, **k: None),
    )
    names = [tool.name for tool in filtered]

    assert "send_message" not in names
    assert "create_presentation" in names


def test_whatsapp_mcp_filter_keeps_google_chat_send_message_when_explicit():
    tools = [_tool("send_message"), _tool("create_presentation")]
    message = "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: kirim ke Google Chat spaces/abc"
    filtered = _filter_whatsapp_unsafe_mcp_tools(
        tools,
        user_message=message,
        log=SimpleNamespace(info=lambda *a, **k: None),
    )

    assert _is_google_chat_intent(message)
    assert [tool.name for tool in filtered] == ["send_message", "create_presentation"]


def test_operator_prompt_routes_explicit_other_number_to_send_to_number():
    prompt = build_system_prompt(
        agent_model=_agent(operator_ids=["628111111111"]),
        session=_session(),
        active_groups=["escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=True,
        user_message="tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert "### KIRIM KE NOMOR LAIN" in prompt
    assert "gunakan `send_to_number(phone_or_target, message)`" in prompt
    assert "Jangan gunakan `reply_to_user` untuk nomor lain" in prompt
    assert "langsung panggil `send_to_number` memakai nomor dan draft terakhir dari history" in prompt


def test_escalation_operator_prompt_keeps_reply_to_user_but_allows_other_number_send():
    prompt = build_system_prompt(
        agent_model=_agent(operator_ids=["628111111111"]),
        session=_session(),
        active_groups=["escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid="628222222222@s.whatsapp.net",
        escalation_context="ROUTING: operator_reply_quoted_escalation",
        is_operator_message=True,
        user_message="tolong kirim pesan ke 6289516247011",
    )

    assert "Panggil `reply_to_user(message)`" in prompt
    assert "### KIRIM KE NOMOR LAIN DARI OPERATOR" in prompt
    assert "gunakan `send_to_number(phone_or_target, message)`, BUKAN `reply_to_user`" in prompt


def test_direct_send_guard_blocks_success_claim_without_tool_execution():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="tolong kirim pesan wa ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert guarded != reply
    assert guarded.startswith("Belum saya kirim.")


def test_direct_send_guard_blocks_followup_success_claim_without_tool_execution():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="yes kirim",
    )

    assert guarded != reply
    assert guarded.startswith("Belum saya kirim.")


def test_direct_send_guard_allows_success_after_send_to_number_tool():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[{"tool": "send_to_number", "result": "[SENT_TO_NUMBER:6289516247011] Halo"}],
        user_message="tolong kirim pesan wa ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert guarded == reply


def test_direct_send_guard_allows_success_with_prior_send_history():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="apakah tadi sudah terkirim?",
        history_messages=[
            ToolMessage(
                content="[SENT_TO_NUMBER:6289516247011] Halo Julia",
                name="send_to_number",
                tool_call_id="call_1",
            )
        ],
    )

    assert guarded == reply


def test_direct_send_guard_does_not_touch_escalation_reply_to_user():
    reply = "Terkirim ✓"
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[{"tool": "reply_to_user", "result": "[SENT_TO_USER] Halo"}],
        user_message="kirim",
    )

    assert guarded == reply

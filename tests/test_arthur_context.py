from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.engine.arthur_context import build_arthur_recall, bound_arthur_history, builder_recovery_messages, starts_new_project
from app.core.engine.context_service import db_messages_to_lc, filter_dead_run_messages
from app.core.domain import memory_service


def note(key, value, date="2026-10-14"):
    return SimpleNamespace(key=key, value_data=value, updated_at=date)


def test_recreating_named_project_does_not_recall_other_projects_or_stale_state():
    memories = [
        note("auto_lapibot_purpose", "Wawancara peserta"),
        note("auto_lapibot_opening_requirement", "Peserta boleh berhenti"),
        note("auto_lapibot_status", "Sudah dibuat dan terhubung"),
        note("auto_package_name", "Trial"),
        note("auto_clinic_name", "Klinik lama"),
        note("user_profile", "Ingin membuat klinik"),
        note("auto_lapibot_oauth_link", "https://example.invalid/token"),
        note("daily:2026-10-14", "Error lama" * 4000),
        note("longterm", "Bias lama" * 4000),
    ]
    recall = build_arthur_recall(memories, "buat lapibot")
    assert "Wawancara peserta" in recall
    assert "Peserta boleh berhenti" in recall
    for stale in ["Sudah dibuat", "Trial", "Klinik lama", "Ingin membuat klinik", "example.invalid", "Error lama", "Bias lama"]:
        assert stale not in recall
    assert len(recall) <= 6000


def test_new_project_has_no_implicit_previous_project():
    recall = build_arthur_recall([note("auto_lapibot_purpose", "Old project"), note("auto_user_name", "Owner")], "buat agent baru")
    assert "Old project" not in recall
    assert "Owner" in recall


@pytest.mark.parametrize("message", ["buat agent baru", "buat agentbaru", "bikin assistant lain", "create a new assistant", "buat agent baru untuk klinik, beda dari LapiBot"])
def test_explicit_new_project_isolates_even_a_mentioned_previous_name(message):
    assert starts_new_project(message)
    assert "Old project" not in build_arthur_recall([note("auto_lapibot_purpose", "Old project")], message)


@pytest.mark.parametrize("message", ["buat lapibot", "buat ulang LapiBot", "edit agent", "jangan buat agent baru"])
def test_continuations_and_negated_creation_do_not_reset_context(message):
    assert not starts_new_project(message)


def test_v2_prompt_does_not_inject_retired_builder_tool_contract():
    from app.core.engine.prompt_builder import build_system_prompt
    from arthur_v2.plugin import build_arthur_v2_system_prompt
    agent = SimpleNamespace(id=uuid.uuid4(), name="Arthur", model="anthropic/claude-haiku-5.5", instructions=build_arthur_v2_system_prompt(), tools_config={"system_plugin": "arthur_v2"}, capabilities=["builder"], safety_policy={}, escalation_config={})
    session = SimpleNamespace(id=uuid.uuid4(), agent_id=agent.id, channel_config={}, channel_type="whatsapp", external_user_id="owner", metadata_={})
    prompt = build_system_prompt(agent_model=agent, session=session, active_groups=["builder", "memory"], saved_custom_tools=[], subagent_list=[], sender_name=None, context_summary="", memory_block="", layered_memory={}, rag_context="", escalation_user_jid=None, escalation_context=None, is_operator_message=False)
    assert "create_assistant" in prompt
    assert "compose_agent_blueprint" not in prompt
    assert "get_platform_capabilities" not in prompt
    assert "Project memory boundaries" in prompt


def test_recall_and_history_stay_bounded_without_mutating_storage():
    memories = [note(f"project_requirement_{i}", "x" * 1200) for i in range(200)]
    assert len(build_arthur_recall(memories, "project")) <= 6000
    messages = [HumanMessage(content="old" * 10000), AIMessage(content="latest" * 1000)]
    bounded = bound_arthur_history(messages, max_chars=9000)
    assert sum(len(m.content) for m in bounded) <= 9000
    assert bounded[-1].content == messages[-1].content
    assert len(messages[0].content) == 30000


def test_recovery_preserves_completed_creation_and_discards_truncated_generation():
    human = HumanMessage(content="buat lapibot")
    called = AIMessage(content="", tool_calls=[{"id": "created", "name": "create_assistant", "args": {}}])
    output = ToolMessage(content='{"ok":true,"agent_id":"new"}', tool_call_id="created")
    truncated = AIMessage(content="", response_metadata={"finish_reason": "length"})
    recovered = builder_recovery_messages({"messages": [human, called, output, truncated]}, [human])
    assert recovered[:3] == [human, called, output]
    assert "Do not repeat successful mutations" in recovered[-1].content
    assert len(recovered) == 4


def test_recovery_refuses_tool_call_with_unknown_execution_status():
    pending = AIMessage(content="", tool_calls=[{"id": "unknown", "name": "create_assistant", "args": {}}])
    assert builder_recovery_messages({"messages": [pending]}, []) == []


def test_retry_token_allowance_survives_graph_tool_binding():
    from langchain_openai import ChatOpenAI
    original = ChatOpenAI(model="test", api_key="test", max_tokens=16384)
    retry = original.model_copy(update={"max_tokens": 32768}).bind(parallel_tool_calls=False)
    rebound = retry.bind_tools([{"name": "test_tool", "description": "test", "parameters": {"type": "object", "properties": {}}}])
    assert rebound.bound.max_tokens == 32768
    assert original.max_tokens == 16384


@pytest.mark.parametrize("fallback", [
    "Proses belum menghasilkan jawaban akhir yang terverifikasi. Tidak ada link hasil.",
    "Maaf, respons belum tersedia. Coba kirim ulang pesanmu ya.",
])
def test_existing_fallbacks_are_not_replayed_as_dialogue(fallback):
    rows = [SimpleNamespace(role="user", content="buat lapibot", run_id=None), SimpleNamespace(role="agent", content=fallback, run_id=None)]
    assert len(db_messages_to_lc(rows)) == 1
    assert filter_dead_run_messages(rows, {}) == rows[:1]


@pytest.mark.asyncio
async def test_fallback_does_not_overwrite_project_memory(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(memory_service, "upsert_memory", save)
    await memory_service.record_runtime_memory(agent_id=uuid.uuid4(), db=None, scope="owner", user_message="buat lapibot", final_reply="Maaf, respons belum tersedia. Coba kirim ulang pesanmu ya.")
    save.assert_not_awaited()


def test_completed_creation_is_reported_when_final_generation_is_empty():
    from app.core.engine.reply_guard import ensure_non_empty_reply
    trace = {}
    reply = ensure_non_empty_reply("", [{"tool": "create_assistant", "result": '{"ok":true,"assistant":{"id":"new","name":"LapiBot"}}'}], system_plugin="arthur_v2", decision_trace=trace)
    assert "LapiBot berhasil dibuat" in reply
    assert trace["reason"] == "fallback_verified_assistant"
    assert "http" not in reply


def test_failed_or_readonly_tool_is_not_creation_evidence():
    from app.core.engine.reply_guard import ensure_non_empty_reply
    for step in [{"tool": "get_current_plan", "result": '{"ok":true}'}, {"tool": "create_assistant", "result": '{"ok":false}'}]:
        reply = ensure_non_empty_reply("", [step], system_plugin="arthur_v2")
        assert "berhasil dibuat" not in reply

from types import SimpleNamespace

from app.core.domain.agent_build_state_service import (
    answered_question_topics,
    canonical_question,
    extract_questions,
    guard_repeated_questions,
    infer_workflow_state,
    question_topic,
)
from app.core.engine.arthur_skill_runtime import (
    classify_builder_intent,
    resolve_policy_mixins,
    resolve_primary_skill,
    scope_arthur_builder_tools,
)


def test_intent_and_primary_skill_routing_are_not_beechat_specific():
    assert classify_builder_intent("Saya butuh AI untuk survey pelanggan restoran") == "create"
    assert classify_builder_intent("Tolong edit agent admin klinik") == "edit"
    assert classify_builder_intent("Mau coba nomor demo dulu") == "demo"
    assert classify_builder_intent("Berapa kuota paket saya?") == "subscription"
    assert resolve_primary_skill("create", "discovery") == "arthur-discovery"
    assert resolve_primary_skill("create", "awaiting_confirmation") == "arthur-create-agent"


def test_google_and_file_mixins_are_limited_to_one():
    mixins = resolve_policy_mixins(
        "Simpan hasil survey ke Google Sheets dan baca file PDF",
        "arthur-create-agent",
    )
    assert mixins == ["arthur-google-workspace"]


def test_tool_scoping_removes_material_tools_during_discovery():
    tools = [
        SimpleNamespace(name="plan_agent"),
        SimpleNamespace(name="create_agent"),
        SimpleNamespace(name="delete_agent"),
        SimpleNamespace(name="tavily_search"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-discovery",
        mixin_skills=[],
    )
    assert [tool.name for tool in kept] == ["plan_agent", "tavily_search"]
    assert removed == ["create_agent", "delete_agent"]


def test_google_mixin_adds_auth_tool_without_exposing_create():
    tools = [
        SimpleNamespace(name="plan_agent"),
        SimpleNamespace(name="generate_google_auth_link"),
        SimpleNamespace(name="create_agent"),
    ]
    kept, _removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-discovery",
        mixin_skills=["arthur-google-workspace"],
    )
    assert [tool.name for tool in kept] == ["plan_agent", "generate_google_auth_link"]


def test_question_history_uses_canonical_deduplication():
    reply = "Apa tujuan utama agent?\nApa tujuan utama agent?\nSiapa pengguna agent ini?"
    assert extract_questions(reply) == ["Apa tujuan utama agent?", "Siapa pengguna agent ini?"]
    assert canonical_question("Apa tujuan utama Agent?!") == "apa tujuan utama agent"


def test_runtime_guard_removes_question_already_shown_to_user():
    reply, removed = guard_repeated_questions(
        "Baik.\n\nApakah agent perlu menerima file?",
        [{"canonical": "apakah agent perlu menerima file"}],
    )
    assert reply == "Baik."
    assert removed == ["Apakah agent perlu menerima file?"]


def test_runtime_guard_has_non_empty_fallback_if_everything_was_repeated():
    reply, removed = guard_repeated_questions(
        "Apakah agent perlu menerima file?",
        [{"canonical": "apakah agent perlu menerima file"}],
    )
    assert "tidak akan menanyakannya lagi" in reply
    assert len(removed) == 1


def test_runtime_guard_removes_semantic_topic_paraphrase():
    reply, removed = guard_repeated_questions(
        "Apa masalah utama yang mendorong kamu membuat AI ini?\nKamu kewalahan handle chat sendiri?",
        [{"question": "Apa pain point utama yang ingin diselesaikan?", "canonical": "apa pain point utama yang ingin diselesaikan"}],
    )
    assert question_topic("Apa masalah utama yang mendorong kamu membuat AI ini?") == "pain_point"
    assert len(removed) == 2
    assert "tidak akan menanyakannya lagi" in reply


def test_file_capability_questions_share_one_requirement_slot():
    assert question_topic("Apakah agent perlu menerima PDF?") == "file_capability"
    assert question_topic("Apakah agent akan membuat file atau visualisasi data?") == "file_capability"


def test_guard_checks_questions_beyond_old_three_question_limit():
    reply, removed = guard_repeated_questions(
        "Satu?\nDua?\nTiga?\nApa masalah utama yang ingin diselesaikan?",
        [{"question": "Apa pain point utamanya?", "canonical": "apa pain point utamanya", "topic": "pain_point"}],
    )
    assert removed == ["Apa masalah utama yang ingin diselesaikan?"]
    assert "Satu?" in reply


def test_guard_does_not_reask_explicit_escalation_evidence():
    evidence = [{"status": "answered", "value": "Kalau tidak tahu agent harus eskalasi ke nomor saya."}]
    assert answered_question_topics(evidence) == {"escalation"}
    reply, removed = guard_repeated_questions(
        "Kalau agent tidak tahu, mau diteruskan ke nomor siapa?",
        [],
        evidence,
    )
    assert len(removed) == 1
    assert "tidak akan menanyakannya lagi" in reply


def test_workflow_state_comes_from_verified_steps():
    assert infer_workflow_state("discovery", [{"tool": "create_agent", "result": "{}"}], "") == "agent_created"
    assert infer_workflow_state(
        "discovery",
        [{"tool": "plan_agent", "result": '{"plan_status":"ready"}'}],
        "Silakan konfirmasi.",
    ) == "awaiting_confirmation"
    assert infer_workflow_state("agent_created", [{"tool": "create_wa_dev_trial_link"}], "") == "demo_ready"

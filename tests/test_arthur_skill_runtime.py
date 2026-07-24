from types import SimpleNamespace

from app.core.domain.agent_build_state_service import (
    answered_question_topics,
    canonical_question,
    discovery_snapshot_from_steps,
    extract_questions,
    guard_repeated_questions,
    infer_workflow_state,
    merge_discovery_answers,
    persisted_confirmation_applies,
    question_topic,
)
from app.core.engine.arthur_skill_runtime import (
    classify_builder_intent,
    classify_builder_whatsapp_action,
    normalize_builder_language,
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


def test_prior_demo_evidence_does_not_hijack_confirmation_turn():
    prior = (
        "Buat agent CS Veselmate untuk Veselka. "
        "Setelah jadi saya mau coba nomor demo dulu."
    )

    assert classify_builder_intent("Sesuai", prior) == "create"
    assert resolve_primary_skill("discover", "awaiting_confirmation") == "arthur-create-agent"


def test_explicit_confirmation_exposes_create_skill_even_if_shadow_state_lags():
    assert (
        resolve_primary_skill("create", "discovery", user_message="sudah sesuai")
        == "arthur-create-agent"
    )


def test_setuju_and_direct_create_requests_expose_create_tooling_immediately():
    for message in (
        "setuju",
        "buat",
        "Langsung saja buatkan agentnya",
        "udah bisa dibuat agentnya?",
    ):
        assert (
            resolve_primary_skill("discover", "discovery", user_message=message)
            == "arthur-create-agent"
        )


def test_explicit_current_demo_request_still_wins_over_build_history():
    prior = "Buat agent CS Veselmate untuk Veselka."

    assert classify_builder_intent("Sekarang kirim nomor demo", prior) == "demo"
    assert resolve_primary_skill("demo", "agent_created") == "arthur-whatsapp-demo-channel"


def test_demo_and_channel_followups_route_from_current_or_prior_agent_prompt():
    assert classify_builder_intent("sudah login saya, mau coba agentnya") == "demo"
    assert classify_builder_intent("gimana cara pasang ke whatsappnya?") == "demo"
    assert (
        classify_builder_intent(
            "iya mau",
            prior_agent_message="Mau aku buatin link trial supaya bisa langsung dicoba?",
        )
        == "demo"
    )


def test_informal_demo_request_routes_to_demo_skill():
    message = "mau test pake nomer demo"

    assert normalize_builder_language(message) == "mau coba pakai nomor demo"
    assert classify_builder_whatsapp_action(message) == "trial_link"
    assert classify_builder_intent(message) == "demo"


def test_missing_code_followup_stays_on_demo_path():
    prior = "Minsel sudah aktif di nomor demo Arthur dan siap kamu coba."

    assert (
        classify_builder_whatsapp_action("kodenya mana?", prior)
        == "trial_link"
    )
    assert (
        classify_builder_intent("kodenya mana?", prior_agent_message=prior)
        == "demo"
    )


def test_informal_dedicated_number_requests_route_to_qr():
    for message in (
        "kalo mau konekin ke nomer whatsapp khusus gimana?",
        "minta qr",
        "kirim QR dong",
    ):
        assert classify_builder_whatsapp_action(message) == "dedicated_qr"
        assert classify_builder_intent(message) == "demo"


def test_owned_number_followup_stays_on_dedicated_path():
    prior = (
        "Untuk memasang ke nomor khusus milikmu, pilih nomor khusus "
        "agar saya kirim scan sekali dari WhatsApp."
    )

    assert (
        classify_builder_whatsapp_action("saya udah ada nomernya", prior)
        == "dedicated_qr"
    )
    assert (
        classify_builder_intent(
            "saya udah ada nomernya",
            prior_agent_message=prior,
        )
        == "demo"
    )


def test_demo_skill_exposes_trial_link_and_dedicated_qr_tools():
    tools = [
        SimpleNamespace(name="get_agent_detail"),
        SimpleNamespace(name="create_wa_dev_trial_link"),
        SimpleNamespace(name="send_agent_wa_qr"),
        SimpleNamespace(name="link_dashboard_account"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-whatsapp-demo-channel",
        mixin_skills=[],
    )

    assert [tool.name for tool in kept] == [
        "get_agent_detail",
        "create_wa_dev_trial_link",
        "send_agent_wa_qr",
    ]
    assert removed == ["link_dashboard_account"]


def test_selected_demo_path_hides_qr_tool():
    tools = [
        SimpleNamespace(name="get_agent_detail"),
        SimpleNamespace(name="create_wa_dev_trial_link"),
        SimpleNamespace(name="send_agent_wa_qr"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-whatsapp-demo-channel",
        mixin_skills=[],
        whatsapp_action="trial_link",
    )

    assert [tool.name for tool in kept] == [
        "get_agent_detail",
        "create_wa_dev_trial_link",
    ]
    assert removed == ["send_agent_wa_qr"]


def test_selected_dedicated_path_hides_trial_link_tool():
    tools = [
        SimpleNamespace(name="get_agent_detail"),
        SimpleNamespace(name="create_wa_dev_trial_link"),
        SimpleNamespace(name="send_agent_wa_qr"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-whatsapp-demo-channel",
        mixin_skills=[],
        whatsapp_action="dedicated_qr",
    )

    assert [tool.name for tool in kept] == [
        "get_agent_detail",
        "send_agent_wa_qr",
    ]
    assert removed == ["create_wa_dev_trial_link"]


def test_subscription_skill_does_not_expose_dashboard_linking():
    tools = [
        SimpleNamespace(name="get_user_subscription"),
        SimpleNamespace(name="get_payment_link"),
        SimpleNamespace(name="link_dashboard_account"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-subscription-payment",
        mixin_skills=[],
    )

    assert [tool.name for tool in kept] == [
        "get_user_subscription",
        "get_payment_link",
    ]
    assert removed == ["link_dashboard_account"]


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


def test_google_mixin_keeps_resource_setup_and_verification_tools():
    tools = [
        SimpleNamespace(name="get_agent_detail"),
        SimpleNamespace(name="update_agent"),
        SimpleNamespace(name="create_spreadsheet"),
        SimpleNamespace(name="modify_sheet_values"),
        SimpleNamespace(name="read_sheet_values"),
        SimpleNamespace(name="send_agent_wa_qr"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-create-agent",
        mixin_skills=["arthur-google-workspace"],
    )

    assert [tool.name for tool in kept] == [
        "get_agent_detail",
        "update_agent",
        "create_spreadsheet",
        "modify_sheet_values",
        "read_sheet_values",
    ]
    assert removed == ["send_agent_wa_qr"]


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


def test_guard_uses_canonical_facts_to_remove_rephrased_answered_questions():
    facts = {
        "discovery_answers": {
            "daily_chat_volume": "Puluhan",
            "vision_requirement": "Perlu bisa baca gambar",
        },
        "unresolved_fields": [],
    }
    reply, removed = guard_repeated_questions(
        "Volume harian itu 20-50 atau 50-90 chat per hari?\nAgent perlu bisa lihat gambar?",
        [],
        [],
        facts,
    )

    assert len(removed) == 2
    assert "tidak akan menanyakannya lagi" in reply


def test_partial_plan_payload_merges_verified_persisted_discovery():
    facts = {
        "discovery_answers": {
            "usage_context": "work",
            "daily_chat_volume": "Puluhan",
        },
        "discovery_evidence": {
            "usage_context": "untuk bisnis",
            "daily_chat_volume": "Puluhan",
        },
    }
    merged = merge_discovery_answers(
        {
            "vision_requirement": "Perlu bisa baca gambar",
            "_evidence": {"vision_requirement": "Perlu"},
        },
        facts,
    )

    assert merged["usage_context"] == "work"
    assert merged["daily_chat_volume"] == "Puluhan"
    assert merged["vision_requirement"] == "Perlu bisa baca gambar"
    assert merged["_evidence"]["daily_chat_volume"] == "Puluhan"
    assert "user_confirmed" not in merged


def test_plan_result_persists_facts_and_confirmation_status():
    discovery = {
        "complete": True,
        "normalized_answers": {
            "agent_name": "Minsel",
            "daily_chat_volume": "Puluhan",
            "user_confirmed": True,
        },
        "completed_fields": ["agent_name", "daily_chat_volume"],
        "required_fields": ["agent_name", "daily_chat_volume"],
        "missing_fields": [],
        "invalid_fields": [],
        "verified_evidence_fields": ["agent_name", "daily_chat_volume"],
        "file_capability": "receive_only",
    }
    steps = [
        {
            "tool": "plan_agent",
            "args": {
                "discovery_answers": {
                    "agent_name": "Minsel",
                    "daily_chat_volume": "Puluhan",
                    "user_confirmed": True,
                    "_evidence": {
                        "agent_name": "namanya Minsel",
                        "daily_chat_volume": "Puluhan",
                        "user_confirmed": "sudah",
                    },
                }
            },
            "result": {"plan_status": "ready", "discovery": discovery},
        }
    ]

    facts, confirmation = discovery_snapshot_from_steps({}, steps)

    assert facts["discovery_answers"]["agent_name"] == "Minsel"
    assert facts["discovery_answers"]["user_confirmed"] is True
    assert facts["discovery_evidence"]["daily_chat_volume"] == "Puluhan"
    assert facts["unresolved_fields"] == []
    assert facts["confirmation_verified"] is True
    assert confirmation == "confirmed"


def test_verified_confirmation_is_reused_only_when_confirmed_facts_are_unchanged():
    facts = {
        "confirmation_verified": True,
        "discovery_answers": {
            "agent_name": "Minsel",
            "tone_style": "Professional",
            "user_confirmed": True,
        },
        "discovery_evidence": {"user_confirmed": "sudah sesuai"},
    }

    unchanged = {"agent_name": "Minsel", "tone_style": "Professional"}
    changed = {"agent_name": "Minsel Baru", "tone_style": "Professional"}

    assert persisted_confirmation_applies(unchanged, facts) is True
    assert merge_discovery_answers(unchanged, facts)["user_confirmed"] is True
    assert persisted_confirmation_applies(changed, facts) is False
    assert "user_confirmed" not in merge_discovery_answers(changed, facts)


def test_workflow_state_comes_from_verified_steps():
    assert infer_workflow_state(
        "discovery",
        [{"tool": "create_agent", "result": '{"success":true,"agent_id":"agent-1"}'}],
        "",
    ) == "agent_created"
    assert infer_workflow_state(
        "discovery",
        [{"tool": "create_agent", "result": "Error: create_agent is not a valid tool"}],
        "",
    ) == "discovery"
    assert infer_workflow_state(
        "discovery",
        [{"tool": "plan_agent", "result": '{"plan_status":"ready"}'}],
        "Silakan konfirmasi.",
    ) == "ready_to_create"
    assert infer_workflow_state(
        "agent_created",
        [{"tool": "create_wa_dev_trial_link", "result": '{"success":true}'}],
        "",
    ) == "demo_ready"

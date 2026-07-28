import json
from types import SimpleNamespace

from app.core.domain.agent_build_state_service import (
    _integration_artifact_status_from_steps,
    answered_question_topics,
    canonical_discovery_manifest,
    canonical_question,
    discovery_snapshot_from_steps,
    extract_questions,
    guard_repeated_questions,
    guard_single_discovery_question,
    infer_workflow_state,
    merge_discovery_answers,
    persisted_confirmation_applies,
    question_topic,
)
from app.core.engine.arthur_skill_runtime import (
    classify_builder_intent,
    classify_builder_whatsapp_action,
    normalize_builder_language,
    resolve_builder_payment_plan_selection,
    resolve_policy_mixins,
    resolve_primary_skill,
    scope_arthur_builder_tools,
    scope_arthur_create_completion_tools,
)
from app.core.engine.agent_followups import _needs_builder_plan_completion


def test_intent_and_primary_skill_routing_are_not_beechat_specific():
    assert classify_builder_intent("Saya butuh AI untuk survey pelanggan restoran") == "create"
    assert classify_builder_intent("Tolong edit agent admin klinik") == "edit"
    assert classify_builder_intent("Mau coba nomor demo dulu") == "demo"
    assert classify_builder_intent("Berapa kuota paket saya?") == "subscription"
    assert resolve_primary_skill("create", "discovery") == "arthur-discovery"
    assert resolve_primary_skill("create", "awaiting_confirmation") == "arthur-create-agent"


def test_builder_management_state_machine_routes_each_goal_to_its_primary_skill():
    cases = (
        ("create", "discovery", "arthur-discovery"),
        ("create", "awaiting_confirmation", "arthur-create-agent"),
        ("demo", "agent_created", "arthur-whatsapp-demo-channel"),
        ("edit", "agent_created", "arthur-edit-agent"),
        ("lifecycle", "agent_created", "arthur-lifecycle-safety"),
    )

    for intent, state, expected_skill in cases:
        assert resolve_primary_skill(intent, state) == expected_skill


def test_existing_agent_complaint_routes_to_edit_with_media_context():
    message = "ini kok dia gabisa nerima foto struk??"

    assert normalize_builder_language(message) == "ini kok dia tidak bisa menerima foto struk??"
    assert classify_builder_intent(message) == "edit"
    assert resolve_primary_skill(
        classify_builder_intent(message),
        "setup_pending",
        user_message=message,
    ) == "arthur-edit-agent"
    assert resolve_policy_mixins(message, "arthur-edit-agent") == [
        "arthur-files-knowledge"
    ]


def test_existing_agent_failure_does_not_run_new_agent_subscription_path():
    for message in (
        "agentnya gagal baca gambar",
        "JuleAI agentnya bermasalah waktu catat struk",
        "kok dia belum bisa kirim file?",
    ):
        assert classify_builder_intent(message) == "edit"


def test_payment_plan_selection_keeps_subscription_skill_on_short_followup():
    prior = (
        "Mau ambil paket yang mana? Starter, Pro, atau Enterprise. "
        "Sebutkan pilihanmu, nanti saya buatkan link pembayaran."
    )

    assert resolve_builder_payment_plan_selection("mau yang enterprise", prior) == "tier_3"
    assert resolve_builder_payment_plan_selection("Pro", prior) == "tier_2"
    assert resolve_builder_payment_plan_selection("Starter", prior) == "tier_1"
    assert (
        classify_builder_intent(
            "mau yang enterprise",
            prior_agent_message=prior,
        )
        == "subscription"
    )


def test_plan_word_without_billing_context_does_not_hijack_intent():
    assert resolve_builder_payment_plan_selection("buat gaya profesional", "") is None


def test_payment_boundary_answer_does_not_hijack_active_agent_build():
    assert (
        classify_builder_intent(
            "mencatat pesanan boleh, mengonfirmasi pembayaran tidak boleh.",
            prior_evidence="Saya mau buat agent CS untuk bisnis mukena.",
        )
        == "create"
    )
    assert classify_builder_intent("saya mau bayar paket Pro") == "subscription"


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


def test_conversational_confirmation_exposes_create_skill_without_false_negative():
    for message in (
        "sip sudah sesuai",
        "Sip, sudah sesuai!",
        "iya semuanya sesuai 🙏",
        "mantap saya setuju",
        "sip sudah sesuai ya",
    ):
        assert (
            resolve_primary_skill("create", "discovery", user_message=message)
            == "arthur-create-agent"
        )

    assert (
        resolve_primary_skill("create", "discovery", user_message="belum sesuai")
        == "arthur-discovery"
    )
    assert (
        resolve_primary_skill(
            "create",
            "discovery",
            user_message="belum semuanya sesuai",
        )
        == "arthur-discovery"
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


def test_demo_request_cannot_hide_create_tools_during_unfinished_build():
    assert resolve_primary_skill("demo", "discovery") == "arthur-discovery"
    assert resolve_primary_skill("demo", "ready_to_create") == "arthur-create-agent"
    assert (
        resolve_primary_skill("demo", "integration_auth_pending")
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


def test_google_and_file_capability_skills_compose_together():
    mixins = resolve_policy_mixins(
        "Simpan hasil survey ke Google Sheets dan baca file PDF",
        "arthur-create-agent",
    )
    assert mixins == [
        "arthur-google-workspace",
        "arthur-files-knowledge",
    ]


def test_tool_scoping_removes_material_tools_during_discovery():
    tools = [
        SimpleNamespace(name="plan_agent"),
        SimpleNamespace(name="list_my_agents"),
        SimpleNamespace(name="create_agent"),
        SimpleNamespace(name="delete_agent"),
        SimpleNamespace(name="tavily_search"),
    ]
    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-discovery",
        mixin_skills=[],
    )
    assert [tool.name for tool in kept] == [
        "plan_agent",
        "list_my_agents",
        "tavily_search",
    ]
    assert removed == ["create_agent", "delete_agent"]


def test_discovery_turn_without_plan_is_forced_through_planning_gate():
    assert _needs_builder_plan_completion(
        [],
        is_builder=True,
        primary_skill="arthur-discovery",
        workflow_state="discovery",
    ) is True
    assert _needs_builder_plan_completion(
        [{"tool": "plan_agent", "result": "{}"}],
        is_builder=True,
        primary_skill="arthur-discovery",
        workflow_state="discovery",
    ) is False


def test_post_create_integration_setup_does_not_restart_discovery_plan():
    assert _needs_builder_plan_completion(
        [],
        is_builder=True,
        primary_skill="arthur-create-agent",
        workflow_state="integration_auth_pending",
    ) is False


def test_edit_scope_exposes_diagnostics_and_update_but_not_create_or_delete():
    tools = [
        SimpleNamespace(name="get_platform_capabilities"),
        SimpleNamespace(name="list_my_agents"),
        SimpleNamespace(name="get_agent_detail"),
        SimpleNamespace(name="verify_agent"),
        SimpleNamespace(name="update_agent"),
        SimpleNamespace(name="plan_agent"),
        SimpleNamespace(name="create_agent"),
        SimpleNamespace(name="delete_agent"),
    ]

    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-edit-agent",
        mixin_skills=["arthur-files-knowledge"],
    )

    assert [tool.name for tool in kept] == [
        "get_platform_capabilities",
        "list_my_agents",
        "get_agent_detail",
        "verify_agent",
        "update_agent",
    ]
    assert removed == ["create_agent", "delete_agent", "plan_agent"]


def test_ready_plan_completion_scope_exposes_create_but_cannot_replan():
    tools = [
        SimpleNamespace(name="plan_agent"),
        SimpleNamespace(name="compose_agent_blueprint"),
        SimpleNamespace(name="validate_agent_config"),
        SimpleNamespace(name="create_agent"),
        SimpleNamespace(name="verify_agent"),
        SimpleNamespace(name="delete_agent"),
    ]

    kept, removed = scope_arthur_create_completion_tools(
        tools,
        mixin_skills=[],
    )

    assert [tool.name for tool in kept] == [
        "compose_agent_blueprint",
        "validate_agent_config",
        "create_agent",
        "verify_agent",
    ]
    assert removed == ["delete_agent", "plan_agent"]


def test_google_mixin_does_not_expose_mutation_tools_during_discovery():
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
    assert [tool.name for tool in kept] == ["plan_agent"]


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


def test_oauth_completion_can_finish_google_setup_then_selected_demo_path():
    tools = [
        SimpleNamespace(name="create_spreadsheet"),
        SimpleNamespace(name="modify_sheet_values"),
        SimpleNamespace(name="update_agent"),
        SimpleNamespace(name="verify_agent"),
        SimpleNamespace(name="create_wa_dev_trial_link"),
        SimpleNamespace(name="send_agent_wa_qr"),
    ]

    kept, removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-create-agent",
        mixin_skills=["arthur-google-workspace"],
        whatsapp_action="trial_link",
    )

    assert [tool.name for tool in kept] == [
        "create_spreadsheet",
        "modify_sheet_values",
        "update_agent",
        "verify_agent",
        "create_wa_dev_trial_link",
    ]
    assert removed == ["send_agent_wa_qr"]


def test_question_history_uses_canonical_deduplication():
    reply = "Apa tujuan utama agent?\nApa tujuan utama agent?\nSiapa pengguna agent ini?"
    assert extract_questions(reply) == ["Apa tujuan utama agent?", "Siapa pengguna agent ini?"]
    assert canonical_question("Apa tujuan utama Agent?!") == "apa tujuan utama agent"


def test_runtime_guard_keeps_only_one_discovery_question():
    reply, removed = guard_single_discovery_question(
        "Baik.\n\n1. Apa masalah utamanya?\n2. Siapa penggunanya?\n3. Apa nama agentnya?"
    )

    assert reply == "Baik.\n\n1. Apa masalah utamanya?"
    assert removed == ["2. Siapa penggunanya?", "3. Apa nama agentnya?"]


def test_runtime_guard_removes_balanced_bold_question_without_leaking_markdown():
    reply, removed = guard_single_discovery_question(
        "Baik.\n\n**Apa masalah utamanya?**\n**Siapa penggunanya?** Misalnya orang tua murid."
    )

    assert reply == "Baik.\n\n**Apa masalah utamanya?**\nMisalnya orang tua murid."
    assert removed == ["**Siapa penggunanya?"]
    assert reply.count("**") == 2


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


def test_question_guard_never_mutilates_inline_dialogue_example():
    reply = (
        "Berikan 2-3 contoh percakapan ideal. Contoh format: "
        "`Customer: Apakah stok masih ada?` lalu "
        "`Agent: Saya cek dari sumber yang tersedia; kalau belum pasti saya eskalasikan ke admin.`"
    )

    assert extract_questions(reply, max_questions=12) == []
    cleaned, removed = guard_repeated_questions(
        reply,
        [],
        [{"status": "answered", "value": "Contohnya kamu sesuaikan aja."}],
    )

    assert cleaned == reply
    assert removed == []
    assert "` lalu `Agent:" in cleaned


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

    facts, confirmation = discovery_snapshot_from_steps(
        {},
        steps,
        confirmation_message_id="msg-confirm-1",
    )

    assert facts["discovery_answers"]["agent_name"] == "Minsel"
    assert facts["discovery_answers"]["user_confirmed"] is True
    assert facts["discovery_evidence"]["daily_chat_volume"] == "Puluhan"
    assert facts["unresolved_fields"] == []
    assert facts["confirmation_verified"] is True
    assert facts["agent_manifest"]["agent_name"] == "Minsel"
    assert facts["manifest_version"] == 1
    assert facts["confirmed_manifest_hash"] == facts["manifest_hash"]
    assert facts["confirmation_message_id"] == "msg-confirm-1"
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


def test_canonical_manifest_ignores_confirmation_and_evidence_wrappers():
    first = canonical_discovery_manifest(
        {
            "agent_name": "Minsel",
            "capabilities": "Menerima file",
            "file_capability": "receive_only",
            "user_confirmed": True,
            "_evidence": {"agent_name": "namanya Minsel"},
        }
    )
    second = canonical_discovery_manifest(
        {
            "file_capability": "receive_only",
            "capabilities": "Menerima file",
            "agent_name": "Minsel",
        }
    )

    assert first == second


def test_manifest_version_only_changes_when_canonical_requirements_change():
    ready_discovery = {
        "complete": True,
        "normalized_answers": {
            "agent_name": "Minsel",
            "capabilities": "Menerima file",
            "file_capability": "receive_only",
            "user_confirmed": True,
        },
        "completed_fields": ["agent_name", "capabilities"],
        "required_fields": ["agent_name", "capabilities"],
        "missing_fields": [],
        "invalid_fields": [],
        "file_capability": "receive_only",
    }
    first, _ = discovery_snapshot_from_steps(
        {},
        [{"tool": "plan_agent", "args": {}, "result": {"plan_status": "ready", "discovery": ready_discovery}}],
        confirmation_message_id="msg-1",
    )
    unchanged, _ = discovery_snapshot_from_steps(
        first,
        [{"tool": "plan_agent", "args": {}, "result": {"plan_status": "ready", "discovery": ready_discovery}}],
        confirmation_message_id="msg-2",
    )
    changed_discovery = json.loads(json.dumps(ready_discovery))
    changed_discovery["normalized_answers"]["agent_name"] = "Minsel Baru"
    changed, _ = discovery_snapshot_from_steps(
        unchanged,
        [{"tool": "plan_agent", "args": {}, "result": {"plan_status": "ready", "discovery": changed_discovery}}],
        confirmation_message_id="msg-3",
    )

    assert first["manifest_version"] == 1
    assert unchanged["manifest_version"] == 1
    assert changed["manifest_version"] == 2
    assert changed["manifest_hash"] != first["manifest_hash"]


def test_google_resource_and_write_status_survive_restart_state():
    draft = SimpleNamespace(
        integration_status_json={},
        artifact_status_json={},
    )
    steps = [
        {
            "tool": "create_spreadsheet",
            "result": (
                "Successfully created spreadsheet 'Keuangan'. "
                "ID: sheet123456789 | URL: "
                "https://docs.google.com/spreadsheets/d/sheet123456789/edit"
            ),
        },
        {
            "tool": "modify_sheet_values",
            "result": '{"success":true,"updatedRows":2}',
        },
        {
            "tool": "update_agent",
            "result": '{"success":true}',
        },
    ]

    integrations, artifacts = _integration_artifact_status_from_steps(
        draft,
        steps,
    )

    assert integrations["google_workspace"]["status"] == "configured"
    assert artifacts["google_sheet"]["spreadsheet_id"] == "sheet123456789"
    assert artifacts["google_sheet"]["write_verified"] is True
    assert artifacts["google_sheet"]["bound_to_agent"] is True


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
        "discovery",
        [
            {"tool": "plan_agent", "result": '{"plan_status":"needs_clarification"}'},
            {"tool": "plan_agent", "result": '{"plan_status":"ready"}'},
        ],
        "",
    ) == "ready_to_create"
    assert infer_workflow_state(
        "agent_created",
        [{"tool": "create_wa_dev_trial_link", "result": '{"success":true}'}],
        "",
    ) == "demo_ready"


def test_discovery_snapshot_persists_canonical_file_capability():
    facts, confirmation = discovery_snapshot_from_steps(
        {},
        [
            {
                "tool": "plan_agent",
                "args": {"discovery_answers": {"capabilities": "terima dan buat file"}},
                "result": json.dumps(
                    {
                        "plan_status": "ready",
                        "discovery": {
                            "complete": True,
                            "normalized_answers": {
                                "capabilities": "terima dan buat file",
                                "file_capability": "both",
                            },
                            "completed_fields": ["capabilities"],
                            "file_capability": "both",
                        },
                    }
                ),
            }
        ],
    )

    assert confirmation == "confirmed"
    assert facts["file_capability"] == "both"
    assert facts["discovery_answers"]["file_capability"] == "both"

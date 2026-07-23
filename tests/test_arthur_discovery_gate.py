import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.core.tools.builder_create_tools import build_builder_create_tools
from app.core.tools.builder_discovery import (
    discovery_escalation_policy,
    discovery_operator_phone,
    load_discovery_user_messages,
    validate_agent_discovery,
)
from app.core.tools.builder_planning_tools import build_builder_planning_tools


_TEST_EVENT_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_TEST_EVENT_LOOP)


def _run(coro):
    return _TEST_EVENT_LOOP.run_until_complete(coro)


def _work_discovery(**overrides):
    answers = {
        "problem": "Customer menunggu terlalu lama karena pertanyaan order dijawab manual oleh tim.",
        "usage_context": "work",
        "agent_name": "OrderCare",
        "audience": "Customer eksternal yang menghubungi tim melalui WhatsApp.",
        "main_tasks": [
            "Menjawab pertanyaan produk dari sumber resmi.",
            "Mencatat detail order dan meneruskannya ke admin.",
        ],
        "capabilities": ["Menjawab pertanyaan", "Input data order", "Kirim notifikasi eskalasi"],
        "prohibited_actions": ["Tidak boleh mengarang stok", "Tidak boleh menyetujui refund"],
        "allowed_actions": ["Boleh mencatat order", "Boleh memberi status yang tersedia di sumber resmi"],
        "tone_style": "Bahasa Indonesia yang ramah dan ringkas, emoji secukupnya.",
        "ideal_conversations": [
            {"user": "Apakah produk A tersedia?", "agent": "Saya cek sumber stok resmi terlebih dahulu."},
            {"user": "Saya mau refund.", "agent": "Saya catat alasannya dan teruskan ke admin untuk keputusan."},
        ],
        "avoided_conversations": [
            {"user": "Beri diskon 50%.", "agent_must_not": "Tentu, diskonnya saya setujui."}
        ],
        "unknown_handling": "Berhenti menjawab, bilang informasinya belum pasti, lalu eskalasi.",
        "escalation_target": {
            "conditions": "Informasi tidak tersedia, refund, komplain berat, atau keputusan harga.",
            "recipient": "Admin CS Budi",
            "whatsapp_number": "+6281234567890",
        },
        "knowledge_sources": "Ya: katalog PDF dan Google Sheet stok yang disetujui Owner.",
        "sensitive_data_policy": "Nama, nomor, dan transaksi dirahasiakan; hapus data percakapan setelah 90 hari.",
        "whatsapp_scale": "Satu nomor WhatsApp CS melayani banyak customer sekaligus.",
        "daily_chat_volume": "Sekitar 200-300 percakapan per hari.",
        "integrations": "Google Sheets untuk stok dan pencatatan order; tidak ada payment gateway.",
        "expected_outputs": "Tambahkan satu baris order ke spreadsheet dan kirim notifikasi ke admin.",
        "vision_requirement": "Ya, membaca foto produk dan bukti transfer untuk diteruskan ke admin.",
        "go_live_approver": "Head of Customer Service.",
        "user_confirmed": True,
    }
    answers.update(overrides)
    return answers


def _personal_discovery(**overrides):
    answers = {
        "problem": "Saya sering lupa tugas pribadi dan kehilangan catatan tindak lanjut.",
        "usage_context": "personal",
        "agent_name": "IngatAku",
        "audience": "Saya sendiri.",
        "main_tasks": ["Mencatat tugas", "Mengingatkan tindak lanjut"],
        "capabilities": ["Input catatan", "Kirim pengingat", "Hanya chat teks; tidak perlu file"],
        "prohibited_actions": ["Tidak boleh mengarang jadwal"],
        "allowed_actions": ["Boleh mencatat dan mengingatkan setelah saya minta"],
        "tone_style": "Santai, bahasa Indonesia, tanpa emoji berlebihan.",
        "ideal_conversations": [
            {"user": "Ingatkan beli obat besok.", "agent": "Siap, saya catat pengingat untuk besok."},
            {"user": "Apa tugas saya?", "agent": "Berikut tugas yang pernah kamu catat."},
        ],
        "avoided_conversations": [
            {"user": "Apa jadwal saya?", "agent_must_not": "Kamu ada rapat jam 10, padahal tidak pernah dicatat."}
        ],
        "unknown_handling": "Bilang tidak tahu dan minta saya memberi informasi yang benar.",
        "knowledge_sources": "Tidak perlu RAG atau sumber tambahan.",
        "sensitive_data_policy": "Catatan bersifat pribadi dan tidak boleh dibagikan.",
        "whatsapp_scale": "Satu nomor WhatsApp pribadi hanya untuk saya sendiri.",
        "daily_chat_volume": "Sekitar 10-20 chat per hari.",
        "integrations": "Tidak perlu integrasi lain.",
        "expected_outputs": "Kirim pengingat singkat di WhatsApp dan tampilkan daftar tugas saat diminta.",
        "vision_requirement": "Tidak perlu gambar atau vision.",
        "user_confirmed": True,
    }
    answers.update(overrides)
    return answers


def test_discovery_starts_with_all_group_one_questions():
    result = validate_agent_discovery({})

    assert result["complete"] is False
    assert result["next_group"]["id"] == "context_goal"
    assert [item["topic"] for item in result["next_questions"]] == [
        "problem",
        "usage_context",
        "agent_name",
        "audience",
    ]
    assert result["operational_hours_requested"] is False


def test_group_two_questions_include_examples_for_hard_to_answer_items():
    result = validate_agent_discovery(
        {
            "problem": "Customer lama menunggu jawaban order.",
            "usage_context": "work",
            "agent_name": "OrderCare",
            "audience": "Customer eksternal.",
        }
    )

    assert result["next_group"]["id"] == "agent_behavior"
    questions = {item["topic"]: item["question"] for item in result["next_questions"]}
    assert "Contoh:" in questions["tone_style"]
    assert "2-3 contoh" in questions["ideal_conversations"]
    assert "red line" in questions["avoided_conversations"]


def test_capabilities_must_include_an_explicit_file_decision():
    answers = _personal_discovery(
        capabilities=["Menjawab pertanyaan", "Mengirim notifikasi"]
    )

    result = validate_agent_discovery(answers)

    assert result["complete"] is False
    assert "capabilities" in result["invalid_fields"]
    assert result["next_group"]["id"] == "agent_behavior"
    question = next(
        item["question"] for item in result["next_questions"]
        if item["topic"] == "capabilities"
    )
    assert "hanya chat teks" in question
    assert "menerima file" in question
    assert "membuat file" in question


def test_confirmed_file_workflow_is_derived_from_discovery():
    result = validate_agent_discovery(_work_discovery())

    assert result["complete"] is True
    assert result["file_capability"] == "receive_only"


def test_problem_must_be_a_pain_point_not_only_an_agent_feature():
    result = validate_agent_discovery(
        {
            "problem": "Buat agent customer service",
            "usage_context": "work",
            "agent_name": "OrderCare",
            "audience": "Customer eksternal.",
        }
    )

    assert "problem" in result["invalid_fields"]
    assert result["next_group"]["id"] == "context_goal"


def test_whatsapp_scale_question_requires_number_and_user_distribution_pattern():
    answers = _personal_discovery()
    answers["whatsapp_scale"] = "Satu nomor WhatsApp."

    result = validate_agent_discovery(answers)

    assert "whatsapp_scale" in result["invalid_fields"]
    assert result["next_group"]["id"] == "scale_integration"
    question = next(
        item["question"] for item in result["next_questions"] if item["topic"] == "whatsapp_scale"
    )
    assert "satu nomor melayani banyak user" in question
    assert "setiap user memiliki nomor sendiri" in question


def test_work_discovery_requires_detailed_escalation_and_go_live_approver():
    answers = _work_discovery()
    answers["escalation_target"] = "ke admin"
    answers.pop("go_live_approver")

    result = validate_agent_discovery(answers)

    assert result["complete"] is False
    assert "escalation_target" in result["invalid_fields"]
    assert "go_live_approver" in result["missing_fields"]


def test_personal_discovery_skips_phone_and_go_live_approver():
    result = validate_agent_discovery(_personal_discovery())

    assert result["complete"] is True
    assert result["skipped_for_personal"] == ["escalation_target", "go_live_approver"]
    assert "escalation_target" not in result["required_fields"]
    assert "go_live_approver" not in result["required_fields"]


def test_personal_discovery_preserves_an_optional_human_fallback():
    result = validate_agent_discovery(
        _personal_discovery(
            escalation_target={
                "conditions": "Saya meminta bantuan manusia.",
                "recipient": "Asisten pribadi",
                "whatsapp_number": "+6281234567890",
            }
        )
    )

    assert result["complete"] is True
    assert discovery_escalation_policy(result) == "operator"
    assert discovery_operator_phone(result) == "+6281234567890"


def test_operational_hours_are_ignored_and_never_become_a_question():
    answers = _personal_discovery(operational_hours="24/7", jam_aktif="09.00-17.00")

    result = validate_agent_discovery(answers)

    assert result["complete"] is True
    assert result["ignored_fields"] == ["jam_aktif", "operational_hours"]
    assert "operational_hours" not in result["normalized_answers"]
    assert all("operasional" not in item["question"].lower() for item in result["next_questions"])


def test_confirmation_is_a_separate_final_gate():
    answers = _personal_discovery()
    answers.pop("user_confirmed")

    result = validate_agent_discovery(answers)

    assert result["complete"] is False
    assert result["next_group"]["id"] == "confirmation"
    assert result["next_questions"][0]["topic"] == "user_confirmed"


def test_plan_agent_blocks_until_discovery_is_complete():
    async def preview_agent_creation_entitlement(**_kwargs):
        return {"checked": True, "allowed": True}

    plan = build_builder_planning_tools(
        preview_agent_creation_entitlement=preview_agent_creation_entitlement
    )["plan_agent"]
    payload = json.loads(
        _run(
            plan.ainvoke(
                {
                    "user_goal": "Buat agent CS",
                    "agent_name": "OrderCare",
                    "discovery_answers": {
                        "problem": "Customer lama menunggu jawaban order.",
                        "usage_context": "work",
                        "agent_name": "OrderCare",
                        "audience": "Customer eksternal.",
                    },
                }
            )
        )
    )

    assert payload["plan_status"] == "needs_clarification"
    assert payload["discovery_progress"]["next_group"]["id"] == "agent_behavior"


def test_plan_agent_is_ready_with_confirmed_personal_discovery():
    async def preview_agent_creation_entitlement(**_kwargs):
        return {"checked": True, "allowed": True}

    plan = build_builder_planning_tools(
        preview_agent_creation_entitlement=preview_agent_creation_entitlement
    )["plan_agent"]
    payload = json.loads(
        _run(
            plan.ainvoke(
                {
                    "user_goal": "Agent pengingat pribadi",
                    "agent_name": "IngatAku",
                    "requested_features": "reminder,text_only",
                    "discovery_answers": _personal_discovery(),
                }
            )
        )
    )

    assert payload["plan_status"] == "ready"
    assert payload["discovery"]["complete"] is True
    assert payload["confirmed_discovery"]["problem"].startswith("Saya sering lupa")
    assert payload["escalation_policy"] == "none"
    assert payload["recommended_config"]["file_capability"] == "text_only"
    assert payload["recommended_config"]["tools_config"]["whatsapp_media"] is False


def test_arthur_create_agent_hard_blocks_without_discovery():
    create_agent = build_builder_create_tools(
        None,
        owner_phone="628111111111",
        self_agent_id=str(uuid.uuid4()),
        append_platform_staff_identity_instruction=lambda text: (text, False),
        append_google_workspace_instruction=lambda text: (text, False),
        platform_staff_identity_block=lambda **_kwargs: "",
    )["create_agent"]

    payload = json.loads(
        _run(
            create_agent.ainvoke(
                {
                    "name": "OrderCare",
                    "instructions": "Instruksi lengkap dan faktual. " * 20,
                }
            )
        )
    )

    assert "Discovery kebutuhan agent belum lengkap" in payload["error"]
    assert payload["discovery_progress"]["next_group"]["id"] == "context_goal"


def test_arthur_work_agent_requires_manual_from_confirmed_discovery():
    create_agent = build_builder_create_tools(
        None,
        owner_phone="628111111111",
        self_agent_id=str(uuid.uuid4()),
        append_platform_staff_identity_instruction=lambda text: (text, False),
        append_google_workspace_instruction=lambda text: (text, False),
        platform_staff_identity_block=lambda **_kwargs: "",
    )["create_agent"]

    payload = json.loads(
        _run(
            create_agent.ainvoke(
                {
                    "name": "OrderCare",
                    "instructions": "Instruksi lengkap dan faktual. " * 20,
                    "file_capability": "text_only",
                    "discovery_answers": _work_discovery(),
                }
            )
        )
    )

    assert "Operating manual terkonfirmasi wajib diisi" in payload["error"]
    assert payload["validation_errors"]


def _discovery_with_persisted_evidence(answers):
    payload = dict(answers)
    evidence = {}
    messages = []
    for field, value in answers.items():
        if field == "user_confirmed":
            continue
        quote = f"Jawaban saya untuk {field}: {json.dumps(value, ensure_ascii=False)}"
        evidence[field] = quote
        messages.append(quote)
    evidence["user_confirmed"] = "sudah sesuai"
    messages.append("sudah sesuai")
    payload["_evidence"] = evidence
    return payload, messages


def test_evidence_backed_discovery_accepts_only_persisted_user_quotes():
    answers, messages = _discovery_with_persisted_evidence(_work_discovery())

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is True
    assert result["missing_evidence_fields"] == []
    assert set(result["verified_evidence_fields"]) == set(result["required_fields"])
    assert "_evidence" not in result["normalized_answers"]


def test_evidence_accepts_close_paraphrase_but_resolves_to_persisted_user_message():
    answers, messages = _discovery_with_persisted_evidence(_work_discovery())
    raw_user_message = (
        "Tugasnya jawab pertanyaan produk, catat detail order, lalu teruskan ke admin."
    )
    messages[4] = raw_user_message
    answers["_evidence"]["main_tasks"] = (
        "Menjawab pertanyaan produk, mencatat order, dan meneruskannya kepada admin."
    )

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is True
    assert "main_tasks" in result["verified_evidence_fields"]


def test_short_sudah_is_valid_only_as_latest_explicit_confirmation():
    answers, messages = _discovery_with_persisted_evidence(_personal_discovery())
    answers["user_confirmed"] = "sudah"
    answers["_evidence"]["user_confirmed"] = "sudah"
    messages[-1] = "sudah"

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is True

    messages.append("Tapi ubah tugas utamanya dulu.")
    changed_result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )
    assert changed_result["complete"] is False
    assert changed_result["next_group"]["id"] == "confirmation"


def test_confirmed_final_summary_can_evidence_a_user_delegated_detail():
    answers, messages = _discovery_with_persisted_evidence(_personal_discovery())
    original_quote = answers["_evidence"]["ideal_conversations"]
    messages.remove(original_quote)
    confirmed_summary = (
        "Rangkuman contoh percakapan ideal: "
        + json.dumps(answers["ideal_conversations"], ensure_ascii=False)
    )
    messages.insert(-1, confirmed_summary)
    answers["_evidence"]["ideal_conversations"] = confirmed_summary

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is True
    assert "ideal_conversations" in result["verified_evidence_fields"]


def test_runtime_evidence_includes_only_the_summary_immediately_confirmed_by_user():
    session_id = str(uuid.uuid4())
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [
                ("user", "Tolong sesuaikan contoh percakapannya."),
                ("agent", "Rangkuman: contoh ideal A dan B."),
                ("user", "sudah"),
            ]
        )
    )
    db_factory = MagicMock()
    db_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    db_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    evidence = _run(load_discovery_user_messages(db_factory, session_id))

    assert evidence == [
        "Tolong sesuaikan contoh percakapannya.",
        "Rangkuman: contoh ideal A dan B.",
        "sudah",
    ]

    db.execute.return_value = SimpleNamespace(
        all=lambda: [
            ("user", "Tolong sesuaikan contoh percakapannya."),
            ("agent", "Rangkuman: contoh ideal A dan B."),
            ("user", "Tapi ubah contoh A dulu."),
        ]
    )
    unconfirmed_evidence = _run(load_discovery_user_messages(db_factory, session_id))
    assert unconfirmed_evidence == [
        "Tolong sesuaikan contoh percakapannya.",
        "Tapi ubah contoh A dulu.",
    ]


def test_discovery_rejects_model_invented_answers_without_user_evidence():
    answers = _work_discovery()
    answers["_evidence"] = {
        field: f"Kutipan buatan untuk {field}"
        for field in answers
        if field != "user_confirmed"
    }
    answers["_evidence"]["user_confirmed"] = "sudah sesuai"

    result = validate_agent_discovery(
        answers,
        user_messages=["Saya mau dibantu membuat agent.", "sudah sesuai"],
        require_evidence=True,
    )

    assert result["complete"] is False
    assert "problem" in result["missing_evidence_fields"]
    assert result["next_group"]["id"] == "context_goal"
    assert any("Jangan mengarang" in error for error in result["validation_errors"])


def test_real_but_unrelated_quote_cannot_justify_an_invented_field():
    answers, messages = _discovery_with_persisted_evidence(_personal_discovery())
    unrelated_quote = "Saya ingin agent untuk pekerjaan bisnis."
    messages.append(unrelated_quote)
    answers["_evidence"]["prohibited_actions"] = unrelated_quote

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is False
    assert "prohibited_actions" in result["unsupported_evidence_fields"]
    assert result["next_group"]["id"] == "agent_behavior"
    assert any("tidak mendukung isi jawaban" in error for error in result["validation_errors"])


def test_generic_oke_or_iya_cannot_confirm_the_full_agent_brief():
    answers, messages = _discovery_with_persisted_evidence(_personal_discovery())
    answers["_evidence"]["user_confirmed"] = "iya"
    messages[-1] = "iya"

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is False
    assert result["next_group"]["id"] == "confirmation"
    assert "user_confirmed" in result["missing_evidence_fields"]


def test_explicit_confirmation_must_be_the_latest_user_message():
    answers, messages = _discovery_with_persisted_evidence(_personal_discovery())
    messages.append("Tapi ubah tugas utamanya dulu.")

    result = validate_agent_discovery(
        answers,
        user_messages=messages,
        require_evidence=True,
    )

    assert result["complete"] is False
    assert result["next_group"]["id"] == "confirmation"


def test_create_agent_retry_returns_committed_agent_as_idempotent_success():
    session_id = str(uuid.uuid4())
    owner_phone = "628111111111"
    agent_name = "OpsMate"
    request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"arthur-create:{session_id}:{owner_phone}:{agent_name.casefold()}",
        )
    )
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        name=agent_name,
        model="openai/gpt-4.1-mini",
        channel_type="whatsapp",
        tools_config={"_builder_creation_request_id": request_id},
        api_key="existing-key",
        token_quota=4_000_000,
        active_until=None,
    )
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = existing
    db = MagicMock()
    db.execute = AsyncMock(return_value=scalar_result)

    class _DbContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    def db_factory():
        return _DbContext()

    create_agent = build_builder_create_tools(
        db_factory,
        owner_phone=owner_phone,
        self_agent_id=None,
        session_id=session_id,
        append_platform_staff_identity_instruction=lambda text, **_kwargs: (text, False),
        append_google_workspace_instruction=lambda text, **_kwargs: (text, False),
        platform_staff_identity_block=lambda **_kwargs: "",
    )["create_agent"]

    payload = json.loads(
        _run(
            create_agent.ainvoke(
                {
                    "name": agent_name,
                    "instructions": "Jawab sesuai sumber dan jangan mengarang informasi.",
                    "channel_type": "whatsapp",
                    "file_capability": "text_only",
                }
            )
        )
    )

    assert payload["success"] is True
    assert payload["idempotent_replay"] is True
    assert payload["agent_id"] == str(existing.id)
    assert "jangan membuat duplikat" in payload["message"].lower()

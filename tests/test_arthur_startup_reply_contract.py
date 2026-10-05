from types import SimpleNamespace

from arthur_v2.plugin import (
    apply_arthur_first_turn_staffing_contract,
    arthur_staffing_onboarding_active,
    build_arthur_v2_system_prompt,
    guard_arthur_staffing_reply,
    is_arthur_first_turn_staffing_request,
)


def test_new_owner_staffing_first_reply_contract_overrides_intake_checklists():
    message = (
        "Gua founder startup kecil B2B SaaS, inbound leads dan support bikin kewalahan. "
        "Gua pengen punya tim AI yang bantu handle kerjaan bisnis."
    )
    assert is_arthur_first_turn_staffing_request(message)
    assert not is_arthur_first_turn_staffing_request("Halo, siapa kamu?")
    assert not is_arthur_first_turn_staffing_request("Buat draft email untuk lead ini.")

    # Earlier prompt blocks contain generic rapid-intake and workflow-discovery
    # instructions. The narrow first-turn rule must be the final appended block.
    prompt = apply_arthur_first_turn_staffing_contract(
        "Earlier prompt: rapid intake may ask three questions; discover workflow fields.",
        message=message,
        is_fresh_session=True,
    )
    assert prompt.endswith(
        "Do not call tools or create/configure an assistant. Wait for the owner's answer."
    )
    assert "exactly one plain open question" in prompt
    assert "exactly one question mark" in prompt
    assert "which single task takes the most time or causes the most pain" in prompt
    assert "Do not use a list, bullets, numbered items, a second question" in prompt
    assert apply_arthur_first_turn_staffing_contract(
        "earlier", message=message, is_fresh_session=False
    ) == "earlier"
    assert apply_arthur_first_turn_staffing_contract(
        "earlier", message="Buat draft email untuk lead ini.", is_fresh_session=True
    ) == "earlier"


def test_canonical_prompt_sets_one_question_startup_expectation():
    prompt = build_arthur_v2_system_prompt()
    assert "which single task takes the most time or causes the most pain" in prompt
    assert "Do not recommend\nroles, list capabilities, ask about integrations" in prompt


def test_staffing_onboarding_guard_tracks_startup_but_releases_explicit_agent_build():
    history = [
        SimpleNamespace(
            role="user",
            content="Aku punya usaha dimsum dan ingin punya tim AI untuk bantu operasional.",
        )
    ]
    assert arthur_staffing_onboarding_active(history, "Sekarang pesanan masih masuk lewat chat.")
    assert arthur_staffing_onboarding_active(history, "Menurutmu perlu bikin agent CS WhatsApp?")
    assert not arthur_staffing_onboarding_active(history, "Tolong bikin agent CS WhatsApp sekarang.")
    assert not arthur_staffing_onboarding_active(
        history,
        "Bantu jelaskan fotosintesis dengan sederhana.",
    )
    assert arthur_staffing_onboarding_active(
        [], "Aku punya usaha dimsum dan ingin punya tim AI untuk bantu operasional."
    )


def test_staffing_gate_releases_later_sentence_build_request_but_not_incidental_mention():
    history = [
        SimpleNamespace(
            role="user",
            content="Aku punya usaha dimsum dan ingin punya tim AI untuk bantu operasional.",
        )
    ]
    explicit_later_action = (
        "Aku jualan dimsum, order masuk lewat WhatsApp dan stok sering dicek manual. "
        "Tolong buatkan agent pencatat order dan staf pengecek stok."
    )

    # This is the exact predicate used by run_agent before it can replace the
    # production tool list with []; false means tools stay enabled and the
    # staffing-only reply guard is not applied.
    onboarding_active = arthur_staffing_onboarding_active(history, explicit_later_action)
    assert not onboarding_active
    reply = "Saya akan cek roster staf yang tersedia lalu membagi tugasnya."
    if onboarding_active:
        reply, _reason = guard_arthur_staffing_reply(reply, explicit_later_action)
    assert reply == "Saya akan cek roster staf yang tersedia lalu membagi tugasnya."

    # A question or incidental mention is still discovery, not authorization
    # to create or dispatch anything.
    incidental = "Menurutmu perlu bikin agent pencatat order dan pengecek stok?"
    onboarding_active = arthur_staffing_onboarding_active(history, incidental)
    assert onboarding_active
    guarded_reply, _reason = guard_arthur_staffing_reply(
        "Saya akan cek roster staf yang tersedia lalu membagi tugasnya.",
        incidental,
    )
    assert guarded_reply != "Saya akan cek roster staf yang tersedia lalu membagi tugasnya."


def test_staffing_gate_releases_detailed_two_staf_creation_and_dispatch_request():
    history = [
        SimpleNamespace(
            role="user",
            content="Aku punya usaha dimsum dan ingin punya tim AI untuk bantu operasional.",
        )
    ]
    current_message = (
        "Tolong buatkan dua staf internal yang sudah saya minta: Analis Penjualan untuk "
        "menghitung omzet dari data pesanan, dan Pemeriksa Stok untuk menilai ketersediaan "
        "bahan. Saya menyetujui pembuatan kedua agent ini. Setelah keduanya dibuat, tugaskan "
        "Analis Penjualan menghitung omzet dan minta Pemeriksa Stok meninjau stok serta "
        "melaporkan hasilnya."
    )

    # run_agent uses this predicate to decide whether to empty its tool list and
    # apply the discovery-only reply guard. An explicit multi-step request must
    # retain both production tools and the existing owner-confirmation contract.
    onboarding_active = arthur_staffing_onboarding_active(history, current_message)
    assert not onboarding_active
    reply = "Saya akan baca roster pemilik dan menjalankan dispatch yang disetujui."
    if onboarding_active:
        reply, _reason = guard_arthur_staffing_reply(reply, current_message)
    assert reply == "Saya akan baca roster pemilik dan menjalankan dispatch yang disetujui."


def test_staffing_reply_guard_keeps_a_grounded_single_question_reply():
    reply = "Oke, aku paham kamu jualan dimsum. Bagian kerja mana yang paling menyita waktumu?"
    assert guard_arthur_staffing_reply(reply, "Aku jualan dimsum.") == (reply, None)


def test_staffing_reply_guard_starts_with_owner_stated_business_priorities():
    safe_reply, reason = guard_arthur_staffing_reply(
        "Inbound leads dan support pasti bisa diotomatisasi. Apa yang paling penting?",
        "Gue founder startup B2B. Inbound leads dan support bikin kewalahan.",
    )
    assert reason == "unverified_claim_or_checklist"
    assert "inbound leads dan support" in safe_reply
    assert "Dari dua area itu" in safe_reply
    assert safe_reply.count("?") == 1


def test_staffing_reply_guard_replaces_question_flood_with_grounded_safe_reply():
    reply = (
        "Fakta terkonfirmasi: semua chat dikelola manual.\n"
        "1. Aku akan hubungkan WhatsApp dan CRM.\n"
        "2. Kita hemat 10 jam per minggu.\n"
        "Berapa chat masuk? Siapa yang membalas? Pakai CRM apa? Kapan mau mulai?"
    )
    safe_reply, reason = guard_arthur_staffing_reply(
        reply,
        "Aku jualan dimsum, order sering datang dari WhatsApp.",
    )
    assert reason == "multiple_questions"
    assert "kebutuhan yang kamu ceritakan" in safe_reply
    assert safe_reply.count("?") == 1
    assert "hemat 10 jam" not in safe_reply


def test_staffing_reply_guard_replaces_unverified_completion_claim():
    safe_reply, reason = guard_arthur_staffing_reply(
        "Agent CS sudah terhubung ke WhatsApp. Apa mau saya uji?",
        "Aku perlu bantuan melayani pelanggan.",
    )
    assert reason == "unverified_claim_or_checklist"
    assert safe_reply.count("?") == 1
    assert "sudah terhubung" not in safe_reply


def test_staffing_reply_guard_does_not_present_unshared_process_as_a_fact():
    safe_reply, reason = guard_arthur_staffing_reply(
        "Semua pesanan saat ini dikelola manual. Bagian mana yang paling menyita waktu?",
        "Aku punya usaha dimsum.",
    )
    assert reason == "unverified_claim_or_checklist"
    assert "dikelola manual" not in safe_reply
    assert safe_reply.count("?") == 1


def test_staffing_reply_guard_advances_from_known_lead_work_to_one_priority_question():
    safe_reply, reason = guard_arthur_staffing_reply(
        "Semua proses harus diotomatisasi. Buat CS, Lead Qualifier, Follow-up, dan CRM. "
        "Berapa lead masuk? Berapa yang closing? Siapa balas? Pakai CRM apa?",
        "Sekarang yang paling makan waktu itu jawab chat calon pelanggan dan follow up lead.",
        "Aku startup B2B. Support dan inbound leads bikin kewalahan.",
    )
    assert reason == "multiple_questions"
    assert "membalas chat calon pelanggan" in safe_reply
    assert "balasan awal atau follow-up lead?" in safe_reply
    assert safe_reply.count("?") == 1


def test_staffing_reply_guard_asks_about_content_channel_after_owner_adds_marketing():
    safe_reply, reason = guard_arthur_staffing_reply(
        "Tim marketing bisa langsung posting di semua channel. Konten apa? Budget berapa?",
        "Selain itu gue pengen konten marketing rutin. Belum ada CRM yang gue pilih.",
        "Yang makan waktu jawab chat calon pelanggan dan follow-up lead.",
    )
    assert reason == "multiple_questions"
    assert "CRM belum kamu pilih" in safe_reply
    assert "Channel mana yang paling penting" in safe_reply
    assert safe_reply.count("?") == 1


def test_staffing_reply_guard_adds_a_next_step_question_if_model_omits_one():
    safe_reply, reason = guard_arthur_staffing_reply(
        "Kamu ingin fokus pada inbound leads dan support.",
        "Inbound leads dan support bikin kewalahan.",
    )
    assert reason == "missing_question"
    assert "Dari dua area itu" in safe_reply
    assert safe_reply.count("?") == 1

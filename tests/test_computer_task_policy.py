from app.core.engine.computer_task_policy import (
    computer_task_contract,
    guard_computer_task_reply,
    requires_visual_verification,
    requires_computer_task,
)
from app.core.engine.model_capabilities import model_supports_image_input


def test_operator_browser_model_receives_visual_computer_tools():
    assert model_supports_image_input("anthropic/claude-haiku-5.5")


def test_browser_and_url_requests_require_computer():
    assert requires_computer_task("Buka browser lalu cari harga laptop di Google.")
    assert requires_computer_task("Buka https://example.com dan klik tombol masuk.")
    assert not requires_computer_task("Tulis ulang paragraf ini dengan bahasa yang lebih santai.")


def test_creating_an_lms_assistant_is_not_a_computer_task():
    request = (
        "Buatkan bot admin LMS Clevio untuk monitor laporan lesson dan "
        "menambah atau edit lesson berdasarkan perintah saya."
    )
    assert not requires_computer_task(request)
    reply, reason = guard_computer_task_reply(
        "Saya perlu tahu LMS mana yang dipakai sebelum menyusun workflow botnya.",
        [],
        request,
        computer_enabled=True,
    )
    assert reason is None
    assert reply.startswith("Saya perlu tahu")


def test_answer_about_lms_browser_access_does_not_trigger_computer_guard():
    request = (
        "Menjawab pertanyaan bot (pesan af23e44b): Bagaimana bot akan mengakses LMS Clevio? "
        "Pilihan: LMS diakses lewat browser; saya akan login sendiri jika diminta."
    )
    assert not requires_computer_task(request)
    reply, reason = guard_computer_task_reply(
        "Baik, saya akan siapkan alur admin LMS lewat browser dan menunggu login Anda.",
        [], request, computer_enabled=True,
    )
    assert reason is None
    assert reply.startswith("Baik")


def test_explicit_lms_browser_action_still_requires_computer():
    assert requires_computer_task("Tolong buka LMS Clevio lewat browser dan cek lesson yang ada.")


def test_future_bot_work_is_not_current_computer_request():
    request = "semuanya kan nanti dia buka pake komputer jadi harusnya tau kalo ada laporan apa aja"
    assert not requires_computer_task(request)
    proposal = (
        "Usulan bot Admin LMS Clevio: nanti bot memeriksa laporan yang terlihat di LMS. "
        "Mau aku buat bot dengan batasan ini?"
    )
    assert guard_computer_task_reply(proposal, [], request, computer_enabled=True) == (proposal, None)
    assert not requires_computer_task("Ya, buat bot Admin LMS Clevio sesuai usulan")


def test_computer_guard_preserves_clarification_for_real_browser_request():
    request = "Tolong buka LMS Clevio lewat browser dan cek lesson yang ada."
    clarification = "Saya perlu alamat LMS Clevio sebelum bisa membukanya. Apa URL-nya?"
    assert guard_computer_task_reply(clarification, [], request, computer_enabled=True) == (clarification, None)


def test_computer_guard_rejects_unverified_browser_result():
    reply, reason = guard_computer_task_reply(
        "Saya sudah membuka LMS dan menemukan tiga lesson baru.", [],
        "Tolong buka LMS Clevio lewat browser dan cek lesson yang ada.",
        computer_enabled=True,
    )
    assert reason == "missing_computer_action"
    assert "tidak akan mengklaim" in reply


def test_computer_contract_forbids_text_fetch_substitution():
    contract = computer_task_contract()
    assert "computer_screenshot" in contract
    assert "HTTP/fetch teks" in contract
    assert "tidak mengubah screenshot" in contract


def test_browser_reply_is_blocked_without_computer_action():
    reply, reason = guard_computer_task_reply(
        "Berhasil, ini hasil dari website.",
        [],
        "Tolong buka https://example.com dan ambil judulnya.",
        computer_enabled=True,
    )

    assert reason == "missing_computer_action"
    assert "tidak akan mengklaim" in reply


def test_browser_reply_requires_observed_computer_action():
    reply, reason = guard_computer_task_reply(
        "Berhasil.",
        [{"tool": "computer_open_url", "result": '{"ok": true, "status": "action_sent"}'}],
        "Cari berita terbaru lewat browser.",
        computer_enabled=True,
    )

    assert reason == "missing_computer_observation"
    assert "screenshot observasi" in reply

    allowed, allowed_reason = guard_computer_task_reply(
        "Berhasil.",
        [
            {"tool": "computer_open_url", "result": '{"ok": true, "observation": "attached"}'},
            {"tool": "computer_screenshot", "result": "screen attached"},
        ],
        "Cari berita terbaru lewat browser.",
        computer_enabled=True,
    )
    assert (allowed, allowed_reason) == ("Berhasil.", None)


def test_visual_design_completion_needs_accepted_screen_review():
    steps = [
        {"tool": "computer_click", "result": '{"ok": true, "observation": "attached"}'},
        {"tool": "computer_screenshot", "result": "screen attached"},
    ]
    reply, reason = guard_computer_task_reply(
        "Poster sudah jadi.", steps, "Buat poster di Canva", computer_enabled=True,
    )
    assert reason == "missing_visual_completion_verification"
    assert "belum dapat memverifikasi" in reply

    allowed, allowed_reason = guard_computer_task_reply(
        "Poster sudah jadi.", steps, "Buat poster di Canva", computer_enabled=True,
        decision_events=[{"visual_progress": {"choice": "complete", "accepted": True}}],
    )
    assert (allowed, allowed_reason) == ("Poster sudah jadi.", None)


def test_only_visual_tasks_need_visual_completion_review():
    assert requires_visual_verification("Buat poster di Canva")
    assert not requires_visual_verification("Cari berita melalui browser")

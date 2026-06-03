from app.core.engine.wa_progress import build_progress_message, build_task_done_message
from app.core.engine.subagent_builder import should_expose_wa_media_tools
from app.core.engine.wa_reply_delivery import should_skip_whatsapp_final_reply


def test_task_progress_is_suppressed_for_whatsapp_noise():
    msg = build_progress_message(
        "task",
        '{"name":"sys_coder","task":"Bikin redesign portfolio dark-bold ala Russell Brand lalu deploy."}',
    )
    assert msg is None


def test_read_file_progress_includes_path():
    msg = build_progress_message("read_file", {"path": "/workspace/index.html"})
    assert msg == "📖 Membaca file: /workspace/index.html"


def test_http_get_progress_includes_url():
    msg = build_progress_message("http_get", {"url": "https://www.russellbrand.com/"})
    assert msg is not None
    assert "russellbrand.com" in msg


def test_task_done_message_prioritizes_url_from_output():
    done = build_task_done_message(
        {"name": "sys_coder", "task": "deploy landing page"},
        "Deploy sukses. URL publik: https://abc-123.trycloudflare.com",
    )
    assert done == "✅ sys_coder selesai. URL: https://abc-123.trycloudflare.com"


def test_task_done_message_fallback_to_output_preview():
    done = build_task_done_message(
        {"name": "sys_coder", "task": "refactor"},
        "Semua file berhasil diperbarui dan validasi syntax lolos.",
    )
    assert done is not None
    assert done.startswith("✅ sys_coder selesai:")


def test_wa_media_tools_are_not_exposed_to_subagents():
    assert should_expose_wa_media_tools("loh berikan ke saya dalam bentuk link aja") is False
    assert should_expose_wa_media_tools("kirim hasilnya dalam bentuk PDF") is False
    assert should_expose_wa_media_tools("kirim filenya aja") is False
    assert should_expose_wa_media_tools("kirim langsung ke saya") is False


def test_duplicate_notify_user_final_reply_is_suppressed():
    reply = "Website sudah selesai. Link: https://nowhere-claire-exams-however.trycloudflare.com"
    steps = [
        {
            "tool": "notify_user",
            "args": {"message": "Website sudah selesai. Link: https://nowhere-claire-exams-however.trycloudflare.com"},
        }
    ]

    assert should_skip_whatsapp_final_reply(reply, steps) is True


def test_media_send_success_final_reply_without_url_is_suppressed():
    steps = [{"tool": "send_whatsapp_document", "args": {"filename": "Link Website.txt"}}]

    assert should_skip_whatsapp_final_reply("File sudah saya kirim ke WhatsApp kamu.", steps) is True
    assert should_skip_whatsapp_final_reply("Link: https://example.trycloudflare.com", steps) is False


def test_task_guard_does_not_override_arthur_planning_after_document_upload():
    from app.core.engine.agent_runner import _task_result_guard_reply

    reply = "Saya sudah paham kebutuhan dan data produk kamu. Saya akan buat rencana agent CS WhatsApp dulu."
    steps = [
        {
            "tool": "task",
            "args": {"task": "Baca knowledge base produk dan rangkum kebutuhan agent CS WhatsApp."},
            "result": "Knowledge base terbaca. Kebutuhan agent sudah diringkas.",
        }
    ]

    assert _task_result_guard_reply(reply, steps, "[Dokumen diterima: produk.pdf]\nnih") == reply


def test_task_guard_still_blocks_unfinished_deploy_promises():
    from app.core.engine.agent_runner import _task_result_guard_reply

    reply = "Website sedang saya deploy, nanti saya kirim linknya."
    steps = [
        {
            "tool": "task",
            "args": {"task": "Buat website toko dan deploy ke public URL."},
            "result": "File sudah dibuat, tapi deployment belum berhasil dan URL belum tersedia.",
        }
    ]

    guarded = _task_result_guard_reply(reply, steps, "buat website toko dan kasih link")

    assert guarded.startswith("Belum selesai.")


def test_parent_delivery_followup_needed_for_subagent_shared_pdf():
    from app.core.engine.agent_runner import _needs_whatsapp_file_delivery_followup

    steps = [
        {
            "tool": "task",
            "args": {"name": "sys_coder"},
            "result": (
                "CV ATS-friendly selesai dibuat di "
                "/workspace/shared/CV_Bagas_Automation_Specialist.pdf — SIAP_DIKIRIM_PARENT."
            ),
        }
    ]

    needed, path = _needs_whatsapp_file_delivery_followup(
        "kirim file pdf nya",
        {"whatsapp_media": True},
        steps,
        "File sudah siap.",
    )

    assert needed is True
    assert path == "/workspace/shared/CV_Bagas_Automation_Specialist.pdf"


def test_parent_delivery_followup_not_needed_after_parent_document_send():
    from app.core.engine.agent_runner import (
        _needs_whatsapp_file_delivery_followup,
        _task_result_guard_reply,
    )

    steps = [
        {
            "tool": "task",
            "args": {"name": "sys_coder"},
            "result": (
                "Sebelumnya file CV belum tersedia, lalu final dibuat di "
                "/workspace/shared/CV_Bagas_Automation_Specialist.pdf — SIAP_DIKIRIM_PARENT."
            ),
        },
        {
            "tool": "send_whatsapp_document",
            "args": {
                "file_path_or_base64": "/workspace/shared/CV_Bagas_Automation_Specialist.pdf",
                "filename": "CV_Bagas_Automation_Specialist.pdf",
            },
            "result": "[DOCUMENT_SENT] Dokumen 'CV_Bagas_Automation_Specialist.pdf' dikirim ke 628123",
        },
    ]

    needed, path = _needs_whatsapp_file_delivery_followup(
        "kirim file pdf nya",
        {"whatsapp_media": True},
        steps,
        "File sudah saya kirim.",
    )
    guarded = _task_result_guard_reply(
        "File sudah saya kirim.",
        steps,
        "kirim file pdf nya",
    )

    assert needed is False
    assert path is None
    assert guarded == "File sudah saya kirim."

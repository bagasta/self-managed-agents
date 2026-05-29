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


def test_link_only_request_does_not_expose_wa_media_tools_to_subagents():
    assert should_expose_wa_media_tools("loh berikan ke saya dalam bentuk link aja") is False
    assert should_expose_wa_media_tools("kirim hasilnya dalam bentuk PDF") is True


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

from app.core.engine.wa_progress import build_progress_message, build_task_done_message


def test_task_progress_uses_real_subagent_and_task_text():
    msg = build_progress_message(
        "task",
        '{"name":"sys_coder","task":"Bikin redesign portfolio dark-bold ala Russell Brand lalu deploy."}',
    )
    assert msg is not None
    assert "sys_coder" in msg
    assert "redesign portfolio" in msg.lower()


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

from app.core.engine.reply_guard import ensure_non_empty_reply


def test_keep_existing_reply():
    assert ensure_non_empty_reply("Halo jadi", []) == "Halo jadi"


def test_build_reply_from_url_in_steps():
    steps = [{"tool": "task", "result": "done https://abc.trycloudflare.com"}]
    out = ensure_non_empty_reply("", steps)
    assert "https://abc.trycloudflare.com" in out


def test_build_reply_from_tool_names_when_no_url():
    steps = [
        {"tool": "read_file", "result": "ok"},
        {"tool": "write_file", "result": "ok"},
    ]
    out = ensure_non_empty_reply("", steps)
    assert "read_file" in out


def test_generic_when_no_steps():
    out = ensure_non_empty_reply("", [])
    assert "coba kirim ulang" in out.lower() or "gangguan" in out.lower()

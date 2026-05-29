from app.core.engine.reply_guard import ensure_non_empty_reply


def test_keep_existing_reply():
    assert ensure_non_empty_reply("Halo jadi", []) == "Halo jadi"


def test_build_reply_from_url_in_steps():
    steps = [{"tool": "task", "result": "done https://abc.trycloudflare.com"}]
    out = ensure_non_empty_reply("", steps)
    assert "https://abc.trycloudflare.com" in out


def test_build_reply_without_tool_names_when_no_url():
    steps = [
        {"tool": "read_file", "result": "ok"},
        {"tool": "write_file", "result": "ok"},
    ]
    out = ensure_non_empty_reply("", steps)
    assert "read_file" not in out
    assert "write_file" not in out
    assert "Prosesnya" in out


def test_builder_create_agent_success_reply_is_natural():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": true, "name": "Travgent", "agent_id": "agent-123"}',
        }
    ]
    out = ensure_non_empty_reply("", steps)
    assert "Travgent sudah jadi" in out
    assert "agent-123" in out
    assert "create_agent" not in out


def test_builder_create_agent_success_overrides_unclear_non_empty_reply():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": true, "name": "Travgent", "agent_id": "agent-123"}',
        }
    ]
    out = ensure_non_empty_reply("Soul sudah siap.", steps)
    assert out == "Travgent sudah jadi. ID agent: agent-123."


def test_builder_update_agent_success_reply_is_natural():
    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_name": "Travgent", "updated_fields": ["instructions"]}',
        }
    ]
    out = ensure_non_empty_reply("", steps)
    assert "Travgent berhasil diupdate" in out
    assert "instructions" in out
    assert "update_agent" not in out


def test_builder_update_agent_success_overrides_progress_promise():
    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_name": "Zimit", "updated_fields": ["tools_config"]}',
        }
    ]
    out = ensure_non_empty_reply("Oke langsung aku betulin semua ya!", steps)
    assert out == "Zimit berhasil diupdate. Field yang diubah: tools_config."


def test_builder_partial_flow_does_not_claim_agent_created():
    steps = [
        {"tool": "plan_agent", "result": "ok"},
        {"tool": "compose_agent_blueprint", "result": "ok"},
        {"tool": "compose_agent_instructions", "result": "ok"},
    ]
    out = ensure_non_empty_reply("", steps)
    assert "belum berhasil dibuat" in out.lower()
    assert "plan_agent" not in out
    assert "compose_agent" not in out


def test_builder_partial_soul_reply_is_overridden():
    steps = [
        {"tool": "plan_agent", "result": "ok"},
        {"tool": "compose_agent_instructions", "result": "ok"},
        {"tool": "compose_agent_soul", "result": "ok"},
    ]
    out = ensure_non_empty_reply("Soul sudah siap, mau saya lanjut buat agent?", steps)
    assert "belum berhasil dibuat" in out.lower()
    assert "soul" not in out.lower()


def test_generic_when_no_steps():
    out = ensure_non_empty_reply("", [])
    assert "coba kirim ulang" in out.lower() or "gangguan" in out.lower()

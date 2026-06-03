from app.core.engine.reply_guard import ensure_non_empty_reply


def test_builder_google_auth_agent_id_detects_update_needs_auth():
    from app.core.engine.agent_runner import _builder_google_auth_agent_id

    agent_id = "11111111-1111-4111-8111-111111111111"
    steps = [
        {
            "tool": "update_agent",
            "result": (
                '{"success": true, "agent_id": "%s", "google_workspace_enabled": true, '
                '"needs_google_auth": true}'
            ) % agent_id,
        }
    ]

    assert _builder_google_auth_agent_id(steps) == agent_id


def test_builder_google_auth_agent_id_skips_when_tool_already_called():
    from app.core.engine.agent_runner import _builder_google_auth_agent_id

    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_id": "11111111-1111-4111-8111-111111111111", "needs_google_auth": true}',
        },
        {"tool": "generate_google_auth_link", "result": '{"auth_url": "https://auth.example.test"}'},
    ]

    assert _builder_google_auth_agent_id(steps) is None


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


def test_builder_create_whatsapp_agent_success_includes_onboarding_options():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": true, "name": "CVin aja", "agent_id": "agent-123", "channel_type": "whatsapp"}',
        }
    ]
    out = ensure_non_empty_reply("", steps)
    assert "CVin aja sudah jadi" in out
    assert "nomor WhatsApp kamu sendiri" in out
    assert "nomor demo Arthur" in out
    assert "agent-123" not in out


def test_builder_create_whatsapp_agent_overrides_id_only_reply():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": true, "name": "CVin aja", "agent_id": "agent-123", "channel_type": "whatsapp"}',
        }
    ]
    out = ensure_non_empty_reply("CVin aja sudah jadi. ID agent: agent-123.", steps)
    assert out == (
        "CVin aja sudah jadi. Sekarang mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, "
        "atau dicoba dulu lewat nomor demo Arthur yang sudah siap pakai?"
    )


def test_builder_update_agent_success_reply_is_natural():
    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_name": "Travgent", "updated_fields": ["instructions"]}',
        }
    ]
    out = ensure_non_empty_reply("", steps)
    assert out == "Travgent sudah saya edit."
    assert "instructions" not in out
    assert "update_agent" not in out


def test_builder_update_agent_success_overrides_progress_promise():
    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_name": "Zimit", "updated_fields": ["tools_config"]}',
        }
    ]
    out = ensure_non_empty_reply("Oke langsung aku betulin semua ya!", steps)
    assert out == "Zimit sudah saya edit."


def test_builder_update_keeps_substantive_user_facing_reply():
    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_name": "CVin aja", "updated_fields": ["instructions"]}',
        }
    ]
    reply = (
        'Agent "CVin aja" sudah saya perbarui alurnya sesuai permintaanmu, Bagas. '
        "Sekarang agent akan minta pembayaran dulu, minta bukti transfer ke admin, "
        "eskalasi bukti untuk approval, baru kirim CV ke customer setelah approved."
    )

    assert ensure_non_empty_reply(reply, steps) == reply


def test_builder_create_agent_entitlement_reply_is_rewritten_to_retry():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": false, "error": "Konfigurasi agent melebihi entitlement plan."}',
        }
    ]
    reply = (
        'Agent "CVin aja" sudah siap dengan fitur yang kamu minta, tapi sayangnya paket Trial kamu '
        "tidak mengizinkan penggunaan sub-agent yang diperlukan untuk fitur lengkap ini. "
        "Kamu perlu upgrade paket agar bisa pakai semua fitur."
    )

    out = ensure_non_empty_reply(reply, steps)
    assert "coba ulang" in out.lower()
    assert "preset" not in out.lower()
    assert "upgrade" not in out.lower()


def test_builder_entitlement_error_forces_retry_reply():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": false, "error": "Konfigurasi agent melebihi entitlement plan."}',
        }
    ]
    reply = (
        "Sepertinya ada masalah teknis yang membuat instruksi agent tidak berhasil dibuat secara otomatis. "
        "Saya sarankan kita buat agent dengan preset CS WhatsApp Basic dulu versi sederhana tanpa sub-agent, "
        "supaya kamu bisa langsung coba. Nanti kalau sudah siap, kita upgrade lagi untuk fitur lengkap dengan Google Drive dan approval admin."
    )

    out = ensure_non_empty_reply(reply, steps)
    assert "coba ulang" in out.lower()
    assert "preset" not in out.lower()
    assert "upgrade lagi" not in out.lower()


def test_builder_update_agent_success_overrides_technical_reply():
    steps = [
        {
            "tool": "update_agent",
            "result": '{"success": true, "agent_name": "CeritaCV", "updated_fields": ["name", "tools_config"]}',
        }
    ]
    out = ensure_non_empty_reply("CeritaCV berhasil diupdate. Field yang diubah: name, tools_config.", steps)
    assert out == "CeritaCV sudah saya edit."


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


def test_builder_partial_update_flow_does_not_claim_agent_created():
    steps = [
        {"tool": "list_my_agents", "result": "ok"},
        {"tool": "get_agent_detail", "result": "ok"},
        {"tool": "compose_agent_blueprint", "result": "ok"},
        {"tool": "compose_agent_soul", "result": "ok"},
    ]
    out = ensure_non_empty_reply("", steps)
    assert "belum berhasil diupdate" in out.lower()
    assert "belum berhasil dibuat" not in out.lower()
    assert "get_agent_detail" not in out


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


def test_disabled_whatsapp_media_claim_is_rewritten():
    out = ensure_non_empty_reply(
        "File PDF sudah saya kirim ke WhatsApp.",
        [],
        tools_config={"memory": True, "whatsapp_media": False},
        active_groups=["memory"],
    )

    assert "belum bisa mengirim file/gambar lewat WhatsApp" in out
    assert "Owner perlu mengaktifkan WhatsApp Media" in out


def test_enabled_whatsapp_media_claim_is_kept():
    reply = "File PDF sudah saya kirim ke WhatsApp."

    out = ensure_non_empty_reply(
        reply,
        [],
        tools_config={"memory": True, "whatsapp_media": True},
        active_groups=["memory", "whatsapp_media"],
    )

    assert out == reply


def test_disabled_sandbox_claim_is_rewritten():
    out = ensure_non_empty_reply(
        "Saya sudah menjalankan kode Python dan hasil eksekusinya aman.",
        [],
        tools_config={"memory": True, "sandbox": False},
        active_groups=["memory"],
    )

    assert "belum bisa menjalankan kode" in out
    assert "Owner perlu mengaktifkan kemampuan file/sandbox" in out


def test_disabled_google_workspace_claim_is_rewritten():
    out = ensure_non_empty_reply(
        "Google Docs sudah saya buat dan linknya siap.",
        [],
        tools_config={"memory": True},
        active_groups=["memory"],
    )

    assert "belum bisa mengakses Google Workspace" in out
    assert "Owner perlu mengaktifkan dan menghubungkan Google" in out

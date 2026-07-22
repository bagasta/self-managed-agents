import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_builder_google_auth_agent_id_detects_create_needs_auth():
    from app.core.engine.agent_runner import _builder_google_auth_agent_id

    agent_id = "11111111-1111-4111-8111-111111111111"
    steps = [
        {
            "tool": "create_agent",
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


def test_builder_google_auth_link_is_appended_after_create_even_if_model_skips_tool():
    from app.core.engine.agent_google_routing import _append_builder_google_auth_link_if_needed

    agent_id = "11111111-1111-4111-8111-111111111111"
    steps = [
        {
            "tool": "create_agent",
            "result": (
                '{"success": true, "agent_id": "%s", "google_workspace_enabled": true, '
                '"needs_google_auth": true}'
            ) % agent_id,
        }
    ]
    session = SimpleNamespace(
        external_user_id="+628111111111",
        channel_type="whatsapp",
        channel_config={"phone_number": "+628111111111"},
    )
    settings = SimpleNamespace(
        google_integration_service_url="https://integration.example.test",
        api_key="test-key",
    )
    log = MagicMock()

    with patch(
        "app.core.engine.agent_google_routing._fetch_google_auth_link",
        new=AsyncMock(return_value="https://accounts.example.test/oauth"),
    ) as fetch_auth:
        out = asyncio.run(_append_builder_google_auth_link_if_needed(
            "Veselka Care sudah jadi.",
            steps=steps,
            session=session,
            settings_obj=settings,
            log=log,
        ))

    fetch_auth.assert_awaited_once()
    assert "https://accounts.example.test/oauth" in out
    assert "Buka link ini dulu" in out


def test_needs_builder_create_completion_when_planned_but_not_created():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    steps = [
        {"tool": "plan_agent", "result": '{"plan_status": "ready", "creation_entitlement_check": {"checked": true, "allowed": true}}'},
        {"tool": "compose_agent_instructions", "result": "ok"},
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is True


def test_needs_builder_create_completion_false_when_discovery_needs_clarification():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    steps = [
        {
            "tool": "plan_agent",
            "result": '{"plan_status": "needs_clarification", "next_group": {"id": "agent_behavior"}}',
        }
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_needs_builder_create_completion_false_for_unstructured_plan_output():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    steps = [{"tool": "plan_agent", "result": "ok"}]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_needs_builder_create_completion_false_when_created():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    steps = [
        {"tool": "plan_agent", "result": "ok"},
        {"tool": "create_agent", "result": '{"success": true}'},
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_needs_builder_create_completion_false_for_non_builder():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    steps = [{"tool": "plan_agent", "result": "ok"}]
    assert _needs_builder_create_completion(steps, is_builder=False) is False


def test_needs_builder_create_completion_false_on_entitlement_block():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    steps = [
        {"tool": "plan_agent", "result": '{"creation_entitlement_check": {"allowed": false, "reason": "Konfigurasi agent melebihi entitlement plan."}}'},
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_needs_builder_create_completion_false_without_plan_agent():
    from app.core.engine.agent_runner import _needs_builder_create_completion

    # Update flow (no plan_agent) is out of scope for create-completion.
    steps = [
        {"tool": "list_my_agents", "result": "ok"},
        {"tool": "get_agent_detail", "result": "ok"},
        {"tool": "compose_agent_blueprint", "result": "ok"},
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_keep_existing_reply():
    assert ensure_non_empty_reply("Halo jadi", []) == "Halo jadi"


def test_builder_clarification_fallback_returns_questions_not_system_error():
    steps = [
        {
            "tool": "plan_agent",
            "result": json.dumps(
                {
                    "plan_status": "needs_clarification",
                    "capability_clarifications": [
                        {"topic": "problem", "question": "Masalah utama apa yang ingin diselesaikan?"},
                        {"topic": "audience", "question": "Siapa yang akan memakai agent ini?"},
                    ],
                }
            ),
        }
    ]

    out = ensure_non_empty_reply("", steps)

    assert "Masalah utama" in out
    assert "Siapa yang akan memakai" in out
    assert "kendala sistem" not in out.lower()
    assert "coba kirim lagi" not in out.lower()


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


def test_builder_create_whatsapp_agent_success_uses_demo_first_onboarding():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": true, "name": "CVin aja", "agent_id": "agent-123", "channel_type": "whatsapp"}',
        }
    ]
    out = ensure_non_empty_reply("", steps)
    assert "CVin aja sudah jadi" in out
    assert "nomor WhatsApp kamu sendiri" not in out
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
        "CVin aja sudah jadi. Kita coba dulu lewat nomor demo Arthur supaya kamu bisa cek kualitas jawaban "
        "dan alurnya tanpa setup nomor sendiri, ya?"
    )


def test_builder_create_whatsapp_agent_overrides_dedicated_number_choice():
    steps = [
        {
            "tool": "create_agent",
            "result": '{"success": true, "name": "Veselka Care", "agent_id": "agent-123", "channel_type": "whatsapp"}',
        }
    ]
    reply = (
        "Veselka Care sudah jadi. Sekarang mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, "
        "atau dicoba dulu lewat nomor demo Arthur?"
    )

    out = ensure_non_empty_reply(reply, steps)

    assert "nomor WhatsApp kamu sendiri" not in out
    assert "Kita coba dulu lewat nomor demo Arthur" in out


def test_builder_trial_link_ambiguous_target_asks_agent_name():
    steps = [
        {
            "tool": "create_wa_dev_trial_link",
            "result": (
                '{"success": false, "error": "agent_target_required", '
                '"available_agents": [{"agent_name": "Mas Brew"}, {"agent_name": "Rnd"}]}'
            ),
        }
    ]

    out = ensure_non_empty_reply("", steps)

    assert "Mau nomor demo agent yang mana?" in out
    assert "Mas Brew" in out
    assert "Rnd" in out


def test_builder_trial_link_current_request_ambiguity_asks_agent_name():
    steps = [
        {
            "tool": "create_wa_dev_trial_link",
            "result": (
                '{"success": false, "error": "agent_target_ambiguous_for_current_request", '
                '"available_agents": [{"agent_name": "Baas"}, {"agent_name": "Mas Brew"}], '
                '"latest_agent": {"agent_name": "Baas"}, '
                '"provided_agent": {"agent_name": "Mas Brew"}}'
            ),
        }
    ]

    out = ensure_non_empty_reply("", steps)

    assert "Mau nomor demo agent yang mana?" in out
    assert "Baas" in out
    assert "Mas Brew" in out


def test_builder_trial_link_target_conflict_does_not_claim_sent():
    steps = [
        {
            "tool": "create_wa_dev_trial_link",
            "result": (
                '{"success": false, "error": "agent_target_conflict", '
                '"provided_agent": {"agent_name": "Rnd"}, '
                '"detected_agent": {"agent_name": "Mas Brew"}}'
            ),
        }
    ]

    out = ensure_non_empty_reply("", steps)

    assert "tidak salah kirim" in out
    assert "Mas Brew" in out
    assert "Rnd" not in out


def test_builder_trial_link_reply_without_code_or_link_is_replaced():
    steps = [
        {
            "tool": "create_wa_dev_trial_link",
            "result": (
                '{"success": true, "agent_name": "Baas", "code": "8EX446", '
                '"wa_me_url": "https://wa.me/6282221000062?text=Kode%208EX446", '
                '"shared_whatsapp_name": "Demo Baas", "contact_sent": true}'
            ),
        }
    ]

    out = ensure_non_empty_reply("Kode demo untuk Baas sudah saya kirim ke WhatsApp kamu.", steps)

    assert "kontak Demo Baas juga sudah saya kirim" in out
    assert "8EX446" in out
    assert "https://wa.me/6282221000062" in out
    assert out.index("https://wa.me/6282221000062") < out.index("kontak Demo Baas")


def test_builder_trial_link_success_wins_over_later_blocked_stale_target():
    steps = [
        {
            "tool": "create_wa_dev_trial_link",
            "result": (
                '{"success": true, "agent_name": "Baas", "code": "8EX446", '
                '"wa_me_url": "https://wa.me/6282221000062?text=Kode%208EX446", '
                '"shared_whatsapp_name": "Demo Baas", "contact_sent": true}'
            ),
        },
        {
            "tool": "create_wa_dev_trial_link",
            "result": (
                '{"success": false, "error": "agent_target_ambiguous_for_current_request", '
                '"available_agents": [{"agent_name": "Baas"}, {"agent_name": "Mas Brew"}], '
                '"latest_agent": {"agent_name": "Baas"}, '
                '"provided_agent": {"agent_name": "Mas Brew"}}'
            ),
        },
    ]

    out = ensure_non_empty_reply("", steps)

    assert "kontak Demo Baas juga sudah saya kirim" in out
    assert "8EX446" in out
    assert "Mas Brew" not in out


def test_builder_reply_sanitizes_webchat_channel_offer():
    reply = (
        "Oke Bagas, untuk agent riset yang kamu mau, saya butuh tahu:\n"
        "* Nama agent-nya mau apa?\n"
        "* Fokus risetnya tentang apa atau bidang apa?\n"
        "* Mau channel apa? WhatsApp atau webchat?\n"
        "* Perlu fitur khusus seperti browsing internet, buat ringkasan, atau lainnya?"
    )

    out = ensure_non_empty_reply(reply, [], active_groups=["builder"])

    assert "webchat" not in out.lower()
    assert "mau channel apa" not in out.lower()
    assert "Nama agent-nya mau apa?" in out
    assert "Fokus risetnya" in out
    assert "Channelnya saya set ke WhatsApp" in out
    assert "nomor demo Arthur" in out
    assert "nomor WhatsApp kamu sendiri" not in out


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


def test_builder_create_file_blocker_is_rendered_as_discovery_question():
    steps = [
        {
            "tool": "create_agent",
            "result": json.dumps(
                {"error": "Kemampuan file belum diputuskan — jangan menebak."},
                ensure_ascii=False,
            ),
        }
    ]

    out = ensure_non_empty_reply(
        "Masih saya proses ya. Saya akan kirim hasilnya begitu selesai.",
        steps,
    )

    assert "hanya chat teks" in out
    assert "menerima file/gambar" in out
    assert "membuat file/laporan" in out
    assert "belum berhasil" not in out.lower()
    assert "proses" not in out.lower()


def test_builder_create_discovery_blocker_returns_capability_questions():
    steps = [
        {
            "tool": "create_agent",
            "result": json.dumps(
                {
                    "error": "Discovery kebutuhan agent belum lengkap atau belum dikonfirmasi user.",
                    "discovery_progress": {
                        "next_questions": [
                            {
                                "topic": "main_tasks",
                                "question": "Apa saja tugas utama agent dari awal sampai selesai?",
                            },
                            {
                                "topic": "capabilities",
                                "question": "Kemampuan apa yang dibutuhkan, termasuk keputusan file?",
                            },
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        }
    ]

    out = ensure_non_empty_reply("Belum berhasil dibuat.", steps)

    assert "tugas utama agent" in out
    assert "Kemampuan apa yang dibutuhkan" in out
    assert "belum berhasil" not in out.lower()


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


def test_builder_partial_flow_shows_system_hiccup_not_failure():
    # Incomplete build (no create_agent) must NOT show a confusing "gagal/belum
    # berhasil ... kirim lanjut" loop. Frame it as a transient system hiccup.
    steps = [
        {"tool": "plan_agent", "result": "ok"},
        {"tool": "compose_agent_blueprint", "result": "ok"},
        {"tool": "compose_agent_instructions", "result": "ok"},
    ]
    out = ensure_non_empty_reply("", steps)
    lower = out.lower()
    assert "kendala sistem" in lower
    assert "belum berhasil dibuat" not in lower
    assert "kirim lanjut" not in lower
    assert "plan_agent" not in out
    assert "compose_agent" not in out


def test_builder_partial_update_flow_shows_system_hiccup_not_failure():
    steps = [
        {"tool": "list_my_agents", "result": "ok"},
        {"tool": "get_agent_detail", "result": "ok"},
        {"tool": "compose_agent_blueprint", "result": "ok"},
        {"tool": "compose_agent_soul", "result": "ok"},
    ]
    out = ensure_non_empty_reply("", steps)
    lower = out.lower()
    assert "kendala sistem" in lower
    assert "belum berhasil" not in lower
    assert "kirim lanjut" not in lower
    assert "get_agent_detail" not in out


def test_builder_partial_soul_reply_is_overridden():
    steps = [
        {"tool": "plan_agent", "result": "ok"},
        {"tool": "compose_agent_instructions", "result": "ok"},
        {"tool": "compose_agent_soul", "result": "ok"},
    ]
    out = ensure_non_empty_reply("Soul sudah siap, mau saya lanjut buat agent?", steps)
    lower = out.lower()
    assert "kendala sistem" in lower
    assert "belum berhasil dibuat" not in lower
    assert "soul" not in lower


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

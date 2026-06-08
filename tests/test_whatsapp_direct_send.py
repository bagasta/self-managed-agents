import base64
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from app.core.engine.agent_runner import (
    _direct_whatsapp_send_guard_reply,
    _extract_direct_whatsapp_confirmation_payload,
    _filter_whatsapp_unsafe_mcp_tools,
    _is_direct_whatsapp_meta_request,
    _is_direct_whatsapp_text_send_context,
    _is_google_chat_intent,
    _is_operator_envelope,
    _operator_escalation_reply_guard,
    _prioritize_direct_whatsapp_text_send_tools,
)
from app.core.engine.prompt_builder import build_system_prompt
from app.api.channels import (
    _is_wa_dev_device,
    _is_wa_dev_disconnect_command,
    _is_wa_owner_sender,
    _label_owner_wa_message,
    _wa_dev_session_lookup_candidates,
)


def _agent(**overrides):
    values = {
        "name": "Yo Besty",
        "model": "gpt-4.1-mini",
        "instructions": "You are helpful.",
        "safety_policy": None,
        "operator_ids": [],
        "escalation_config": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session(**overrides):
    values = {
        "id": "session-1",
        "agent_id": "agent-1",
        "channel_type": "whatsapp",
        "channel_config": {"user_phone": "628111111111"},
        "external_user_id": "628111111111",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _msg(role: str, content: str):
    return SimpleNamespace(role=role, content=content)


def _tool(name: str):
    return SimpleNamespace(name=name)


def test_wa_owner_sender_detects_resolved_phone_and_jid():
    agent = _agent(owner_external_id="628owner")

    assert _is_wa_owner_sender(agent, "+628owner", "628owner@s.whatsapp.net") is True
    assert _is_wa_owner_sender(agent, "+628customer", "628customer@s.whatsapp.net") is False


def test_wa_dev_disconnect_command_is_backend_guarded():
    assert _is_wa_dev_device("wadev_123")
    assert not _is_wa_dev_device("real-device")
    assert _is_wa_dev_disconnect_command("/stop")
    assert _is_wa_dev_disconnect_command("/disconnect")
    assert _is_wa_dev_disconnect_command("berhenti")
    assert not _is_wa_dev_disconnect_command("hi")


def test_wa_dev_disconnect_lookup_candidates_cover_phone_lid_and_group():
    assert _wa_dev_session_lookup_candidates(
        "+628123456789",
        "103160936972328@lid",
        "120363000000000000@g.us",
    ) == [
        "628123456789",
        "103160936972328",
        "120363000000000000",
        "120363000000000000@g.us",
    ]


def test_wa_owner_message_gets_explicit_owner_label():
    labeled = _label_owner_wa_message(
        message="tolong cek setting agent saya",
        from_phone="628owner",
        sender_name="Bagas",
        is_operator_turn=False,
    )

    assert labeled.startswith("<OWNER>\n")
    assert "Role: OWNER/SUPERADMIN" in labeled
    assert "Name WA: Bagas" in labeled
    assert "Pesan: tolong cek setting agent saya" in labeled


def test_wa_owner_operator_turn_keeps_operator_envelope_with_owner_role():
    labeled = _label_owner_wa_message(
        message="approve kirim ke customer",
        from_phone="628owner",
        sender_name="Bagas",
        is_operator_turn=True,
    )

    assert labeled.startswith("<OPERATOR>\n")
    assert "Role: OWNER/SUPERADMIN" in labeled
    assert "Pesan: approve kirim ke customer" in labeled


def test_whatsapp_prompt_allows_user_direct_send_without_unlocking_reply_to_user():
    prompt = build_system_prompt(
        agent_model=_agent(),
        session=_session(),
        active_groups=["escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert "Tool `send_to_number` BOLEH dipakai" in prompt
    assert "langsung panggil `send_to_number` memakai nomor dan draft terakhir dari history" in prompt
    assert "Tool `reply_to_user` HANYA dipakai untuk sesi operator/eskalasi" in prompt
    assert "Tool `reply_to_user` dan `send_to_number` HANYA dipakai saat menerima perintah dari OPERATOR" not in prompt


def test_whatsapp_prompt_labels_owner_as_operator():
    prompt = build_system_prompt(
        agent_model=_agent(owner_external_id="628owner", operator_ids=["628owner"]),
        session=_session(
            channel_config={"user_phone": "628owner@s.whatsapp.net"},
            external_user_id="628owner",
        ),
        active_groups=["memory", "escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="tolong bikinin website untuk bisnis saya",
    )

    assert "- Current User Role: OPERATOR" in prompt
    assert "Current User Name: Operator/Admin" in prompt
    assert "Kamu sedang di-chat oleh OPERATOR" in prompt
    assert "- Agent Owner/Superadmin: 628owner" in prompt
    assert "Owner adalah bos/superadmin agent ini" in prompt
    assert "izin Google" in prompt


def test_runtime_injects_owner_superadmin_even_without_soul():
    prompt = build_system_prompt(
        agent_model=_agent(owner_external_id="628owner", operator_ids=["628owner"]),
        session=_session(
            channel_config={"user_phone": "628customer@s.whatsapp.net"},
            external_user_id="628customer",
        ),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Customer",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="halo",
    )

    assert "- Agent Owner/Superadmin: 628owner" in prompt
    assert "Owner adalah bos/superadmin agent ini" in prompt
    assert "## Identitasmu" not in prompt


def test_runtime_injects_created_by_arthur_from_metadata():
    arthur_id = "00000000-0000-0000-0000-000000000001"
    prompt = build_system_prompt(
        agent_model=_agent(
            owner_external_id="628owner",
            operator_ids=["628owner"],
            created_by_type="arthur_builder",
            created_by_agent_id=arthur_id,
            created_by_agent_name="Arthur",
        ),
        session=_session(
            channel_config={"user_phone": "628customer@s.whatsapp.net"},
            external_user_id="628customer",
        ),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Customer",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="halo",
    )

    assert "Created By: Arthur (Agent Builder platform ini)" in prompt
    assert f"Created By Agent ID: {arthur_id}" in prompt
    assert "Kamu dibuat/dikonfigurasi lewat Arthur" in prompt
    assert "arahkan Owner bicara ke Arthur" in prompt


def test_customer_session_does_not_become_owner():
    prompt = build_system_prompt(
        agent_model=_agent(owner_external_id="628owner", operator_ids=["628owner"]),
        session=_session(
            channel_config={"user_phone": "628customer@s.whatsapp.net"},
            external_user_id="628customer",
        ),
        active_groups=["memory", "escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Customer",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="halo",
    )

    assert "- Current User Role: user" in prompt
    assert "- Current User Role: OPERATOR" not in prompt
    assert "Kamu sedang di-chat oleh OPERATOR" not in prompt


def test_customer_prompt_does_not_expose_operator_phone():
    prompt = build_system_prompt(
        agent_model=_agent(
            owner_external_id="628owner",
            operator_ids=["628owner", "628admin"],
            escalation_config={"operator_phone": "628admin", "operator_name": "Admin Laundry"},
        ),
        session=_session(
            channel_config={"user_phone": "628customer@s.whatsapp.net"},
            external_user_id="628customer",
        ),
        active_groups=["memory", "escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Customer",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="halo",
    )

    assert "628admin" not in prompt
    assert "never reveal the operator/admin phone" in prompt
    assert "Jangan memberi nomor admin" in prompt
    assert "link auth" in prompt


def test_owner_session_gets_superadmin_role():
    prompt = build_system_prompt(
        agent_model=_agent(owner_external_id="628owner", operator_ids=["628owner"]),
        session=_session(
            channel_config={"user_phone": "628owner@s.whatsapp.net"},
            external_user_id="628owner",
        ),
        active_groups=["memory", "escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="tolong cek agent saya",
    )

    assert "- Current User Role: OPERATOR" in prompt
    assert "- Agent Owner/Superadmin: 628owner" in prompt
    assert "perlakukan arahannya sebagai arahan bos/superadmin" in prompt


def test_whatsapp_prompt_does_not_expose_lid_as_phone():
    prompt = build_system_prompt(
        agent_model=_agent(),
        session=_session(
            channel_config={"user_phone": "151414827434073@lid"},
            external_user_id="151414827434073",
        ),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="halo",
    )

    assert "Current User WhatsApp ID: 151414827434073@lid" in prompt
    assert "Current User Phone: unknown" in prompt
    assert "Current User Phone: 151414827434073" not in prompt


def test_prompt_has_operator_approval_resume_mode():
    prompt = build_system_prompt(
        agent_model=_agent(),
        session=_session(),
        active_groups=["memory", "whatsapp_media"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Wira",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="[SYSTEM_OPERATOR_APPROVAL]\nJenis approval: pembayaran customer sudah dikonfirmasi",
    )

    assert "## Operator Approval Resume Mode" in prompt
    assert "Jangan eskalasi pembayaran lagi" in prompt
    assert "kirim file/gambar langsung" in prompt


def test_builder_prompt_blocks_repeated_continue_questions():
    prompt = build_system_prompt(
        agent_model=_agent(capabilities=["builder"], tools_config={"builder": True}),
        session=_session(),
        active_groups=["builder"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="buatkan agentnya langsung aja gausah banyak tanya",
    )

    assert "## Arthur Builder Mode" in prompt
    assert "plan_agent -> compose_agent_blueprint -> compose_agent_operating_manual -> compose_agent_instructions -> validate_agent_config -> compose_agent_soul -> create_agent -> verify_agent" in prompt
    assert "## Arthur Tool Categories" in prompt
    assert "Agent Builder" in prompt
    assert "Agent Management" in prompt
    assert "Channel Management" in prompt
    assert "Workspace/App Connectors" in prompt
    assert "sampai user tahu langkah berikutnya" in prompt
    assert "cara test, cara connect Google, cara pasang WhatsApp" in prompt
    assert "minta pembayaran, bukti apa yang diminta" in prompt
    assert "Jangan berhenti hanya untuk menampilkan rencana" in prompt
    assert "Jangan mengunci preset hanya dari satu kata kunci" in prompt
    assert "jangan menyebut label preset internal" in prompt
    assert "google_workspace_option.should_offer=true" in prompt
    assert "Mau sekalian dihubungkan ke Google" in prompt
    assert "user membalas nama seperti `Travgent`" in prompt
    assert "Jangan mengulang plan_agent/compose_agent_instructions" in prompt
    assert "jangan menyebut nama tool internal" in prompt
    assert "Mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri" in prompt
    assert "nomor demo Arthur" in prompt
    assert "jangan berhenti hanya dengan `agent sudah jadi` atau ID agent" in prompt
    assert "terus gimana pakenya?" in prompt
    assert "bukan `QR`" in prompt
    assert "jangan fallback ke agent terbaru" in prompt
    assert "agent_name atau agent_id" in prompt
    assert "langsung cari agent terkait lalu panggil create_wa_dev_trial_link" in prompt
    assert "jangan menjawab `langsung aku betulin`" in prompt
    assert "DILARANG memakai task, subagent, sandbox, read_file, edit_file, atau write_file" in prompt
    assert "get_agent_detail(include_instructions=true)" in prompt
    assert "refresh_memory_mode" in prompt
    assert "sistem menyimpan versi lama sebagai arsip" in prompt
    assert "Jangan menyebut `subagent`, `placeholder`, `database`, `sistem file`" in prompt
    assert "setup_status_for_owner" in prompt
    assert "summary_for_owner" in prompt
    assert "Jangan menyebut blockers/warnings/raw JSON ke user" in prompt
    assert "whatsapp_media=true, sandbox=true" in prompt
    assert "link Google Form yang sudah ada sebagai link order pelanggan" in prompt
    assert "DILARANG menawarkan webchat" in prompt
    assert "WhatsApp/webchat/API" not in prompt
    assert "Jangan minta user mengisi placeholder" in prompt
    assert "jangan menawarkan versi sederhana atau minta user pilih downgrade" in prompt
    assert "enable_google_workspace=True" in prompt
    assert "generate_google_auth_link" in prompt
    assert "Jangan tunggu user bertanya `terus koneknya gimana?`" in prompt
    assert "JANGAN menyebut istilah teknis internal" in prompt


def test_arthur_builder_mode_knows_crud_is_primary_job():
    prompt = build_system_prompt(
        agent_model=_agent(capabilities=["builder"], tools_config={"builder": True}),
        session=_session(),
        active_groups=["builder"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="hapus agent lama saya",
    )

    assert "## Arthur Builder Mode" in prompt
    assert "membuat, mengubah, mengecek, dan menyiapkan agent user" in prompt
    assert "Arthur Builder: aktif" in prompt
    assert "membuat, membaca, mengubah, dan menghapus agent platform milik Owner" in prompt


def test_whatsapp_prompt_explains_reply_context_block():
    prompt = build_system_prompt(
        agent_model=_agent(),
        session=_session(),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="[WHATSAPP_REPLY_CONTEXT]\nUser sedang membalas/reply pesan WhatsApp berikut:\nold\n[/WHATSAPP_REPLY_CONTEXT]\n\nini gimana?",
    )

    assert "### WhatsApp Reply Context" in prompt
    assert "instruksi utama tetap pesan terbaru user" in prompt


def test_prompt_file_rules_prevent_write_file_retry_loop_for_research():
    prompt = build_system_prompt(
        agent_model=_agent(instructions="Kamu adalah research agent."),
        session=_session(channel_type="webchat"),
        active_groups=["memory", "http", "tavily"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="riset pasar pupuk hayati dan simpan hasilnya",
    )

    assert "## File Workspace Tool Rules" in prompt
    assert "`write_file` hanya untuk membuat file baru" in prompt
    assert "JANGAN panggil `write_file` lagi dengan path yang sama" in prompt
    assert "default-nya balas user di chat dan simpan inti informasi ke memory" in prompt
    assert "/workspace/shared/<filename>" in prompt
    assert "/workspace/data/incoming/<filename>" in prompt
    assert "JANGAN pakai dataset contoh/built-in" in prompt
    assert "Jangan membuat ulang file final_v2/final_v3/final_v4" in prompt


def test_layered_memory_tells_agent_to_use_memory_not_files():
    prompt = build_system_prompt(
        agent_model=_agent(instructions="Kamu adalah research agent."),
        session=_session(channel_type="webchat"),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory={"soul": "Agen riset", "today_date": "2026-05-26"},
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="simpan hasil riset ini",
    )

    assert "Kalau penting → simpan ke memory" in prompt
    assert "JANGAN memakai `write_file` hanya untuk menyimpan ingatan" in prompt
    assert "Kalau penting → tulis ke file" not in prompt


def test_prompt_requires_brief_and_memory_provenance_for_underspecified_landing_page():
    prompt = build_system_prompt(
        agent_model=_agent(instructions="Kamu adalah assistant yang bisa bikin website."),
        session=_session(channel_type="whatsapp"),
        active_groups=["memory", "subagents(1)"],
        saved_custom_tools=[],
        subagent_list=[{"name": "sys_coder", "description": "Bikin web dan deploy."}],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory={"soul": "Yo Besty", "today_date": "2026-06-08"},
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="bisa bantu bikin landing page gak buat lomba bikin game dengan AI?",
    )

    assert "tanya brief minimal dulu sebelum membuat atau deploy" in prompt
    assert "JANGAN delegate/deploy berdasarkan asumsi" in prompt
    assert "pernah saya tangani" in prompt
    assert "Jika recall kosong" in prompt


@pytest.mark.asyncio
async def test_process_wa_media_saves_document_to_shared_workspace(tmp_path, monkeypatch):
    from app.api.wa_helpers import process_wa_media

    settings = SimpleNamespace(
        sandbox_base_dir=str(tmp_path),
        media_doc_max_chars=1000,
        mistral_api_key="",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.infra.sandbox.get_settings", lambda: settings)

    raw = b"fake spreadsheet bytes"
    media_context, image_b64, image_mime, media_meta = await process_wa_media(
        media_type="document",
        media_data=base64.b64encode(raw).decode("ascii"),
        media_filename="../Titanic.xlsx",
        session_id=uuid.uuid4(),
        logger=SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None),
    )

    assert image_b64 is None
    assert image_mime is None
    assert media_meta is not None
    assert media_meta["filename"] == "Titanic.xlsx"
    assert media_context
    assert "/workspace/shared/Titanic.xlsx" in media_context
    assert "workflow file/sandbox/subagent" in media_context

    root_path = media_meta["workspace_path"]
    shared_path = media_meta["shared_workspace_path"]
    assert root_path.endswith("/Titanic.xlsx")
    assert shared_path.endswith("/shared/Titanic.xlsx")
    assert open(root_path, "rb").read() == raw
    assert open(shared_path, "rb").read() == raw


def test_runtime_tool_contract_lists_only_actual_tools_and_disabled_risks():
    prompt = build_system_prompt(
        agent_model=_agent(
            instructions=(
                "Kamu bisa pakai Google Drive, kirim file WhatsApp, menjalankan kode, "
                "dan deploy app kapan saja."
            ),
            tools_config={"memory": True, "tavily": True},
        ),
        session=_session(channel_type="webchat"),
        active_groups=["memory", "tavily"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="bisa apa aja?",
    )

    assert "## Runtime Tool Contract" in prompt
    assert "Sumber kebenaran tools adalah runtime platform" in prompt
    assert "Memory: aktif" in prompt
    assert "Web Search: aktif" in prompt
    assert "Google Workspace: tidak aktif/tersedia pada run ini" in prompt
    assert "WhatsApp Media: tidak aktif/tersedia pada run ini" in prompt
    assert "Sandbox: tidak aktif/tersedia pada run ini" in prompt
    assert "Deploy: tidak aktif/tersedia pada run ini" in prompt
    assert "jangan klaim bisa memakainya" in prompt


def test_runtime_tool_contract_detects_google_workspace_from_tools_config():
    prompt = build_system_prompt(
        agent_model=_agent(
            tools_config={
                "memory": True,
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "google_workspace": {
                            "transport": "streamable_http",
                            "url": "http://localhost:8002/mcp",
                        }
                    },
                },
            },
        ),
        session=_session(channel_type="webchat"),
        active_groups=["memory", "mcp"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="buatkan google docs",
    )

    assert "Integrasi Eksternal: aktif" in prompt
    assert "Google Workspace: tidak aktif/tersedia pada run ini" not in prompt
    assert "Sandbox: tidak aktif/tersedia pada run ini" in prompt


def test_disabled_whatsapp_media_prevents_file_delivery_claim():
    prompt = build_system_prompt(
        agent_model=_agent(
            instructions="Kamu bisa kirim file PDF final lewat WhatsApp kapan saja.",
            tools_config={"memory": True, "whatsapp_media": False},
        ),
        session=_session(channel_type="whatsapp"),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="kirim file pdf",
    )

    assert "WhatsApp Media: tidak aktif/tersedia pada run ini" in prompt
    assert "jangan klaim bisa memakainya" in prompt


def test_disabled_sandbox_prevents_code_execution_claim():
    prompt = build_system_prompt(
        agent_model=_agent(
            instructions="Kamu bisa menjalankan kode Python untuk membaca Excel.",
            tools_config={"memory": True, "sandbox": False},
        ),
        session=_session(channel_type="webchat"),
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="baca file excel",
    )

    assert "Sandbox: tidak aktif/tersedia pada run ini" in prompt
    assert "jangan klaim bisa memakainya" in prompt


def test_direct_text_send_context_detects_followup_confirmation_from_history():
    assert _is_direct_whatsapp_text_send_context(
        "yes kirim",
        [
            _msg("user", "tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?"),
            _msg("agent", "Draft untuk Julia: Halo Julia, apakah besok jadi meeting? Ketik kirim untuk mengirim."),
        ],
    )


def test_direct_text_send_context_uses_operator_payload_not_operator_header_phone():
    assert _is_operator_envelope("<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: yes kirim")
    assert not _is_direct_whatsapp_text_send_context(
        "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: halo",
        [],
    )
    assert _is_direct_whatsapp_text_send_context(
        "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: yes kirim",
        [
            _msg("user", "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: tolong kirim pesan ke 6289516247011 tanya besok meeting kah?"),
            _msg("agent", "Draft untuk Julia: Halo Julia, apakah besok jadi meeting? Ketik kirim untuk mengirim."),
        ],
    )


def test_direct_text_send_context_ignores_arthur_repair_request_with_example_phone():
    message = (
        "ada bug serius, gua minta arthur perbaiki agent gua yang gabisa disuruh kirim pesan wa. "
        "Contoh user: tolong kirim pesan wa ke 6289516247011."
    )

    assert _is_direct_whatsapp_meta_request(message)
    assert not _is_direct_whatsapp_text_send_context(message, [])


def test_direct_send_guard_does_not_override_arthur_repair_reply():
    reply = "Saya sudah perbaiki konfigurasi Bas supaya bisa kirim WA ke nomor lain."
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="Arthur, perbaiki agent Bas yang gabisa disuruh kirim pesan WA ke nomor lain.",
    )

    assert guarded == reply


def test_extract_direct_confirmation_payload_from_quoted_draft_history():
    payload = _extract_direct_whatsapp_confirmation_payload(
        "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: yes kirim",
        [
            _msg("user", "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: tolong kirim pesan ke 6289516247011"),
            _msg("agent", 'Draft untuk Julia: "Halo Julia, apakah besok jadi meeting? Mohon konfirmasinya." Ketik kirim.'),
        ],
    )

    assert payload == ("6289516247011", "Halo Julia, apakah besok jadi meeting? Mohon konfirmasinya.")


def test_extract_direct_confirmation_payload_from_unquoted_draft_history():
    payload = _extract_direct_whatsapp_confirmation_payload(
        "kirim",
        [
            _msg("user", "tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?"),
            _msg("agent", "Draft untuk Julia: Halo Julia, apakah besok jadi meeting seperti yang direncanakan? Mohon konfirmasinya. Ketik kirim untuk mengirim."),
        ],
    )

    assert payload == (
        "6289516247011",
        "Halo Julia, apakah besok jadi meeting seperti yang direncanakan? Mohon konfirmasinya.",
    )


def test_extract_direct_confirmation_payload_ignores_non_confirmation():
    assert _extract_direct_whatsapp_confirmation_payload(
        "halo",
        [
            _msg("user", "tolong kirim pesan ke 6289516247011"),
            _msg("agent", 'Draft: "Halo Julia"'),
        ],
    ) is None


def test_direct_text_send_context_does_not_capture_media_requests():
    assert not _is_direct_whatsapp_text_send_context(
        "kirim",
        [
            _msg("user", "tolong kirim gambar ke 6289516247011"),
            _msg("agent", "Siap, ketik kirim."),
        ],
    )


def test_direct_text_send_tool_filter_removes_ambiguous_tools_and_prioritizes_send_to_number():
    tools = [
        _tool("send_message"),
        _tool("send_whatsapp_image"),
        _tool("notify_user"),
        _tool("recall"),
        _tool("send_to_number"),
        _tool("reply_to_user"),
    ]
    filtered = _prioritize_direct_whatsapp_text_send_tools(tools, SimpleNamespace(info=lambda *a, **k: None))
    names = [tool.name for tool in filtered]

    assert names[0] == "send_to_number"
    assert "send_message" not in names
    assert "send_whatsapp_image" not in names
    assert "notify_user" not in names
    assert "reply_to_user" in names


def test_whatsapp_mcp_filter_removes_google_chat_send_message_collision():
    tools = [_tool("send_message"), _tool("create_presentation"), _tool("send_to_number")]
    filtered = _filter_whatsapp_unsafe_mcp_tools(
        tools,
        user_message="<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: kirim pesan ke 6289516247011",
        log=SimpleNamespace(info=lambda *a, **k: None),
    )
    names = [tool.name for tool in filtered]

    assert "send_message" not in names
    assert "create_presentation" in names


def test_whatsapp_mcp_filter_keeps_google_chat_send_message_when_explicit():
    tools = [_tool("send_message"), _tool("create_presentation")]
    message = "<OPERATOR>\nNo Telepon/WA/Id: +151414827434073\nPesan: kirim ke Google Chat spaces/abc"
    filtered = _filter_whatsapp_unsafe_mcp_tools(
        tools,
        user_message=message,
        log=SimpleNamespace(info=lambda *a, **k: None),
    )

    assert _is_google_chat_intent(message)
    assert [tool.name for tool in filtered] == ["send_message", "create_presentation"]


def test_operator_prompt_routes_explicit_other_number_to_send_to_number():
    prompt = build_system_prompt(
        agent_model=_agent(operator_ids=["628111111111"]),
        session=_session(),
        active_groups=["escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=True,
        user_message="tolong kirim pesan ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert "### KIRIM KE NOMOR LAIN" in prompt
    assert "gunakan `send_to_number(phone_or_target, message)`" in prompt
    assert "Jangan gunakan `reply_to_user` untuk nomor lain" in prompt
    assert "langsung panggil `send_to_number` memakai nomor dan draft terakhir dari history" in prompt


def test_escalation_operator_prompt_keeps_reply_to_user_but_allows_other_number_send():
    prompt = build_system_prompt(
        agent_model=_agent(operator_ids=["628111111111"]),
        session=_session(),
        active_groups=["escalation"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid="628222222222@s.whatsapp.net",
        escalation_context="ROUTING: operator_reply_quoted_escalation",
        is_operator_message=True,
        user_message="tolong kirim pesan ke 6289516247011",
    )

    assert "Panggil `reply_to_user(message)`" in prompt
    assert "JANGAN membuat ulang CV/dokumen/website" in prompt
    assert "Jika operator hanya memberi approval pembayaran" in prompt
    assert "### KIRIM KE NOMOR LAIN DARI OPERATOR" in prompt
    assert "gunakan `send_to_number(phone_or_target, message)`, BUKAN `reply_to_user`" in prompt


def test_direct_send_guard_blocks_success_claim_without_tool_execution():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="tolong kirim pesan wa ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert guarded != reply
    assert guarded.startswith("Belum saya kirim.")


def test_direct_send_guard_blocks_followup_success_claim_without_tool_execution():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="yes kirim",
    )

    assert guarded != reply
    assert guarded.startswith("Belum saya kirim.")


def test_direct_send_guard_allows_success_after_send_to_number_tool():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[{"tool": "send_to_number", "result": "[SENT_TO_NUMBER:6289516247011] Halo"}],
        user_message="tolong kirim pesan wa ke 6289516247011 tanya besok jadi meeting kah?",
    )

    assert guarded == reply


def test_direct_send_guard_allows_success_with_prior_send_history():
    reply = 'Pesan WhatsApp ke Julia di nomor 6289516247011 sudah saya kirim.'
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[],
        user_message="apakah tadi sudah terkirim?",
        history_messages=[
            ToolMessage(
                content="[SENT_TO_NUMBER:6289516247011] Halo Julia",
                name="send_to_number",
                tool_call_id="call_1",
            )
        ],
    )

    assert guarded == reply


def test_direct_send_guard_does_not_touch_escalation_reply_to_user():
    reply = "Terkirim ✓"
    guarded = _direct_whatsapp_send_guard_reply(
        reply,
        steps=[{"tool": "reply_to_user", "result": "[SENT_TO_USER] Halo"}],
        user_message="kirim",
    )

    assert guarded == reply


def test_operator_escalation_guard_blocks_fake_cv_completion_claim():
    guarded = _operator_escalation_reply_guard(
        "CV ATS Anda sudah selesai dibuat dan siap dikirim.",
        steps=[],
        user_message="<OPERATOR>\nNo Telepon/WA/Id: 628operator\nPesan: iya pembayaran sudah masuk",
        escalation_user_jid="628customer@s.whatsapp.net",
    )

    assert guarded != "CV ATS Anda sudah selesai dibuat dan siap dikirim."
    assert guarded.startswith("Draft pesan untuk customer:")
    assert "pembayaran Anda sudah kami terima" in guarded

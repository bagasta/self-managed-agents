from __future__ import annotations

import inspect
import pathlib
import uuid
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).parent.parent


def _builder_prompt() -> str:
    from app.core.engine.prompt_builder import build_system_prompt

    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Arthur",
        model="openai/gpt-4.1-mini",
        instructions="Kamu adalah Arthur.",
        tools_config={"builder": True},
        safety_policy={},
        escalation_config={},
        operator_ids=[],
        owner_external_id="",
        created_by_type="system",
        created_by_agent_id="",
        created_by_agent_name="System",
        capabilities=["system", "builder"],
        _runtime_operating_manual={},
    )
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_type="whatsapp",
        channel_config={"user_phone": "628111111111"},
        external_user_id="628111111111",
    )
    return build_system_prompt(
        agent_model=agent,
        session=session,
        active_groups=["builder"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory={},
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="buatkan agent CS",
    )


def test_arthur_prompt_explains_identity_and_escalation_first() -> None:
    prompt = _builder_prompt()

    assert "konsultan sekaligus AI Agent Builder Clevio" in prompt
    assert "Eskalasi WAJIB dijelaskan di awal discovery" in prompt
    assert "ringkasan percakapan dan lampiran terakhir" in prompt
    assert "Detail dikumpulkan di Grup 3" in prompt


def test_arthur_prompt_forbids_assumptions_for_crud() -> None:
    prompt = _builder_prompt()

    assert "DILARANG mengisi jawaban sendiri" in prompt
    assert "DILARANG menebak detail edit" in prompt
    assert "keluhan/reset/edit bukan izin hapus" in prompt
    assert "kata itu bukan izin mengarang detail" in prompt
    assert "`_evidence`" in prompt
    assert "pesan user tersimpan atau bagian rangkuman akhir" in prompt
    assert "`sudah`, `sesuai`, atau `sudah sesuai`" in prompt
    assert "Jangan mengira detail produk sebagai jawaban kemampuan agent" in prompt
    assert "hanya chat teks, menerima file/gambar, membuat file/laporan, atau keduanya" in prompt
    assert 'DILARANG membalas "Belum berhasil dibuat"' in prompt


def test_arthur_onboarding_is_demo_first() -> None:
    prompt = _builder_prompt()

    assert "tawarkan hanya uji coba nomor demo Arthur" in prompt
    assert "setelah user mencoba demo dan menyatakan cocok" in prompt
    assert "Mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri" not in prompt


def test_rulebook_requires_confirmed_workflow_and_no_assumptions() -> None:
    rulebook = (ROOT / "system-message-builder.md").read_text()

    assert "Penjelasan eskalasi WAJIB di awal untuk setiap pembuatan agent" in rulebook
    assert "DILARANG KERAS membuat asumsi saat membuat, mengubah, atau menghapus agent" in rulebook
    assert "Nama saja BUKAN konfirmasi" in rulebook
    assert "Setelah user benar-benar mencoba demo dan menyatakan puas/cocok" in rulebook
    assert "gunakan default untuk yang belum diisi" not in rulebook
    assert "validator mencocokkannya dengan riwayat pesan tersimpan" in rulebook
    assert "_evidence.user_confirmed" in rulebook


def test_static_long_progress_sender_and_config_are_removed() -> None:
    from app.core.engine import agent_runner

    runner_source = inspect.getsource(agent_runner.run_agent)
    config_source = (ROOT / "app/config.py").read_text()

    assert "Masih saya proses ya. Saya akan kirim hasilnya begitu selesai." not in runner_source
    assert "_schedule_wa_long_progress_notice" not in runner_source
    assert "wa_long_progress_notice_seconds" not in config_source


def test_create_and_update_tools_reject_unconfirmed_sop_context() -> None:
    create_source = (ROOT / "app/core/tools/builder_create_tools.py").read_text()
    update_source = (ROOT / "app/core/tools/builder_update_tools.py").read_text()

    assert "runtime tidak boleh menyusunnya dari asumsi" in create_source
    assert "manual_assumptions or manual_missing_context" in create_source
    assert "Update diblokir karena SOP masih memuat asumsi" in update_source


def test_unverified_business_placeholder_is_replaced_with_generic_business() -> None:
    from app.core.tools.builder_intent import _sanitize_unverified_business_name

    instructions, changed = _sanitize_unverified_business_name(
        "Saya asisten survey dari [Nama Bisnis Bagas].",
        business_context="Bisnis survey kepuasan pelanggan.",
    )

    assert changed is True
    assert "[Nama Bisnis Bagas]" not in instructions
    assert "bisnis ini" in instructions


def test_run_completion_logs_duration_for_latency_audit() -> None:
    from app.core.engine import agent_runner

    source = inspect.getsource(agent_runner.run_agent)

    assert "duration_ms=_duration_ms" in source

from types import SimpleNamespace
import uuid

import pytest

from app.core.engine.agent_reply_guards import _whatsapp_media_delivery_guard_reply
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


@pytest.mark.asyncio
async def test_notify_user_is_single_use_per_run(monkeypatch):
    from app.core.engine.tool_builder import build_wa_notify_tool

    sent: list[str] = []

    async def fake_send_wa_message(_device_id, _target, message):
        sent.append(message)

    async def fake_start_typing(_device_id, _target):
        return None

    monkeypatch.setattr("app.core.infra.wa_client.send_wa_message", fake_send_wa_message)
    monkeypatch.setattr("app.core.infra.wa_client.start_wa_typing", fake_start_typing)

    notify = build_wa_notify_tool(
        SimpleNamespace(channel_config={"device_id": "wa-device", "user_phone": "628111"})
    )[0]

    first = await notify.ainvoke({"message": "Masih saya proses ya. Saya akan kirim hasilnya begitu selesai."})
    second = await notify.ainvoke({"message": "Mohon tunggu sebentar, masih saya proses."})

    assert first == "notifikasi terkirim"
    assert "suppressed" in second
    assert sent == ["Masih saya proses ya. Saya akan kirim hasilnya begitu selesai."]


@pytest.mark.asyncio
async def test_notify_user_suppresses_media_delivery_claim(monkeypatch):
    from app.core.engine.tool_builder import build_wa_notify_tool

    sent: list[str] = []

    async def fake_send_wa_message(_device_id, _target, message):
        sent.append(message)

    async def fake_start_typing(_device_id, _target):
        return None

    monkeypatch.setattr("app.core.infra.wa_client.send_wa_message", fake_send_wa_message)
    monkeypatch.setattr("app.core.infra.wa_client.start_wa_typing", fake_start_typing)

    notify = build_wa_notify_tool(
        SimpleNamespace(channel_config={"device_id": "wa-device", "user_phone": "628111"})
    )[0]

    result = await notify.ainvoke({"message": "Bos, file PDF sudah siap saya kirim sekarang."})

    assert "send_whatsapp_document" in result
    assert sent == []


def test_media_delivery_claim_without_media_send_step_is_rewritten():
    reply = "Bos Bagas, file PDF Laporan_Titanic_Bagas.pdf sudah saya kirim ke WhatsApp."

    guarded = _whatsapp_media_delivery_guard_reply(reply, [])

    assert guarded.startswith("Belum saya kirim.")
    assert "tool kirim dokumen/gambar WhatsApp" in guarded


def test_media_delivery_guard_does_not_rewrite_demo_contact_plus_file_capability():
    reply = (
        "Bisa. Baas bisa bikin visualisasi data dan generate file PDF. "
        "Kontak Demo Baas sudah saya kirim."
    )

    assert _whatsapp_media_delivery_guard_reply(reply, []) == reply


def test_media_delivery_guard_rewrites_file_send_claim_in_same_sentence():
    reply = "Baas sudah selesai. Saya kirim file PDF-nya ke WhatsApp sekarang."

    guarded = _whatsapp_media_delivery_guard_reply(reply, [])

    assert guarded.startswith("Belum saya kirim.")


def test_media_delivery_claim_is_kept_after_media_send_step():
    reply = "Bos Bagas, file PDF Laporan_Titanic_Bagas.pdf sudah saya kirim ke WhatsApp."
    steps = [
        {
            "tool": "send_whatsapp_document",
            "result": "[DOCUMENT_SENT] Dokumen 'Laporan_Titanic_Bagas.pdf' dikirim ke 628111",
        }
    ]

    assert _whatsapp_media_delivery_guard_reply(reply, steps) == reply


def test_ascii_text_request_does_not_trigger_file_delivery_followup():
    from app.core.engine.agent_followups import _needs_whatsapp_file_delivery_followup

    steps = [
        {
            "tool": "task",
            "result": "Selesai: /workspace/shared/bite_radar_ascii.txt SIAP_DIKIRIM_PARENT",
        }
    ]

    needs_delivery, path = _needs_whatsapp_file_delivery_followup(
        "I said recreate it in ascii, send it all in text form",
        {"whatsapp_media": True},
        steps,
        "ASCII siap di /workspace/shared/bite_radar_ascii.txt",
    )

    assert needs_delivery is False
    assert path is None


@pytest.mark.asyncio
async def test_shared_pdf_followup_invokes_document_tool_directly():
    from app.core.engine.agent_runner import _deliver_shared_whatsapp_file_via_tool

    calls: list[dict] = []

    class FakeDocumentTool:
        name = "send_whatsapp_document"

        async def ainvoke(self, args):
            calls.append(args)
            return "[DOCUMENT_SENT] Dokumen 'Laporan.pdf' dikirim ke 628111"

    parsed = {
        "final_reply": "PDF siap di /workspace/shared/Laporan.pdf",
        "steps": [
            {
                "step": 1,
                "tool": "task",
                "args": {"name": "sys_coder"},
                "result": "Selesai: /workspace/shared/Laporan.pdf SIAP_DIKIRIM_PARENT",
            }
        ],
        "total_tokens_used": 0,
        "db_messages": [],
        "has_output": True,
    }

    sent, reply = await _deliver_shared_whatsapp_file_via_tool(
        tools=[FakeDocumentTool()],
        shared_path="/workspace/shared/Laporan.pdf",
        parsed=parsed,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step_index=10,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert sent is True
    assert reply == "File Laporan.pdf sudah saya kirim ke WhatsApp."
    assert calls == [
        {
            "file_path_or_base64": "/workspace/shared/Laporan.pdf",
            "filename": "Laporan.pdf",
            "caption": "Berikut file Laporan.pdf.",
        }
    ]
    assert parsed["steps"][-1]["tool"] == "send_whatsapp_document"
    assert parsed["db_messages"][-1].tool_name == "send_whatsapp_document"


@pytest.mark.asyncio
async def test_shared_text_artifact_falls_back_to_inline_reply_when_media_tool_missing(tmp_path, monkeypatch):
    from app.core.engine.agent_runner import _deliver_shared_whatsapp_file_via_tool
    from app.core.infra.sandbox import get_shared_dir

    session_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.core.infra.sandbox.get_settings",
        lambda: SimpleNamespace(sandbox_base_dir=str(tmp_path)),
    )
    shared_dir = get_shared_dir(session_id)
    (shared_dir / "bite_radar_ascii.txt").write_text("  /\\_/\\\\\n ( ASCII )\n", encoding="utf-8")

    parsed = {
        "final_reply": "ASCII siap di /workspace/shared/bite_radar_ascii.txt",
        "steps": [],
        "total_tokens_used": 0,
        "db_messages": [],
        "has_output": True,
    }

    sent, reply = await _deliver_shared_whatsapp_file_via_tool(
        tools=[],
        shared_path="/workspace/shared/bite_radar_ascii.txt",
        parsed=parsed,
        session_id=session_id,
        run_id=uuid.uuid4(),
        step_index=10,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )

    assert sent is True
    assert "ASCII" in reply
    assert "Tool send_whatsapp_document" not in reply


def test_send_to_number_blocks_media_delivery_claim_text():
    from app.core.tools.escalation_tool import _looks_like_media_delivery_text

    assert _looks_like_media_delivery_text(
        "Bos Bagas, ini file PDF hasil visualisasi data Titanic yang sudah saya buat. Silakan cek filenya di attachment ya."
    )
    assert _looks_like_media_delivery_text(
        "Maaf Bos, saya akan langsung kirim file PDF visualisasi data Titanic sekarang."
    )
    assert _looks_like_media_delivery_text(
        "Berikut saya kirimkan file PDF laporan visualisasi data Titanic yang Bos minta."
    )
    assert _looks_like_media_delivery_text(
        "Bos Bagas, file laporan sudah selesai dibuat. Saya kirim file PDF-nya ke WhatsApp Bos sekarang."
    )
    assert _looks_like_media_delivery_text("Silakan cek file terlampir.")
    assert not _looks_like_media_delivery_text("Halo Julia, meeting kita jam 3 sore ya.")


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

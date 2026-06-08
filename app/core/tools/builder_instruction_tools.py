"""Instruction writer tool for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import structlog
from langchain_core.tools import tool

from app.core.tools.builder_catalog import AGENT_PRESETS
from app.core.tools.builder_fallbacks import _fallback_agent_instructions
from app.core.tools.builder_intent import (
    _has_approval_state_contract,
    _looks_like_file_delivery_workflow,
    _looks_like_payment_approval_workflow,
    _sanitize_unverified_business_name,
    file_delivery_contract_issues,
)
from app.core.tools.builder_text import find_unfilled_placeholders as _find_unfilled_placeholders

logger = structlog.get_logger(__name__)

InstructionWriter = Callable[..., Awaitable[str]]
LoggerProvider = Callable[[], Any]


def build_builder_instruction_tools(
    *,
    call_instruction_writer: InstructionWriter,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _call_instruction_writer = call_instruction_writer
    _get_logger = get_logger or (lambda: logger)

    @tool
    async def compose_agent_instructions(
        preset_id: str,
        agent_name: str,
        business_context: str,
        persona: str = "ramah dan profesional",
        channel: str = "whatsapp",
        escalation_info: str = "",
        extra_rules: str = "",
        agent_blueprint: str = "",
    ) -> str:
        """
        Tulis system prompt (instructions) berkualitas tinggi untuk agent baru atau agent existing
        menggunakan model writer khusus (deepseek/deepseek-v4-pro).

        Hasilnya lebih spesifik, lebih kontekstual, dan lebih cerdas dibanding template manual.
        Tidak ada placeholder yang tersisa — semua diisi dengan info nyata.

        WAJIB dipanggil sebelum create_agent untuk agent baru, atau sebelum update_agent
        untuk agent existing yang sedang diperbaiki. Gunakan hasilnya sebagai parameter
        `instructions` saat memanggil create_agent/update_agent.
        Setelah hasil instructions valid, lanjutkan langsung ke create_agent atau update_agent
        sesuai konteks. Jangan minta persetujuan user untuk "lanjut" jika user sudah meminta
        agent dibuat/diperbaiki.

        Args:
            preset_id: Preset yang digunakan (coding_deploy_agent, cs_whatsapp_basic, dll)
            agent_name: Nama agent
            business_context: Info bisnis lengkap: produk, layanan, jam buka, kebijakan, harga, dll.
                              Semakin detail semakin baik. Kosong hanya untuk agent coding/general.
            persona: Gaya bicara dan karakter agent (misal: "hangat, sabar, suka bercanda")
            channel: Channel: 'whatsapp'
            escalation_info: Kondisi eskalasi dan info operator (misal: "Eskalasi jika komplain besar. Operator: +62812xxx")
            extra_rules: Aturan tambahan yang diminta user
            agent_blueprint: JSON/string hasil compose_agent_blueprint agar instructions mengikuti workflow custom user.
        """
        preset = AGENT_PRESETS.get(preset_id, {})
        skeleton = preset.get("instruction_skeleton", "")
        is_whatsapp = channel == "whatsapp"

        system_msg = (
            "Kamu adalah spesialis menulis system prompt untuk AI agent. "
            "Tugasmu: tulis system prompt yang kuat, spesifik, dan kontekstual — BUKAN template generik.\n\n"
            "ATURAN KERAS:\n"
            "1. JANGAN gunakan placeholder [xxx] atau {xxx} — isi semua dengan informasi nyata\n"
            f"2. {'JANGAN gunakan markdown (*, #, **) — channel WhatsApp tidak merender markdown' if is_whatsapp else 'Markdown boleh digunakan minimal'}\n"
            "3. Sertakan 2-3 contoh percakapan konkret (few-shot) yang mencerminkan bisnis ini\n"
            "4. Identitas harus kuat: nama, peran spesifik, bisnis, kepribadian yang jelas\n"
            "5. Aturan kerja harus spesifik dan actionable\n"
            "6. Bahasa harus mengikuti bahasa user; default Indonesia hanya jika bahasa user tidak jelas. Tetap natural dan sesuai persona yang diminta\n"
            "7. Panjang ideal: 250-500 kata — cukup detail tapi tidak bloated\n"
            "8. Mulai langsung dari 'Kamu adalah...' — tanpa intro atau penjelasan\n"
            "9. SKELETON REFERENSI hanya panduan struktur kapabilitas — JANGAN copy-paste. "
            "Sesuaikan seluruh konten dengan konteks bisnis, nama, dan kebutuhan spesifik user. "
            "Dua agent dengan preset sama tapi bisnis berbeda HARUS punya instructions yang berbeda.\n"
            "10. Jika ada AGENT BLUEPRINT, jadikan itu sumber utama workflow. "
            "Instructions harus memuat workflow steps, data wajib dikumpulkan, knowledge plan, memory plan, dan escalation rules.\n"
            "11. Untuk agent bisnis/jasa, tulis seperti SOP pekerja manusia: state kerja, data wajib, kapan boleh lanjut, kapan harus berhenti, "
            "kapan harus minta approval manusia, dan apa definisi task selesai.\n"
            "12. Jika ada pembayaran, bukti transfer, approval admin, atau deliverable berbayar, instructions WAJIB memuat state minimal: "
            "intake -> waiting_payment -> payment_review -> approved -> delivery -> aftercare. "
            "Agent tidak boleh mengirim deliverable sebelum payment approved, tidak boleh eskalasi payment berulang setelah approved, "
            "dan setelah approval harus melanjutkan workflow customer dari konteks customer.\n"
            "13. Instructions harus memuat identitas platform: agent dibuat oleh Arthur, memiliki Owner, dan Owner adalah bos/superadmin. "
            "Agent harus minta bantuan Owner/operator saat butuh keputusan manusia, izin Google/akun, atau ada masalah yang tidak bisa diselesaikan sendiri.\n"
            "14. JANGAN mengarang nama brand/bisnis. Jika user tidak memberi nama bisnis eksplisit, sebut saja 'bisnis ini', "
            "'usaha ini', atau deskripsi bisnisnya."
        )

        # Build tool hints so the instruction writer knows which tools are available
        tc_preset = preset.get("tools_config", {})
        tool_hints: list[str] = []
        if tc_preset.get("memory"):
            tool_hints.append(
                "- remember(key, value) / recall(key) / forget(key) — simpan dan ambil info user lintas sesi. "
                "Gunakan untuk menyimpan preferensi, nama, konteks penting yang perlu diingat antar percakapan."
            )
        if tc_preset.get("escalation"):
            tool_hints.append(
                "- escalate_to_human(reason, summary) — eskalasi ke operator. "
                "Jika user mengirim bukti transfer/gambar/dokumen lalu butuh approval manusia, "
                "ringkas konteksnya dan panggil tool ini; sistem akan meneruskan notifikasi dan lampiran terakhir ke operator. "
                "- reply_to_user(message) — hanya untuk sesi operator. "
                "Jika operator meminta draft, tampilkan draft dulu; jika operator sudah bilang 'kirim', "
                "'langsung kirim', atau 'rapihin terus kirim', rapikan pesan lalu panggil reply_to_user(message)."
            )
        if tc_preset.get("http"):
            tool_hints.append(
                "- http_get(url) / http_post(url, body) / http_patch(url, body) / http_delete(url) — "
                "akses API eksternal, ambil data dari web, atau kirim data ke sistem lain."
            )
        if tc_preset.get("tavily", True):
            tool_hints.append(
                "- tavily_search(query) / tavily_extract(urls, query) — browsing web dengan Tavily. "
                "Gunakan untuk info terbaru, riset, rekomendasi, berita, harga, dan membaca sumber URL."
            )
        if tc_preset.get("wa_agent_manager"):
            tool_hints.append(
                "- send_agent_wa_qr(agent_id, caption, phone) — kirim QR WhatsApp ke nomor tertentu agar user bisa scan dan connect."
            )
        tool_hints.append(
            "- create_wa_dev_trial_link(agent_id, agent_name, phone, force_new_code, send_contact) — "
            "buat kode 6 karakter + link wa.me untuk user mencoba agent lewat nomor WhatsApp shared Arthur tanpa scan QR. "
            "Jika user menyebut nama agent tertentu, isi agent_name atau agent_id agar tidak salah target."
        )
        tool_hints.append(
            "- set_agent_memory(agent_id, key, value) — simpan soul atau blueprint ke memory agent setelah create, tanpa HTTP/API."
        )
        if tc_preset.get("scheduler"):
            tool_hints.append(
                "- set_reminder(message, run_at) / list_reminders() / cancel_reminder(id) — jadwalkan pengingat otomatis untuk user."
            )
        if tc_preset.get("rag"):
            tool_hints.append(
                "- search_documents(query) — cari jawaban dari dokumen/knowledge base yang sudah diupload."
            )
        if tc_preset.get("whatsapp_media"):
            tool_hints.append(
                "- send_whatsapp_document(file_path_or_base64, filename, caption) / send_whatsapp_image(...) — "
                "kirim file atau gambar langsung ke WhatsApp user. Gunakan untuk delivery dokumen final setelah approval."
            )
        subagents_cfg = tc_preset.get("subagents", {})
        if isinstance(subagents_cfg, dict) and subagents_cfg.get("enabled"):
            tool_hints.append(
                "- task(name, task) — delegasikan pekerjaan ke sub-agent spesialis. "
                "Contoh: task('sys_coder', 'Buat file PDF ...'), task('sys_researcher', 'Cari info ...')."
            )
        tool_hints.append(
            "- get_self_config() — baca konfigurasi diri sendiri (nama, model, tools aktif). "
            "Berguna untuk menjawab pertanyaan user tentang kemampuan agent."
        )
        tools_section = (
            "\n\nTOOLS YANG TERSEDIA UNTUK AGENT INI (wajib disebutkan di instructions cara pakainya):\n"
            + "\n".join(tool_hints)
        ) if tool_hints else ""

        # For coding agents, add sys_coder delegation context
        coder_note = ""
        if preset_id == "coding_deploy_agent":
            coder_note = (
                "\n\nCATATAN PENTING UNTUK CODING AGENT:\n"
                "Agent ini memiliki subagent bernama sys_coder yang tugasnya KHUSUS eksekusi kode dan deploy website.\n"
                "System prompt HARUS instruksikan agent untuk:\n"
                "- Delegasikan SEMUA task coding/web/deploy ke sys_coder via task(name='sys_coder', task='...')\n"
                "- Untuk request web/frontend, task description WAJIB menyebut: vanilla HTML/CSS/JavaScript terpisah, tanpa framework, tanpa inline CSS/JS\n"
                "- task description ke sys_coder harus RINGKAS (maks 3-4 kalimat): sebutkan apa yang dibuat, teknologi vanilla, port jika perlu\n"
                "- JANGAN sertakan spec detail, pseudocode, atau desain panjang di task description — sys_coder sudah tau cara kerjanya\n"
                "- sys_coder akan menulis file, deploy, dan kembalikan URL publik\n"
                "- Main agent hanya orchestrate: terima request → delegate ke sys_coder → relay hasil ke user\n"
                "- JANGAN instruksikan main agent untuk nulis kode sendiri\n"
            )

        user_msg = (
            f"Tulis system prompt untuk agent berikut:\n\n"
            f"NAMA AGENT: {agent_name}\n"
            f"PRESET: {preset_id} ({preset.get('label', '')})\n"
            f"CHANNEL: {channel}\n"
            f"PERSONA: {persona}\n\n"
            f"KONTEKS BISNIS:\n{business_context or 'Agent umum tanpa konteks bisnis spesifik'}\n\n"
            f"INFO ESKALASI:\n{escalation_info or 'Tidak ada eskalasi khusus'}\n\n"
            f"ATURAN TAMBAHAN:\n{extra_rules or 'Tidak ada'}\n\n"
            f"AGENT BLUEPRINT CUSTOM:\n{agent_blueprint or 'Tidak ada blueprint khusus'}\n\n"
            f"SKELETON REFERENSI (jadikan panduan struktur, jangan copy-paste):\n{skeleton[:600] if skeleton else 'Tidak ada'}"
            f"{tools_section}"
            f"{coder_note}\n\n"
            "Tulis system prompt lengkap sekarang. "
            "Pastikan instructions menyebutkan tools yang tersedia dan kapan/cara menggunakannya secara konkret."
        )

        try:
            instructions = await _call_instruction_writer(user_msg, system_msg)
            empty_writer_output = not str(instructions or "").strip()
            file_delivery_workflow = _looks_like_file_delivery_workflow(
                business_context,
                escalation_info,
                extra_rules,
            )
            needs_payment_contract = (
                preset_id == "approval_gated_service_agent"
                or _looks_like_payment_approval_workflow(
                    business_context,
                    escalation_info,
                    extra_rules,
                )
            )
            weak_payment_instructions = (
                needs_payment_contract
                and (
                    len(instructions.strip()) < 1200
                    or not _has_approval_state_contract(instructions)
                    or "escalate_to_human" not in instructions
                    or bool(file_delivery_contract_issues(instructions, file_delivery=file_delivery_workflow))
                )
            )
            hallucinated_payment_contract = (
                not needs_payment_contract
                and (
                    _has_approval_state_contract(instructions)
                    or "bukti transfer" in instructions.lower()
                    or "payment_review" in instructions.lower()
                    or "waiting_payment" in instructions.lower()
                    or "send_whatsapp_document" in instructions.lower()
                    or "file final" in instructions.lower()
                )
            )
            if weak_payment_instructions or hallucinated_payment_contract:
                _get_logger().warning(
                    "builder_tools.compose_agent_instructions.deterministic_fallback",
                    preset_id=preset_id,
                    agent_name=agent_name,
                    char_count=len(instructions or ""),
                    reason=(
                        "weak_payment_contract"
                        if weak_payment_instructions
                        else "hallucinated_payment_contract"
                    ),
                )
                instructions = _fallback_agent_instructions(
                    preset_id=preset_id,
                    agent_name=agent_name,
                    business_context=business_context,
                    persona=persona,
                    channel=channel,
                    escalation_info=escalation_info,
                    extra_rules=extra_rules,
                    agent_blueprint=agent_blueprint,
                )
                fallback_status = (
                    "deterministic_fallback"
                    if weak_payment_instructions
                    else "deterministic_fallback_removed_hallucinated_payment"
                )
            elif empty_writer_output:
                _get_logger().warning(
                    "builder_tools.compose_agent_instructions.empty_writer_output",
                    preset_id=preset_id,
                    agent_name=agent_name,
                )
                instructions = _fallback_agent_instructions(
                    preset_id=preset_id,
                    agent_name=agent_name,
                    business_context=business_context,
                    persona=persona,
                    channel=channel,
                    escalation_info=escalation_info,
                    extra_rules=extra_rules,
                    agent_blueprint=agent_blueprint,
                )
                fallback_status = "deterministic_fallback_empty_writer_output"
            else:
                fallback_status = None

            instructions, business_name_sanitized = _sanitize_unverified_business_name(
                instructions,
                business_context=business_context,
            )

            # Sanity check: flag only real template placeholders.
            placeholders = _find_unfilled_placeholders(instructions)

            payload = {
                "instructions": instructions,
                "char_count": len(instructions),
                "remaining_placeholders": placeholders,
                "warning": (
                    f"PERINGATAN: Masih ada {len(placeholders)} placeholder yang belum diisi: {placeholders}. "
                    "Panggil compose_agent_instructions ulang dengan business_context yang lebih lengkap."
                    if placeholders else None
                ),
                "next_step": (
                    "Gunakan 'instructions' di atas sebagai parameter create_agent untuk agent baru, "
                    "atau update_agent untuk agent existing yang sedang diperbaiki. "
                    "Jika remaining_placeholders tidak kosong, perbaiki secara manual atau panggil ulang maksimal satu kali. "
                    "Jika valid untuk agent baru, langsung create_agent tanpa tanya approval lagi. "
                    "Jika valid untuk agent existing, langsung update_agent tanpa tanya approval lagi."
                ),
            }
            if fallback_status:
                payload["fallback_status"] = fallback_status
            if business_name_sanitized:
                payload["business_name_sanitized"] = True
            return json.dumps(payload, ensure_ascii=False, indent=2)

        except Exception as exc:
            _get_logger().error("builder_tools.compose_agent_instructions.error", error=str(exc))
            fallback = _fallback_agent_instructions(
                preset_id=preset_id,
                agent_name=agent_name,
                business_context=business_context,
                persona=persona,
                channel=channel,
                escalation_info=escalation_info,
                extra_rules=extra_rules,
                agent_blueprint=agent_blueprint,
            )
            return json.dumps({
                "error": f"Gagal generate dengan model reasoning: {exc}",
                "fallback_skeleton": fallback,
                "note": "Gunakan fallback_skeleton sebagai instructions jika validasi lolos. Pastikan tidak ada placeholder.",
            }, ensure_ascii=False)

    return {"compose_agent_instructions": compose_agent_instructions}

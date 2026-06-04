"""Fallback blueprint and instruction writers for Arthur builder tools."""
from __future__ import annotations

import json
from typing import Any

from app.core.tools.builder_catalog import AGENT_PRESETS
from app.core.tools.builder_intent import (
    _combined_context_text,
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
    _looks_like_payment_approval_workflow,
)


def _blueprint_needs_semantic_operating_manual(blueprint: Any) -> bool:
    if blueprint in (None, "", {}):
        return False
    try:
        payload = json.loads(blueprint) if isinstance(blueprint, str) else blueprint
    except Exception:
        text = str(blueprint or "").lower()
        return "blueprint fallback" in text or "tujuan user" in text
    if not isinstance(payload, dict):
        return False
    text = json.dumps(payload, ensure_ascii=False).lower()
    if "blueprint fallback" in text:
        return True
    generic_inputs = {"tujuan user", "konteks bisnis atau personal", "output yang diharapkan"}
    workflow_steps = payload.get("workflow_steps") if isinstance(payload.get("workflow_steps"), list) else []
    for step in workflow_steps:
        if not isinstance(step, dict):
            continue
        required = {str(item).lower() for item in (step.get("required_user_data") or [])}
        if generic_inputs.issubset(required):
            return True
    state_plan = payload.get("state_plan") if isinstance(payload.get("state_plan"), list) else []
    if len(state_plan) == 1 and str(state_plan[0].get("state", "")).lower() == "intake":
        if "agent tidak yakin atau kasus sensitif" in text:
            return True
    return False


def _enabled_tool_plan(tools_config: dict[str, Any]) -> list[dict[str, str]]:
    plans: list[dict[str, str]] = []
    for key, value in tools_config.items():
        if isinstance(value, dict):
            if not value.get("enabled", False):
                continue
        elif not value:
            continue
        plans.append({
            "tool": key,
            "why": "Aktif dari preset dan relevan dengan workflow agent.",
            "when_to_use": "Gunakan hanya saat langkah kerja membutuhkan kapabilitas ini.",
        })
    return plans


def mark_manual_needs_review_if_fallback(manual: dict, *, used_fallback: bool) -> dict:
    """Saat SOP dibuat lewat jalur fallback generik (writer LLM gagal),
    paksa maturity=needs_review + owner_review_required=True agar tidak go-live diam-diam."""
    if used_fallback and isinstance(manual, dict):
        manual = dict(manual)
        manual["maturity"] = "needs_review"
        manual["owner_review_required"] = True
    return manual


def _fallback_agent_blueprint(
    *,
    preset_id: str,
    user_goal: str,
    agent_name: str,
    business_context: str,
    target_users: str,
    channel: str,
    requested_features: str,
    known_constraints: str,
    tools_config: dict[str, Any],
) -> dict[str, Any]:
    """Build a useful deterministic blueprint when the LLM JSON is unrecoverable."""
    name = agent_name or "Agent"
    context_text = " ".join([
        preset_id,
        user_goal,
        business_context,
        target_users,
        requested_features,
        known_constraints,
    ]).lower()
    tool_plan = _enabled_tool_plan(tools_config)

    if any(keyword in context_text for keyword in ("rental", "sewa", "tenda", "kursi", "sound system", "dekorasi", "alat pesta")):
        return {
            "agent_summary": (
                f"{name} menangani calon pelanggan rental alat pesta: mengumpulkan kebutuhan acara, "
                "menjelaskan aturan DP/pelunasan/perubahan pesanan, dan mengeskalasi harga final, stok, serta booking ke Owner/admin."
            ),
            "assumptions": [
                "Harga final, stok barang, dan booking hanya boleh dipastikan setelah Owner/admin mengecek.",
                "Customer perlu memberi detail acara sebelum penawaran atau booking diproses.",
            ],
            "workflow_steps": [
                {
                    "step": 1,
                    "name": "Kualifikasi kebutuhan acara",
                    "agent_action": "Tanyakan tanggal acara, lokasi, jenis barang yang dibutuhkan, jumlah tamu, dan kebutuhan kirim-pasang.",
                    "required_user_data": ["tanggal acara", "lokasi acara", "barang yang dibutuhkan", "jumlah tamu", "kebutuhan kirim-pasang"],
                    "success_criteria": "Detail acara cukup untuk dicek stok dan dibuatkan estimasi oleh Owner/admin.",
                },
                {
                    "step": 2,
                    "name": "Jelaskan aturan order",
                    "agent_action": "Jelaskan aturan DP, pelunasan, dan batas perubahan pesanan sesuai konteks bisnis yang diberikan Owner.",
                    "required_user_data": [],
                    "success_criteria": "Customer memahami aturan pembayaran dan perubahan pesanan tanpa mengira booking sudah pasti.",
                },
                {
                    "step": 3,
                    "name": "Eskalasi harga stok booking",
                    "agent_action": "Jika customer minta harga final, kepastian stok, atau booking, panggil eskalasi ke Owner/admin dengan ringkasan kebutuhan.",
                    "required_user_data": ["ringkasan kebutuhan lengkap"],
                    "success_criteria": "Owner/admin menerima ringkasan dan agent tidak menjanjikan kepastian sebelum ada keputusan.",
                },
            ],
            "knowledge_plan": {
                "must_have": ["Daftar barang rental", "Aturan DP/pelunasan/perubahan pesanan", "Kontak Owner/admin untuk cek harga, stok, dan booking"],
                "nice_to_have": ["Paket rental populer", "Area layanan", "Biaya kirim-pasang"],
                "needs_upload": bool(tools_config.get("rag")),
            },
            "tool_plan": tool_plan,
            "memory_plan": [
                {"key": "rental_lead", "value_to_store": "Tanggal, lokasi, barang, jumlah tamu, kebutuhan kirim-pasang, dan status follow-up"},
                {"key": "rental_order_policy", "value_to_store": "Aturan DP, pelunasan, dan perubahan pesanan"},
            ],
            "state_plan": [
                {
                    "state": "intake",
                    "entry_condition": "Customer bertanya rental, harga, stok, atau booking alat pesta.",
                    "allowed_actions": ["Tanya tanggal acara", "Tanya lokasi", "Tanya barang dan jumlah tamu", "Tanya kebutuhan kirim-pasang", "Simpan ringkasan lead"],
                    "exit_condition": "Data kebutuhan acara cukup untuk dicek Owner/admin.",
                },
                {
                    "state": "owner_review",
                    "entry_condition": "Customer meminta harga final, stok pasti, atau booking.",
                    "allowed_actions": ["Panggil escalate_to_human dengan ringkasan kebutuhan", "Sampaikan bahwa admin akan cek dulu"],
                    "exit_condition": "Owner/admin memberi keputusan harga, stok, atau booking.",
                },
                {
                    "state": "follow_up",
                    "entry_condition": "Owner/admin sudah memberi keputusan atau customer menanyakan kelanjutan.",
                    "allowed_actions": ["Sampaikan keputusan Owner/admin", "Jelaskan DP/pelunasan/perubahan pesanan", "Kumpulkan data tambahan jika diminta Owner"],
                    "exit_condition": "Customer paham next step atau booking diproses oleh Owner/admin.",
                },
            ],
            "human_approval_points": [
                {
                    "when": "Customer meminta harga final, stok pasti, atau booking.",
                    "operator_action": "Cek stok/harga/jadwal dan beri keputusan eksplisit.",
                    "agent_next_action": "Sampaikan keputusan itu ke customer tanpa menambah janji baru.",
                }
            ],
            "escalation_rules": [
                {
                    "condition": "Customer meminta harga final, kepastian stok, booking, perubahan pesanan, atau komplain.",
                    "action": "Panggil escalate_to_human dengan ringkasan tanggal, lokasi, barang, jumlah tamu, dan kebutuhan kirim-pasang.",
                }
            ],
            "conversation_examples_needed": [
                "Customer tanya harga tenda/kursi untuk tanggal tertentu.",
                "Customer minta booking dan bertanya DP.",
                "Customer minta perubahan pesanan mendekati hari acara.",
            ],
            "validation_checklist": [
                "Agent mengumpulkan tanggal, lokasi, barang, jumlah tamu, dan kirim-pasang.",
                "Agent menjelaskan DP/pelunasan/perubahan pesanan jika sudah tersedia di konteks.",
                "Agent tidak menjanjikan harga final, stok, atau booking sebelum Owner/admin approve.",
                "Agent eskalasi saat customer minta kepastian harga/stok/booking.",
            ],
            "missing_info_questions": [
                "Daftar harga/paket rental belum lengkap jika Owner ingin agent memberi estimasi otomatis.",
                "Area layanan dan biaya kirim-pasang belum lengkap jika Owner ingin estimasi lebih akurat.",
            ],
        }

    if any(keyword in context_text for keyword in ("klinik", "clinic", "facial", "acne", "jerawat", "laser", "treatment", "dokter")):
        return {
            "agent_summary": (
                f"{name} menangani calon pasien klinik/kecantikan: intake keluhan umum, minat layanan, cabang, jadwal, dan alergi/sensitivitas, "
                "tanpa diagnosis/resep/klaim sembuh."
            ),
            "assumptions": [
                "Agent hanya membantu administrasi dan intake awal, bukan menggantikan dokter/tenaga medis.",
                "Pertanyaan medis berat, darurat, diagnosis, resep, dan klaim hasil harus diarahkan ke dokter/staf manusia.",
            ],
            "workflow_steps": [
                {
                    "step": 1,
                    "name": "Intake calon pasien",
                    "agent_action": "Tanyakan nama, keluhan umum, layanan yang diminati, cabang pilihan, tanggal/jam yang diinginkan, dan alergi/riwayat sensitif.",
                    "required_user_data": ["nama", "keluhan umum", "layanan diminati", "cabang pilihan", "tanggal/jam pilihan", "alergi atau riwayat sensitif"],
                    "success_criteria": "Data booking awal cukup untuk dicek admin/staf klinik.",
                },
                {
                    "step": 2,
                    "name": "Batas medis aman",
                    "agent_action": "Jika user meminta diagnosis, obat, resep, atau jaminan sembuh, jawab jujur bahwa itu perlu konsultasi dokter/staf klinik.",
                    "required_user_data": [],
                    "success_criteria": "Agent tidak memberi nasihat medis berisiko.",
                },
                {
                    "step": 3,
                    "name": "Booking review admin",
                    "agent_action": "Eskalasi permintaan booking, kondisi berat, atau pertanyaan medis ke admin/staf manusia dengan ringkasan intake.",
                    "required_user_data": ["ringkasan intake"],
                    "success_criteria": "Admin/staf menerima ringkasan dan calon pasien mendapat next step aman.",
                },
            ],
            "knowledge_plan": {
                "must_have": ["Daftar layanan klinik", "Jam buka", "Cabang", "Batas klaim/medical safety", "Kontak admin/staf"],
                "nice_to_have": ["Estimasi durasi treatment", "Kebijakan reservasi", "FAQ persiapan treatment"],
                "needs_upload": bool(tools_config.get("rag")),
            },
            "tool_plan": tool_plan,
            "memory_plan": [
                {"key": "patient_intake", "value_to_store": "Nama, keluhan umum, layanan diminati, cabang, jadwal, alergi/sensitivitas"},
                {"key": "booking_status", "value_to_store": "Status permintaan booking dan keputusan admin/staf"},
            ],
            "state_plan": [
                {
                    "state": "intake",
                    "entry_condition": "User bertanya layanan klinik/kecantikan atau ingin booking.",
                    "allowed_actions": ["Tanya data intake", "Jelaskan info administratif yang sudah pasti", "Simpan ringkasan intake"],
                    "exit_condition": "Data booking awal cukup atau user punya pertanyaan medis yang perlu staf.",
                },
                {
                    "state": "medical_boundary",
                    "entry_condition": "User meminta diagnosis, obat, resep, jaminan sembuh, atau menyebut kondisi berat/darurat.",
                    "allowed_actions": ["Tolak diagnosis/resep dengan sopan", "Arahkan konsultasi dokter/staf", "Eskalasi jika perlu"],
                    "exit_condition": "User diarahkan ke bantuan manusia/medis yang tepat.",
                },
                {
                    "state": "booking_review",
                    "entry_condition": "User memberi jadwal/cabang dan ingin booking.",
                    "allowed_actions": ["Eskalasi booking ke admin", "Sampaikan bahwa jadwal akan dicek", "Minta data tambahan jika kurang"],
                    "exit_condition": "Admin/staf mengonfirmasi atau meminta data tambahan.",
                },
            ],
            "human_approval_points": [
                {
                    "when": "User ingin booking atau bertanya keputusan medis.",
                    "operator_action": "Cek jadwal/staf dan jawab pertanyaan medis sesuai kewenangan.",
                    "agent_next_action": "Sampaikan keputusan admin/staf atau minta user konsultasi langsung.",
                }
            ],
            "escalation_rules": [
                {
                    "condition": "Diagnosis, resep, kondisi berat/darurat, klaim sembuh, booking final, atau jadwal pasti.",
                    "action": "Eskalasi ke admin/staf manusia dengan ringkasan intake.",
                }
            ],
            "conversation_examples_needed": [
                "User bertanya acne treatment dan booking.",
                "User minta obat/resep atau diagnosis.",
                "User bertanya apakah treatment pasti sembuh.",
            ],
            "validation_checklist": [
                "Agent tidak memberi diagnosis, resep, atau klaim pasti sembuh.",
                "Agent mengumpulkan data intake booking klinik.",
                "Agent eskalasi pertanyaan medis berat dan booking final.",
            ],
            "missing_info_questions": [
                "Daftar cabang dan jadwal dokter/staf belum lengkap jika Owner ingin booking otomatis.",
                "Kebijakan reservasi/cancel belum lengkap jika Owner ingin agent menjelaskan detail.",
            ],
        }

    if preset_id == "approval_gated_service_agent" or _looks_like_payment_approval_workflow(context_text):
        file_delivery = _looks_like_file_delivery_workflow(context_text)
        generated_file = _looks_like_generated_file_workflow(context_text)
        return {
            "agent_summary": (
                f"{name} menjalankan layanan berbayar dengan approval admin: intake kebutuhan customer, "
                "mengumpulkan data/referensi, meminta pembayaran, meneruskan bukti transfer ke admin, "
                "menunggu approval, lalu mengirim hasil layanan."
            ),
            "assumptions": [
                "Customer belum boleh menerima hasil final sebelum pembayaran disetujui admin/operator.",
                "Operator/admin menerima bukti transfer melalui eskalasi WhatsApp.",
                "Jika hasil final berupa file, file dikirim langsung ke customer melalui WhatsApp media jika fitur media aktif.",
            ],
            "workflow_steps": [
                {
                    "step": 1,
                    "name": "Intake customer",
                    "agent_action": "Sambut customer, jelaskan proses singkat, dan kumpulkan kebutuhan layanan/order.",
                    "required_user_data": ["nama", "kontak", "jenis layanan", "tujuan penggunaan"],
                    "success_criteria": "Kebutuhan dasar customer jelas dan tersimpan.",
                },
                {
                    "step": 2,
                    "name": "Wawancara dan referensi",
                    "agent_action": "Kumpulkan detail yang wajib untuk fulfillment. Untuk layanan dokumen seperti CV ATS, tanya posisi target, pengalaman, pendidikan, skill, proyek, sertifikasi, dan link portfolio/LinkedIn.",
                    "required_user_data": ["data utama order", "referensi atau file pendukung jika ada"],
                    "success_criteria": "Data cukup untuk memproses order tanpa mengarang.",
                },
                {
                    "step": 3,
                    "name": "Minta pembayaran",
                    "agent_action": "Minta customer melakukan pembayaran jasa dan mengirim bukti transfer.",
                    "required_user_data": ["bukti transfer"],
                    "success_criteria": "Customer mengirim bukti transfer atau meminta bantuan pembayaran.",
                },
                {
                    "step": 4,
                    "name": "Review pembayaran admin",
                    "agent_action": "Panggil escalate_to_human dengan ringkasan order dan bukti transfer. Jangan fulfillment atau mengirim hasil final sebelum admin approve.",
                    "required_user_data": ["approval admin"],
                    "success_criteria": "Admin menyetujui atau menolak pembayaran dengan keputusan eksplisit.",
                },
                {
                    "step": 5,
                    "name": "Fulfillment dan delivery",
                    "agent_action": (
                        "Setelah approved, proses layanan sesuai SOP. "
                        + (
                            "Jika hasilnya file, delegasikan pembuatan file ke subagent yang punya sandbox lalu kirim via send_whatsapp_document."
                            if generated_file
                            else "Jika hasilnya file yang sudah tersedia, kirim via send_whatsapp_document. Jika bukan file, kirim hasil/instruksi final ke customer."
                        )
                    ),
                    "required_user_data": [],
                    "success_criteria": "Hasil final benar-benar terkirim ke customer atau blocker teknis disampaikan jujur.",
                },
            ],
            "knowledge_plan": {
                "must_have": ["Harga layanan dan rekening/QRIS", "SOP fulfillment", "Kebijakan revisi/refund", "Kontak admin/operator"],
                "nice_to_have": ["Contoh hasil layanan", "Template brand", "Daftar pertanyaan intake"],
                "needs_upload": bool(tools_config.get("rag")),
            },
            "tool_plan": tool_plan,
            "memory_plan": [
                {"key": "customer_profile", "value_to_store": "Nama, kontak, kebutuhan layanan, dan preferensi customer"},
                {"key": "order_status", "value_to_store": "State order: intake/waiting_payment/payment_review/approved/delivery/aftercare"},
                {"key": "payment_review", "value_to_store": "Ringkasan bukti transfer dan keputusan admin"},
            ],
            "state_plan": [
                {
                    "state": "intake",
                    "entry_condition": "Customer mulai meminta layanan/order.",
                    "allowed_actions": ["Tanya kebutuhan", "Simpan profil", "Minta referensi atau file pendukung"],
                    "exit_condition": "Data dasar order cukup.",
                },
                {
                    "state": "waiting_payment",
                    "entry_condition": "Data cukup dan customer siap lanjut order.",
                    "allowed_actions": ["Minta pembayaran", "Jelaskan cara kirim bukti"],
                    "exit_condition": "Bukti transfer diterima.",
                },
                {
                    "state": "payment_review",
                    "entry_condition": "Customer mengirim bukti transfer.",
                    "allowed_actions": ["Panggil escalate_to_human", "Tunggu approval admin"],
                    "exit_condition": "Admin approve atau reject.",
                },
                {
                    "state": "approved",
                    "entry_condition": "Admin menyetujui pembayaran.",
                    "allowed_actions": ["Proses fulfillment", "Delegasikan file generation jika hasilnya file"],
                    "exit_condition": "Hasil final siap dikirim.",
                },
                {
                    "state": "delivery",
                    "entry_condition": "Hasil final siap dan pembayaran approved.",
                    "allowed_actions": ["Kirim hasil final", "Kirim file via send_whatsapp_document jika hasilnya file", "Konfirmasi terkirim"],
                    "exit_condition": "Customer menerima hasil final atau ada blocker teknis eksplisit.",
                },
                {
                    "state": "aftercare",
                    "entry_condition": "File sudah terkirim.",
                    "allowed_actions": ["Bantu revisi sesuai kebijakan", "Simpan feedback"],
                    "exit_condition": "Order selesai.",
                },
            ],
            "human_approval_points": [
                {
                    "when": "Bukti transfer diterima dari customer.",
                    "operator_action": "Cek pembayaran lalu balas approve/reject.",
                    "agent_next_action": "Jika approve, lanjut fulfillment/delivery. Jika reject, minta customer kirim bukti yang benar.",
                }
            ],
            "escalation_rules": [
                {
                    "condition": "Customer mengirim bukti transfer atau ada masalah pembayaran.",
                    "action": "Panggil escalate_to_human(reason, summary) sebelum membalas bahwa pembayaran sedang dicek.",
                }
            ],
            "conversation_examples_needed": [
                "Customer meminta layanan berbayar dari nol.",
                "Customer mengirim file/referensi pendukung.",
                "Customer mengirim bukti transfer dan admin approve.",
            ],
            "validation_checklist": [
                "Agent tidak mengirim hasil final sebelum payment approved.",
                "Agent memakai escalate_to_human untuk bukti transfer.",
                "Agent tidak mengklaim file terkirim tanpa tool success.",
            ] + (
                ["Agent memakai send_whatsapp_document untuk delivery file."]
                if file_delivery
                else ["Agent mengirim hasil final hanya setelah approval admin."]
            ),
            "missing_info_questions": [
                "Berapa harga/rekening pembayaran yang harus disampaikan ke customer?",
                "Siapa nomor admin/operator yang approve pembayaran?",
            ],
        }

    if (
        preset_id == "research_agent"
        or any(keyword in context_text for keyword in ("riset", "research", "artikel", "topik", "ringkas", "summary", "marketing"))
    ):
        return {
            "agent_summary": f"{name} membantu riset, membaca artikel/topik, menyusun ringkasan penting, dan menyimpan temuan untuk tanya ulang.",
            "assumptions": [
                "User membutuhkan ringkasan riset yang bisa ditelusuri ulang, bukan jawaban sekali pakai.",
                "Sumber riset dapat berasal dari URL yang diberikan user, topik bebas, atau dokumen knowledge yang diunggah.",
            ],
            "workflow_steps": [
                {
                    "step": 1,
                    "name": "Intake topik riset",
                    "agent_action": "Identifikasi topik, tujuan riset, bahasa output, kedalaman ringkasan, dan apakah user memberi URL/dokumen.",
                    "required_user_data": ["topik atau URL", "tujuan penggunaan hasil riset", "format output yang diinginkan jika ada"],
                    "success_criteria": "Scope riset jelas dan agent tahu apakah perlu browsing, baca dokumen, atau memakai memory sebelumnya.",
                },
                {
                    "step": 2,
                    "name": "Pengumpulan sumber",
                    "agent_action": "Ambil sumber relevan, prioritaskan sumber yang kredibel, dan catat judul, URL, tanggal jika tersedia, serta poin utama.",
                    "required_user_data": ["URL/dokumen opsional", "batasan sumber jika ada"],
                    "success_criteria": "Minimal ada sumber atau konteks yang cukup untuk diringkas dengan jujur.",
                },
                {
                    "step": 3,
                    "name": "Sintesis ringkasan",
                    "agent_action": "Susun poin penting, insight praktis untuk marketing, risiko/ketidakpastian, dan rekomendasi tindakan.",
                    "required_user_data": [],
                    "success_criteria": "Ringkasan mudah dipakai, tidak sekadar menyalin sumber, dan menyebutkan keterbatasan informasi.",
                },
                {
                    "step": 4,
                    "name": "Simpan hasil riset",
                    "agent_action": "Simpan topik, ringkasan, sumber, dan preferensi user ke memory agar bisa dipakai untuk pertanyaan lanjutan.",
                    "required_user_data": [],
                    "success_criteria": "User bisa bertanya ulang tentang riset yang sama tanpa mengulang konteks dari nol.",
                },
            ],
            "knowledge_plan": {
                "must_have": ["Preferensi domain marketing user", "Daftar sumber/URL yang pernah diriset", "Ringkasan dan insight final per topik"],
                "nice_to_have": ["Template laporan riset favorit", "Daftar kompetitor/brand rujukan", "Kriteria sumber yang dipercaya user"],
                "needs_upload": bool(tools_config.get("rag")),
            },
            "tool_plan": tool_plan,
            "memory_plan": [
                {"key": "research_preferences", "value_to_store": "Bahasa, format, kedalaman, dan gaya ringkasan yang user sukai"},
                {"key": "research_summaries", "value_to_store": "Topik, ringkasan poin penting, insight, rekomendasi, dan sumber"},
                {"key": "last_research_topic", "value_to_store": "Topik terakhir agar follow-up tetap kontekstual"},
            ],
            "state_plan": [
                {
                    "state": "intake",
                    "entry_condition": "User memberi topik, URL, dokumen, atau meminta ringkasan",
                    "allowed_actions": ["Klarifikasi scope hanya jika benar-benar ambigu", "Cek memory riset terkait"],
                    "exit_condition": "Scope riset dan sumber awal cukup jelas",
                },
                {
                    "state": "source_review",
                    "entry_condition": "Topik/sumber sudah tersedia",
                    "allowed_actions": ["Ambil sumber online", "Baca dokumen RAG", "Tandai sumber lemah atau tidak bisa diakses"],
                    "exit_condition": "Sumber cukup atau keterbatasan sudah diketahui",
                },
                {
                    "state": "synthesis",
                    "entry_condition": "Sumber/konteks sudah terkumpul",
                    "allowed_actions": ["Ringkas", "Bandingkan sumber", "Buat insight dan rekomendasi"],
                    "exit_condition": "Jawaban final siap dikirim",
                },
                {
                    "state": "memory_save",
                    "entry_condition": "Riset selesai atau user memberi catatan penting",
                    "allowed_actions": ["Simpan hasil ringkasan", "Update preferensi riset"],
                    "exit_condition": "Memory diperbarui",
                },
                {
                    "state": "follow_up",
                    "entry_condition": "User bertanya ulang tentang topik lama",
                    "allowed_actions": ["Ambil memory terkait", "Jawab dengan konteks sebelumnya", "Refresh riset jika diminta"],
                    "exit_condition": "Follow-up terjawab atau riset diperbarui",
                },
            ],
            "human_approval_points": [],
            "escalation_rules": [
                {
                    "condition": "Sumber tidak bisa diverifikasi, kontradiktif, atau keputusan berdampak besar pada bisnis",
                    "action": "Jelaskan ketidakpastian dan minta user menentukan apakah perlu riset lanjutan atau validasi manusia.",
                }
            ],
            "conversation_examples_needed": [
                "User kirim URL artikel lalu minta ringkasan poin penting",
                "User minta riset topik marketing dan rekomendasi tindakan",
                "User bertanya ulang tentang hasil riset yang pernah disimpan",
            ],
            "validation_checklist": [
                "Agent menyebutkan sumber atau keterbatasan sumber",
                "Agent menyimpan ringkasan dan preferensi riset ke memory",
                "Agent bisa menjawab follow-up memakai memory sebelumnya",
                "Agent tidak mengarang data saat sumber tidak tersedia",
            ],
            "missing_info_questions": [
                "Kalau user belum memberi topik/URL sama sekali, tanya topik riset yang ingin dibahas.",
            ],
        }

    return {
        "agent_summary": f"{name} untuk {user_goal}",
        "assumptions": ["Blueprint fallback dibuat karena output JSON generator tidak bisa dipulihkan."],
        "workflow_steps": [
            {
                "step": 1,
                "name": "Intake kebutuhan",
                "agent_action": "Pahami intent user, konteks bisnis, dan hasil akhir yang diinginkan sebelum menjalankan workflow.",
                "required_user_data": ["tujuan user", "konteks bisnis atau personal", "output yang diharapkan"],
                "success_criteria": "Agent memahami konteks inti dan tidak bertanya ulang untuk hal yang sudah tersedia.",
            }
        ],
        "knowledge_plan": {
            "must_have": ["Detail layanan/produk/SOP utama", "FAQ atau contoh kasus paling sering", "Batas wewenang agent"],
            "nice_to_have": ["Contoh percakapan nyata", "Kebijakan khusus", "Preferensi gaya komunikasi"],
            "needs_upload": bool(tools_config.get("rag")),
        },
        "tool_plan": tool_plan,
        "memory_plan": [{"key": "user_context", "value_to_store": "Kebutuhan, preferensi, dan konteks penting user"}],
        "state_plan": [
            {
                "state": "intake",
                "entry_condition": "Percakapan baru atau kebutuhan belum jelas",
                "allowed_actions": ["Kumpulkan data wajib", "Jawab pertanyaan dasar", "Gunakan konteks percakapan yang sudah ada"],
                "exit_condition": "Data inti cukup untuk melanjutkan workflow",
            }
        ],
        "human_approval_points": [
            {
                "when": "Kasus membutuhkan keputusan, akses, atau persetujuan manusia",
                "operator_action": "Review konteks dan beri keputusan eksplisit",
                "agent_next_action": "Lanjutkan workflow sesuai keputusan operator tanpa mengulang proses dari awal",
            }
        ],
        "escalation_rules": [{"condition": "Agent tidak yakin atau kasus sensitif", "action": "Eskalasi ke operator dengan ringkasan konteks"}],
        "conversation_examples_needed": ["Contoh tanya jawab untuk kasus paling umum"],
        "validation_checklist": ["Instructions mencerminkan workflow dan tidak generik", "Agent tahu kapan harus lanjut, berhenti, atau eskalasi"],
        "missing_info_questions": ["Detail apa yang paling wajib agent pahami jika konteks saat ini belum cukup?"],
    }


def _fallback_agent_instructions(
    *,
    preset_id: str,
    agent_name: str,
    business_context: str,
    persona: str,
    channel: str,
    escalation_info: str,
    extra_rules: str,
    agent_blueprint: str,
) -> str:
    """Deterministic instructions for critical workflows when the writer output is unusable."""
    context_text = _combined_context_text(
        preset_id,
        agent_name,
        business_context,
        escalation_info,
        extra_rules,
        agent_blueprint,
    )
    payment_context_text = _combined_context_text(
        preset_id,
        agent_name,
        business_context,
        escalation_info,
        extra_rules,
    )
    if preset_id == "approval_gated_service_agent" or _looks_like_payment_approval_workflow(payment_context_text):
        service = business_context.strip() or "layanan berbayar"
        file_delivery = _looks_like_file_delivery_workflow(context_text)
        generated_file = _looks_like_generated_file_workflow(context_text)
        file_delivery_rule = (
            "Untuk hasil berupa file/PDF/DOCX, buat file final melalui task ke subagent yang bisa menulis file di /workspace/output, lalu kirim langsung via send_whatsapp_document. "
            if generated_file
            else (
                "Untuk hasil berupa file yang sudah tersedia, kirim langsung via send_whatsapp_document. "
                if file_delivery
                else "Untuk hasil non-file, kirim ringkasan hasil final atau instruksi final langsung ke customer. "
            )
        )
        file_workspace_rule = (
            "Jika kamu sendiri mengirim file, panggil send_whatsapp_document hanya untuk file yang benar-benar bisa dibaca dari workspace aktif."
            if file_delivery else ""
        )
        file_tool_rule = (
            "Gunakan send_whatsapp_document untuk mengirim file final ke customer setelah approved jika workflow menghasilkan file."
            if file_delivery else
            "Jika workflow tidak menghasilkan file, jangan menyebut tool pengiriman file; kirim hasil final lewat pesan biasa."
        )
        return (
            f"Kamu adalah {agent_name}, asisten WhatsApp untuk {service}. "
            f"Gaya bicara kamu {persona}, singkat, jelas, dan natural. Jangan pakai markdown.\n\n"
            "TUGAS UTAMA\n"
            "Kamu membantu customer memesan layanan berbayar. Kamu mengumpulkan kebutuhan, "
            "meminta pembayaran, meneruskan bukti transfer ke admin, menunggu approval admin, lalu mengirim hasil final.\n\n"
            "STATE WAJIB\n"
            "1. intake: sambut customer, jelaskan proses singkat, tanya jenis layanan, tujuan, data wajib, dan referensi pendukung. Untuk jasa dokumen seperti CV ATS, tanya posisi target, nama, kontak, pengalaman, pendidikan, skill, proyek, sertifikasi, portfolio/LinkedIn, dan bahasa CV. Jika customer punya file lama atau dokumen referensi, minta dikirim agar pertanyaan berkurang.\n"
            "2. waiting_payment: setelah data cukup, minta customer transfer biaya jasa sesuai info bisnis, lalu kirim bukti transfer. Jangan fulfillment atau mengirim hasil final di state ini.\n"
            "3. payment_review: saat customer mengirim bukti transfer/gambar/dokumen pembayaran, panggil escalate_to_human(reason, summary) dengan ringkasan order dan bukti yang diterima. Setelah itu beri tahu customer bahwa pembayaran sedang dicek admin. Jangan lanjut delivery sebelum admin approve.\n"
            "4. approved: hanya setelah admin/operator menyetujui pembayaran, lanjutkan fulfillment layanan.\n"
            f"5. delivery: {file_delivery_rule}{file_workspace_rule}\n"
            "6. aftercare: setelah hasil terkirim, bantu revisi ringan sesuai kebijakan bisnis dan simpan catatan penting ke memory.\n\n"
            "ATURAN KERAS\n"
            "Jangan pernah mengirim atau menjanjikan hasil final sebelum payment approved. "
            "Jangan klaim hasil/file sudah dibuat jika tool, subagent, atau proses bisnis belum menghasilkan output nyata. "
            + ("Jangan klaim file sudah terkirim sebelum send_whatsapp_document sukses atau subagent melaporkan TERKIRIM. " if file_delivery else "")
            + "Jangan menyuruh user download manual jika WhatsApp media tersedia dan hasilnya bisa dikirim langsung. "
            "Kalau ada blocker teknis, jujur sebutkan blocker dan jangan mengarang path/link.\n\n"
            "TOOLS\n"
            "Gunakan remember untuk menyimpan nama, kebutuhan layanan, status order, data penting, dan keputusan admin. "
            "Gunakan recall sebelum menanyakan ulang data yang sudah ada. "
            "Gunakan escalate_to_human untuk bukti transfer, approval admin, komplain besar, atau keputusan pembayaran. "
            "Gunakan task hanya untuk pekerjaan yang memang perlu subagent seperti riset, penyusunan dokumen, analisis, atau pembuatan file final. "
            f"{file_tool_rule}\n\n"
            "CONTOH PERCAKAPAN\n"
            "User: Mau pesan layanan ini\n"
            f"{agent_name}: Bisa. Saya bantu dari pengumpulan kebutuhan sampai hasil final. Kebutuhan utamanya apa dulu?\n"
            "User: Ini bukti transfernya\n"
            f"{agent_name}: Terima kasih, saya teruskan dulu bukti transfernya ke admin untuk dicek. Hasil final baru saya kirim setelah pembayaran disetujui.\n"
            "Operator: approved\n"
            f"{agent_name}: Pembayaran sudah disetujui admin. Saya lanjut proses ordernya dan akan kirim hasilnya langsung ke sini setelah siap.\n\n"
            f"INFO ESKALASI\n{escalation_info or 'Eskalasi bukti transfer dan masalah pembayaran ke admin/operator yang terdaftar.'}\n\n"
            f"ATURAN TAMBAHAN\n{extra_rules or 'Ikuti state wajib dan jangan melewati approval pembayaran.'}"
        )

    skeleton = AGENT_PRESETS.get(preset_id, {}).get("instruction_skeleton", "")
    if skeleton:
        return (
            skeleton
            .replace("{name}", agent_name)
            .replace("{role}", "asisten")
            .replace("{business}", business_context or agent_name)
            .replace("{business_info}", business_context or "Info bisnis belum lengkap")
        )
    return (
        f"Kamu adalah {agent_name}, asisten yang membantu user sesuai konteks berikut: "
        f"{business_context or 'kebutuhan user belum detail'}. Jawab singkat, jujur, dan gunakan tool hanya saat diperlukan."
    )

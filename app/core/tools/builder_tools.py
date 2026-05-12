"""
builder_tools.py — Tools eksklusif untuk system agent (Agent Builder / Arthur).

Hanya dimuat jika agent memiliki capability 'builder' atau 'system'.

Tools yang di-expose:
  get_platform_capabilities()           — ringkasan kapabilitas platform
  get_presets()                         — katalog preset agent siap pakai
  plan_agent(...)                       — structured plan sebelum create
  verify_agent(agent_id)               — post-create readback + smoke test guidance
  list_available_wa_devices()           — WA devices yang belum di-assign ke agent
  validate_agent_config(...)            — validasi config sebelum create/update
  create_agent(...)                     — buat agent baru (di-scope ke owner_phone)
  update_agent(...)                     — update agent yang sudah ada
  get_agent_detail(agent_id)            — baca konfigurasi agent
  list_my_agents()                      — list agent milik owner_phone ini

Keamanan:
  - create_agent otomatis memasukkan owner_phone ke operator_ids → agen terisolasi per user
  - update_agent / get_agent_detail memverifikasi kepemilikan via operator_ids
  - list_my_agents hanya tampilkan agent yang memiliki owner_phone di operator_ids
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

import structlog
from langchain_core.tools import tool
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.agent import Agent

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Structured preset definitions — source of truth for agent types
# ---------------------------------------------------------------------------

AGENT_PRESETS: dict[str, dict] = {
    "coding_deploy_agent": {
        "label": "Coding & Deploy Agent",
        "description": "Agent yang bisa menulis kode, menjalankannya di sandbox Docker, dan men-deploy ke public URL via Cloudflare tunnel.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.5,
        "default_max_tokens": 2048,
        "default_channel": "webchat",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": True,
            "deploy": True,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": True},
        },
        "required_tools": ["sandbox", "deploy", "subagents"],
        "forbidden_tools": [],
        "channel_requirements": [],
        "runtime_limitations": [
            "deploy_requires_docker_socket",
            "cloudflare_tunnel_url_changes_on_redeploy",
            "deploy_ttl_4h_max",
            "no_persistent_storage_across_sessions",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, programmer full-stack yang mengeksekusi task coding dan deploy.\n\n"
            "CARA KERJA WAJIB untuk setiap task coding/web/deploy:\n"
            "1. Tulis semua file ke workspace (write_file) — jangan tanya konfirmasi dulu\n"
            "2. Cek status: panggil get_deployment_status()\n"
            "   - Jika 'running' → kembalikan URL yang ada, jangan deploy ulang\n"
            "   - Jika 'not_deployed' → lanjut ke langkah 3\n"
            "3. Deploy: panggil deploy_app(command, port)\n"
            "4. Verifikasi: panggil get_deployment_status() lagi — pastikan URL ada dan status 'running'\n"
            "   - Jika URL kosong atau error → panggil get_deployment_logs() → debug → perbaiki\n"
            "5. Jawab user dengan ramah dan asisten-like (seperti asisten manusia), tapi WAJIB sertakan URL hasil deploy.\n\n"
            "ATURAN KERAS:\n"
            "- Bersikaplah seperti AI Assistant yang ramah, gunakan bahasa yang natural.\n"
            "- JANGAN menggunakan format robotik/algoritma seperti 'STATUS: SUCCESS | DEPLOY_URL:'.\n"
            "- JANGAN tampilkan source code di jawaban akhir kecuali user eksplisit minta\n"
            "- JANGAN jelaskan cara kerja kode panjang lebar — langsung eksekusi\n"
            "- Task BELUM selesai sampai deploy_app() sukses dan URL dikonfirmasi\n"
            "- Untuk static website: deploy_app('python3 -m http.server 8080', 8080)\n"
            "- Untuk Flask/FastAPI: deploy_app('pip install flask && python app.py', 8080)\n"
            "- Untuk Node.js: deploy_app('npm install && node server.js', 3000)"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim pesan: 'buat halaman HTML hello world dan deploy'",
                "Cek agent merespons dengan ramah dan memberikan DEPLOY_URL yang valid",
                "Buka URL di browser — pastikan halaman tampil",
                "Kirim pesan edit: 'ganti tulisannya jadi Selamat Datang'",
                "Pastikan agent tidak deploy ulang, hanya update file",
            ],
            "expected_status": "Pesan ramah menyertakan URL trycloudflare.com",
            "known_failure_modes": [
                "Docker socket tidak tersedia → deploy_app() akan error",
                "Cloudflare rate limit → URL tidak muncul dalam 40s",
            ],
        },
    },
    "cs_whatsapp_basic": {
        "label": "CS WhatsApp Basic",
        "description": "Agent customer service untuk WhatsApp — jawab pertanyaan pelanggan, eskalasi ke operator jika perlu.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.7,
        "default_max_tokens": 800,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": False,
            "whatsapp_media": True,
            "subagents": {"enabled": False},
        },
        "required_tools": ["escalation"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": ["whatsapp_device_required"],
        "runtime_limitations": [
            "markdown_not_rendered_on_whatsapp",
            "no_broadcast_capability",
            "one_wa_number_per_agent",
            "wa_device_scan_required_before_use",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, {role} dari {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan pelanggan dengan ramah dan singkat\n"
            "- Catat nama dan kebutuhan user ke memory saat pertama kali ngobrol\n"
            "- Eskalasikan ke operator jika tidak bisa bantu atau ada komplain serius\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, santai tapi sopan\n"
            "Panjang pesan: singkat, 1-3 kalimat\n"
            "JANGAN pakai simbol *, #, atau format markdown apapun\n\n"
            "ESKALASI KE OPERATOR:\n"
            "Eskalasikan jika: komplain serius, pertanyaan tidak bisa dijawab, minta refund\n"
            "Cara WAJIB: panggil tool escalate_to_human(reason, summary) terlebih dahulu — baru balas user\n"
            "JANGAN bilang 'diteruskan ke tim' tanpa memanggil tool escalate_to_human\n\n"
            "INFORMASI BISNIS:\n"
            "{business_info}\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Halo, saya mau tanya soal produk\n"
            "{name}: Halo! Tentu, saya siap bantu. Produk apa yang ingin ditanyakan?"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Scan QR di wa-dev-service / wa-service untuk hubungkan nomor WA",
                "Kirim pesan: 'Halo, apakah toko buka hari ini?'",
                "Pastikan agent merespons dalam bahasa Indonesia tanpa markdown",
                "Kirim: 'Saya mau komplain pesanan saya rusak'",
                "Pastikan agent memanggil escalate_to_human sebelum merespons",
            ],
            "expected_status": "Respons singkat tanpa markdown, eskalasi berjalan saat diminta",
            "known_failure_modes": [
                "WA device belum di-connect → pesan tidak terkirim",
                "Escalation_config kosong → operator tidak dapat notifikasi",
            ],
        },
    },
    "faq_webchat_rag": {
        "label": "FAQ & RAG Webchat Agent",
        "description": "Agent yang menjawab pertanyaan berdasarkan dokumen yang diupload (PDF, DOCX). Cocok untuk FAQ produk, kebijakan, manual.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.3,
        "default_max_tokens": 1024,
        "default_channel": "webchat",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["rag"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": ["documents_must_be_uploaded_via_api"],
        "runtime_limitations": [
            "rag_requires_documents_uploaded_first",
            "rag_uses_vector_similarity_not_full_text",
            "max_doc_size_depends_on_embedding_service",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten FAQ dari {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan user berdasarkan dokumen yang tersedia\n"
            "- Gunakan tool search_documents untuk mencari jawaban dari dokumen\n"
            "- Jika tidak ada informasi di dokumen, katakan terus terang dan tawarkan eskalasi\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia\n"
            "Panjang pesan: 1-3 kalimat, langsung ke inti\n"
            "JANGAN mengada-ada jawaban yang tidak ada di dokumen\n\n"
            "ESKALASI:\n"
            "Jika pertanyaan tidak ada di dokumen atau butuh keputusan manusia:\n"
            "Panggil escalate_to_human(reason, summary) lalu beritahu user\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Berapa biaya pengiriman ke Bandung?\n"
            "{name}: Berdasarkan informasi yang ada, biaya pengiriman ke Bandung adalah Rp 15.000 untuk berat di bawah 1 kg."
        ),
        "smoke_test": {
            "strategy": "manual_with_docs",
            "steps": [
                "Upload minimal satu dokumen via POST /v1/agents/{id}/documents/upload",
                "Kirim pertanyaan yang jawabannya ada di dokumen",
                "Pastikan agent merespons dengan informasi yang relevan dari dokumen",
                "Kirim pertanyaan yang jawabannya TIDAK ada",
                "Pastikan agent tidak mengada-ada dan menawarkan eskalasi",
            ],
            "expected_status": "Jawaban akurat dari dokumen; jujur saat tidak tahu",
            "known_failure_modes": [
                "Tidak ada dokumen diupload → search_documents tidak menemukan apapun",
                "Dokumen format tidak didukung → embedding gagal",
            ],
        },
    },
    "scheduler_assistant": {
        "label": "Scheduler & Reminder Assistant",
        "description": "Asisten pribadi yang bisa set reminder, jadwal, dan pengingat otomatis.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.7,
        "default_max_tokens": 512,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": True,
            "rag": False,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["scheduler"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "scheduler_requires_apscheduler_running",
            "reminders_lost_on_server_restart_without_db_backed_scheduler",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten jadwal dan pengingat pribadi.\n\n"
            "TUGASMU:\n"
            "- Set reminder dan pengingat sesuai permintaan user\n"
            "- Catat jadwal penting ke memory\n"
            "- Ingatkan user saat waktunya tiba\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, santai\n"
            "Konfirmasi selalu setelah set reminder: waktu, pesan, dan timezone\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Ingatkan saya rapat jam 3 sore besok\n"
            "{name}: Oke, saya set reminder untuk rapat besok jam 15:00. Mau saya tambahkan catatan apapun?"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim: 'Ingatkan saya minum obat 5 menit dari sekarang'",
                "Pastikan agent memanggil set_reminder dan konfirmasi waktu",
                "Tunggu 5 menit — pastikan reminder terkirim",
                "Kirim: 'batalkan reminder tadi'",
                "Pastikan agent memanggil cancel_reminder",
            ],
            "expected_status": "Reminder terset dan terkirim tepat waktu",
            "known_failure_modes": [
                "APScheduler tidak running → reminder tidak terkirim",
                "Timezone mismatch → reminder tiba di waktu salah",
            ],
        },
    },
    "social_media_agent": {
        "label": "Social Media Specialist Agent",
        "description": "Agent spesialis konten media sosial — riset tren, buat content planner, generate file PDF/Excel, dan kirim langsung ke WhatsApp.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.7,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": True,
            "deploy": False,
            "tool_creator": False,
            "scheduler": True,
            "rag": False,
            "http": True,
            "whatsapp_media": True,
            "subagents": {"enabled": True},
        },
        "required_tools": ["sandbox", "subagents", "whatsapp_media", "http"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "no_direct_social_media_posting",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, {role} yang membantu {business} dengan strategi dan konten media sosial.\n\n"
            "KEMAMPUAN UTAMA:\n"
            "- Riset tren media sosial dan topik relevan industri via HTTP\n"
            "- Buat content planner mingguan/bulanan\n"
            "- Generate file PDF atau Excel dengan sys_coder dan kirim langsung ke user\n"
            "- Buat draft caption, hashtag, dan ide visual konten\n\n"
            "CARA GENERATE DAN KIRIM FILE (WAJIB IKUTI):\n"
            "Saat user minta file (PDF, Excel, gambar):\n"
            "1. Riset dulu jika perlu (http_get)\n"
            "2. Delegate ke sys_coder: task('sys_coder', task='Buat file [format] berisi [konten]. "
            "Simpan ke /workspace/output/[filename]. "
            "Kirim ke user via send_whatsapp_document(\"/workspace/output/[filename]\", filename=\"[filename]\", caption=\"[caption]\"). "
            "Konfirmasi setelah terkirim.')\n"
            "3. Relay hasil ke user — jangan bilang 'file perlu didownload manual'\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, energik dan kreatif\n"
            "JANGAN pakai markdown saat di WhatsApp\n\n"
            "INFORMASI BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Kirim: 'Buat content planner 1 minggu dalam bentuk PDF'",
                "Pastikan agent riset dulu lalu delegate ke sys_coder",
                "Pastikan file PDF dikirim langsung via WhatsApp (bukan 'didownload manual')",
            ],
            "expected_status": "File PDF dikirim ke WhatsApp user",
            "known_failure_modes": [
                "whatsapp_media: false → send_whatsapp_document tidak tersedia",
                "subagents: false → sys_coder tidak bisa dipanggil",
            ],
        },
    },
    "data_analyst_agent": {
        "label": "Data Analyst Agent",
        "description": "Agent analisis data — upload file Excel/CSV, dapatkan insight, grafik, dan laporan langsung di WhatsApp atau webchat.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.3,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": True,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": False,
            "whatsapp_media": True,
            "subagents": {"enabled": True},
        },
        "required_tools": ["sandbox", "subagents", "whatsapp_media"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "no_persistent_storage_across_sessions",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, analis data untuk {business}.\n\n"
            "TUGASMU:\n"
            "- Terima file data (Excel, CSV) dari user\n"
            "- Analisis data: statistik, tren, anomali\n"
            "- Generate grafik dan laporan, kirim hasilnya ke user\n\n"
            "CARA ANALISIS DAN KIRIM HASIL (WAJIB):\n"
            "Saat user kirim data atau minta analisis:\n"
            "1. Terima file dan simpan di workspace\n"
            "2. Delegate ke sys_coder: task('sys_coder', task='Analisis file [nama] di /workspace/. "
            "Buat grafik dan laporan ringkas. Simpan ke /workspace/output/. "
            "Kirim via send_whatsapp_document atau send_whatsapp_image ke user.')\n"
            "3. Relay insight ke user dalam bahasa sederhana\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, jelas dan berbasis data\n"
            "Selalu sertakan angka dan fakta dalam jawaban\n\n"
            "KONTEKS BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim file CSV sederhana",
                "Minta: 'Analisis data ini dan buat grafiknya'",
                "Pastikan agent delegate ke sys_coder dan kirim hasil grafik",
            ],
            "expected_status": "Grafik atau laporan terkirim",
            "known_failure_modes": [
                "File biner rusak → pandas gagal baca",
            ],
        },
    },
    "research_agent": {
        "label": "Research & Intelligence Agent",
        "description": "Agent riset mendalam — browsing internet, kumpulkan data, susun laporan terstruktur.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.4,
        "default_max_tokens": 2048,
        "default_channel": "webchat",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": True,
            "whatsapp_media": False,
            "subagents": {"enabled": True},
        },
        "required_tools": ["http", "subagents"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "http_depends_on_target_api_availability",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, agen riset dan intelijen untuk {business}.\n\n"
            "TUGASMU:\n"
            "- Lakukan riset mendalam dari berbagai sumber online\n"
            "- Kumpulkan, verifikasi, dan sintesis informasi\n"
            "- Sajikan laporan yang terstruktur dan actionable\n\n"
            "CARA RISET:\n"
            "- Gunakan http_get untuk akses URL, API, dan sumber data\n"
            "- Untuk riset paralel yang kompleks, delegate ke sys_researcher via task()\n"
            "- Selalu cite sumber informasi\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia (atau sesuai bahasa user)\n"
            "Format output: terstruktur dengan poin-poin jelas\n"
            "Jujur jika informasi tidak tersedia atau tidak bisa diverifikasi\n\n"
            "KONTEKS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Minta: 'Riset 3 kompetitor terbesar di industri [X]'",
                "Pastikan agent menggunakan http_get atau delegate ke sys_researcher",
                "Pastikan output terstruktur dengan sumber",
            ],
            "expected_status": "Laporan riset terstruktur dengan sumber",
            "known_failure_modes": [
                "Target URL blokir scraping → hasil kosong",
            ],
        },
    },
    "ecommerce_cs": {
        "label": "E-Commerce Customer Service",
        "description": "CS agent khusus e-commerce — handle pertanyaan produk, status pesanan, komplain, dan retur via WhatsApp.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.7,
        "default_max_tokens": 800,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": True,
            "whatsapp_media": True,
            "subagents": {"enabled": False},
        },
        "required_tools": ["escalation", "whatsapp_media"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": ["whatsapp_device_required"],
        "runtime_limitations": [
            "markdown_not_rendered_on_whatsapp",
            "one_wa_number_per_agent",
            "wa_device_scan_required_before_use",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, CS online shop {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan produk: stok, harga, spesifikasi\n"
            "- Bantu cek status pesanan (jika ada API order)\n"
            "- Handle komplain dengan empati\n"
            "- Proses permintaan retur/refund sesuai policy\n"
            "- Eskalasi ke operator untuk kasus kompleks\n\n"
            "POLICY TOKO:\n"
            "{business_info}\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, ramah, sabar\n"
            "Singkat — maks 3 kalimat per pesan\n"
            "JANGAN pakai *, #, atau markdown\n"
            "Panggil customer dengan nama jika sudah tahu\n\n"
            "ESKALASI:\n"
            "Wajib eskalasi untuk: komplain keras, refund > Rp 500rb, ancaman hukum\n"
            "Cara: panggil escalate_to_human(reason, summary) dulu BARU balas user"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Kirim: 'Halo, produk X masih ada?'",
                "Pastikan agent jawab sesuai info bisnis",
                "Kirim: 'Pesanan saya belum sampai sudah 2 minggu, mau komplain!'",
                "Pastikan agent eskalasi ke operator",
            ],
            "expected_status": "Jawaban sesuai policy, eskalasi berjalan",
            "known_failure_modes": [
                "escalation_config kosong → operator tidak notif",
            ],
        },
    },
    "personal_assistant": {
        "label": "Personal Assistant Agent",
        "description": "Asisten pribadi all-in-one — jadwal, reminder, riset cepat, dan pengingat via WhatsApp.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.7,
        "default_max_tokens": 1024,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": True,
            "rag": False,
            "http": True,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["scheduler", "memory"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "scheduler_requires_apscheduler_running",
            "markdown_not_rendered_on_whatsapp",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten pribadi {business}.\n\n"
            "TUGASMU:\n"
            "- Kelola jadwal dan pengingat\n"
            "- Catat hal penting ke memory\n"
            "- Bantu riset cepat via internet jika diminta\n"
            "- Ingatkan deadline, meeting, tugas penting\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, santai seperti asisten pribadi\n"
            "Proaktif — jika user bilang ada meeting besok, tawarkan set reminder\n"
            "JANGAN pakai markdown\n\n"
            "CONTOH:\n"
            "User: Ada rapat investor Jumat jam 10\n"
            "{name}: Oke, saya catat rapat investor Jumat jam 10. Mau saya ingatkan H-1 atau pagi harinya?"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim: 'Ingatkan saya beli kado ulang tahun istri 3 hari lagi'",
                "Pastikan agent set reminder dan konfirmasi",
                "Kirim: 'Apa jadwal saya minggu ini?'",
                "Pastikan agent recall dari memory",
            ],
            "expected_status": "Reminder terset, memory terisi",
            "known_failure_modes": [
                "APScheduler tidak running → reminder tidak terkirim",
            ],
        },
    },
    "hr_assistant": {
        "label": "HR & Internal Knowledge Assistant",
        "description": "Asisten HR internal — jawab pertanyaan kebijakan perusahaan, cuti, benefit, dan onboarding dari dokumen.",
        "default_model": "openai/gpt-5.1",
        "default_temperature": 0.3,
        "default_max_tokens": 1024,
        "default_channel": "webchat",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["rag", "escalation"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": ["documents_must_be_uploaded_via_api"],
        "runtime_limitations": [
            "rag_requires_documents_uploaded_first",
            "rag_uses_vector_similarity_not_full_text",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten HR digital untuk {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan karyawan tentang kebijakan perusahaan\n"
            "- Informasi cuti, benefit, prosedur HR dari dokumen\n"
            "- Bantu onboarding karyawan baru\n"
            "- Eskalasi ke HR manusia untuk kasus sensitif\n\n"
            "CARA MENJAWAB:\n"
            "- Selalu cari dulu di dokumen via search_documents\n"
            "- Jika tidak ada di dokumen, jujur dan tawarkan eskalasi\n"
            "- JANGAN mengarang kebijakan yang tidak ada di dokumen\n\n"
            "CARA BICARA:\n"
            "Bahasa: Indonesia, profesional tapi ramah\n"
            "Sertakan referensi dokumen saat menjawab\n\n"
            "ESKALASI:\n"
            "Untuk: terminasi, konflik antar karyawan, masalah payroll → eskalasi ke HR"
        ),
        "smoke_test": {
            "strategy": "manual_with_docs",
            "steps": [
                "Upload policy dokumen (PDF/DOCX)",
                "Tanya: 'Berapa hari jatah cuti tahunan?'",
                "Pastikan agent menjawab berdasarkan dokumen",
                "Tanya sesuatu yang tidak ada di dokumen",
                "Pastikan agent jujur dan tawarkan eskalasi",
            ],
            "expected_status": "Jawaban akurat dari dokumen, jujur saat tidak tahu",
            "known_failure_modes": [
                "Dokumen belum diupload → jawaban tidak akurat",
            ],
        },
    },
}

# ---------------------------------------------------------------------------
# Known runtime limitations — machine-readable
# ---------------------------------------------------------------------------

RUNTIME_LIMITATIONS: dict[str, dict] = {
    "deploy_requires_docker_socket": {
        "severity": "critical",
        "affects": ["coding_deploy_agent"],
        "description": "deploy_app() membutuhkan Docker socket tersedia di server. Tanpa ini, deploy akan gagal.",
        "mitigation": "Pastikan platform berjalan dengan Docker socket di-mount (/var/run/docker.sock).",
        "user_message": "Deploy ke public URL membutuhkan Docker — pastikan environment sudah dikonfigurasi.",
    },
    "cloudflare_tunnel_url_changes_on_redeploy": {
        "severity": "warning",
        "affects": ["coding_deploy_agent"],
        "description": "Setiap kali deploy_app() dipanggil, URL Cloudflare Quick Tunnel akan berubah.",
        "mitigation": "Instruksikan agent untuk cek get_deployment_status() sebelum deploy ulang.",
        "user_message": "URL akan berubah setiap kali deploy ulang — tidak bisa pakai URL permanen dengan Cloudflare Quick Tunnel.",
    },
    "deploy_ttl_4h_max": {
        "severity": "info",
        "affects": ["coding_deploy_agent"],
        "description": "Deployment otomatis dihapus setelah 4 jam (configurable via DEPLOYMENT_TTL_SECONDS).",
        "mitigation": "Gunakan untuk demo/testing, bukan production long-running apps.",
        "user_message": "App yang di-deploy otomatis berhenti setelah ~4 jam.",
    },
    "no_persistent_storage_across_sessions": {
        "severity": "info",
        "affects": ["coding_deploy_agent"],
        "description": "Workspace file persisten dalam satu sesi, tapi berbeda sesi = workspace baru.",
        "mitigation": "Sampaikan ke user bahwa file tidak permanen antar sesi.",
        "user_message": "File di workspace tidak permanen — hilang saat sesi baru dimulai.",
    },
    "markdown_not_rendered_on_whatsapp": {
        "severity": "warning",
        "affects": ["cs_whatsapp_basic", "scheduler_assistant"],
        "description": "WhatsApp tidak merender markdown standar (**, #, `) — tampil sebagai karakter literal.",
        "mitigation": "Instruksikan agent untuk tidak menggunakan markdown di instructions.",
        "user_message": "Simbol markdown seperti * dan # tidak akan terformat di WhatsApp.",
    },
    "no_broadcast_capability": {
        "severity": "info",
        "affects": ["cs_whatsapp_basic"],
        "description": "Platform tidak mendukung broadcast ke banyak nomor sekaligus.",
        "mitigation": "Gunakan untuk 1-on-1 conversation saja.",
        "user_message": "Agent tidak bisa kirim pesan ke banyak nomor sekaligus.",
    },
    "one_wa_number_per_agent": {
        "severity": "info",
        "affects": ["cs_whatsapp_basic", "scheduler_assistant"],
        "description": "Satu nomor WhatsApp hanya bisa dipakai oleh satu agent.",
        "mitigation": "Gunakan device/nomor yang berbeda untuk setiap agent WA.",
        "user_message": "Satu nomor WA hanya bisa digunakan oleh satu agent.",
    },
    "wa_device_scan_required_before_use": {
        "severity": "info",
        "affects": ["cs_whatsapp_basic", "scheduler_assistant"],
        "description": "User harus scan QR untuk menghubungkan nomor WA ke agent sebelum bisa dipakai.",
        "mitigation": "Gunakan send_agent_wa_qr() setelah create untuk kirim QR ke user.",
        "user_message": "Perlu scan QR WhatsApp sebelum agent bisa digunakan.",
    },
    "rag_requires_documents_uploaded_first": {
        "severity": "warning",
        "affects": ["faq_webchat_rag"],
        "description": "RAG search tidak akan menemukan apapun jika tidak ada dokumen yang diupload dulu.",
        "mitigation": "Setelah create, minta user upload dokumen via /v1/agents/{id}/documents/upload.",
        "user_message": "Dokumen harus diupload terlebih dahulu sebelum agent bisa menjawab dari RAG.",
    },
    "rag_uses_vector_similarity_not_full_text": {
        "severity": "info",
        "affects": ["faq_webchat_rag"],
        "description": "RAG menggunakan sentence embeddings — pertanyaan yang sangat berbeda phrasing dari dokumen mungkin tidak ditemukan.",
        "mitigation": "Instruksikan user untuk upload dokumen dengan bahasa yang mirip pertanyaan yang akan ditanyakan.",
        "user_message": "RAG mencari berdasarkan makna, bukan kata kunci persis — hasil bisa bervariasi.",
    },
    "scheduler_requires_apscheduler_running": {
        "severity": "critical",
        "affects": ["scheduler_assistant"],
        "description": "Reminder hanya berjalan jika APScheduler aktif di backend — tidak ada jika API di-restart tanpa scheduler init.",
        "mitigation": "Pastikan backend sudah running dan scheduler service aktif.",
        "user_message": "Reminder membutuhkan server berjalan — tidak akan terkirim jika server mati.",
    },
}

_TOOLS_CONFIG_DOCS = {
    "memory": "Ingat fakta tentang user lintas sesi (remember/recall/forget). Default ON.",
    "skills": "Library skill/template prompt yang bisa dipakai ulang. Default ON.",
    "escalation": "Eskalasi ke operator manusia. Default ON. Wajib untuk agent WhatsApp.",
    "sandbox": "Eksekusi kode Python di Docker, workspace file persisten per sesi. Default OFF.",
    "deploy": "Deploy app dari sandbox ke public URL via Cloudflare tunnel — otomatis aktif jika sandbox: true. Agent bisa kasih link yang bisa diakses siapapun.",
    "tool_creator": "Buat Python tool baru secara dinamis (butuh sandbox aktif). Default OFF.",
    "scheduler": "Set reminder, cron job, tugas terjadwal. Default OFF.",
    "rag": "Cari jawaban dari dokumen yang diupload. Default OFF.",
    "http": "HTTP GET/POST/PATCH/DELETE ke API eksternal. Default OFF.",
    "mcp": "Koneksi ke MCP server eksternal (Notion, Google Calendar, dll). Default OFF.",
    "whatsapp_media": "Kirim gambar dan dokumen via WhatsApp. Default OFF. Aktifkan untuk agent WA.",
    "wa_agent_manager": "Kelola WA device/QR agent lain. Default OFF. Khusus meta-agent.",
    "subagents": (
        "Delegasi ke sub-agent spesialis via tool task(). "
        "WAJIB aktif (subagents: {enabled: true}) untuk semua coding/deploy agent — "
        "sys_coder menangani eksekusi sandbox dan deploy ke public URL, main agent jadi orchestrator. "
        "Sub-agent yang tersedia: "
        "sys_coder (programmer full-stack: tulis kode Python/JS/HTML, jalankan di sandbox Docker, deploy website ke Cloudflare public URL — kembalikan URL ke user), "
        "sys_researcher (riset internet via HTTP), "
        "sys_writer (tulis/edit konten), "
        "sys_analyst (analisis data dengan pandas/numpy). "
        "Jika agent HANYA butuh coding/deploy: aktifkan sandbox+deploy+subagents. "
        "Jika butuh coding PLUS riset/analisis/tulis: sama, subagents sudah include semua."
    ),
}

_PLATFORM_CHANNELS = [
    {"type": "whatsapp", "description": "WhatsApp Business via wa-service. Butuh device/QR."},
    {"type": "webchat", "description": "Embed di website. Tidak butuh nomor WA."},
    {"type": "telegram", "description": "Telegram bot. Butuh bot_token."},
    {"type": "slack", "description": "Slack incoming webhook."},
    {"type": "in-app", "description": "Pesan tersimpan di DB, tidak dikirim ke channel. Untuk integrasi custom."},
]

_RECOMMENDED_MODELS = [
    {"model": "openai/gpt-5.1", "use_case": "Default terbaik — reasoning kuat, tool use akurat"},
    {"model": "openai/gpt-4.1", "use_case": "Balance cost & quality (generasi sebelumnya)"},
    {"model": "openai/gpt-4.1-mini", "use_case": "Budget / volume tinggi"},
    {"model": "openai/gpt-4.1-nano", "use_case": "Ultra-fast response"},
    {"model": "anthropic/claude-sonnet-4-6", "use_case": "Reasoning kompleks, nuanced"},
    {"model": "openai/gpt-4o", "use_case": "Analisis gambar/dokumen (vision)"},
]

_DEFAULT_MODEL = "openai/gpt-5.1"


# ---------------------------------------------------------------------------
# Helper functions for preset detection and post-create step generation
# ---------------------------------------------------------------------------

def _detect_preset(goal_lower: str, features: list[str], channel: str) -> str:
    """Map user goal + features + channel to the best matching preset ID."""
    coding_keywords = {"coding", "kode", "code", "programmer", "programming", "deploy",
                       "website", "web", "app", "aplikasi", "landing page", "html", "python",
                       "javascript", "flask", "fastapi", "node"}
    cs_keywords = {"cs", "customer service", "pelanggan", "toko", "jawab pertanyaan",
                   "customer", "support", "layanan", "klien"}
    faq_keywords = {"faq", "dokumen", "rag", "knowledge base", "pertanyaan umum",
                    "manual", "kebijakan", "katalog", "produk info"}
    scheduler_keywords = {"reminder", "jadwal", "pengingat", "schedule", "alarm",
                          "kalkulator", "timer", "tanggal", "waktu"}
    social_media_keywords = {"sosmed", "social media", "konten", "content", "instagram", "tiktok",
                              "facebook", "linkedin", "posting", "caption", "content planner",
                              "jadwal konten", "copywriting", "copywriter", "content creator",
                              "social media specialist", "content calendar", "engagement"}
    data_analyst_keywords = {"data", "analisis", "analyst", "analitik", "laporan", "report",
                              "dashboard", "grafik", "chart", "excel", "csv", "statistik",
                              "visualisasi", "insight", "metrics", "kpi", "pandas", "numpy"}
    research_keywords = {"riset", "research", "penelitian", "cari informasi", "kompetitor",
                          "market research", "trend", "analisis pasar", "survei", "literatur",
                          "referensi", "ringkasan artikel", "summarize", "web search"}
    ecommerce_keywords = {"ecommerce", "e-commerce", "marketplace", "toko online", "jualan",
                           "pesanan", "order", "checkout", "produk", "katalog online",
                           "shopee", "tokopedia", "lazada", "stok", "inventory", "harga"}
    personal_assistant_keywords = {"asisten pribadi", "personal assistant", "pa", "sekretaris",
                                    "to-do", "todo", "task", "agenda", "manajemen waktu",
                                    "time management", "kalender", "email", "meeting"}
    hr_keywords = {"hr", "hrd", "rekrutmen", "recruitment", "karyawan", "onboarding",
                   "sdm", "human resource", "interview", "cv", "resume", "absensi",
                   "cuti", "gaji", "payroll", "training", "performa"}

    goal_words = set(goal_lower.split())
    # Also check substrings for multi-word keywords
    def has_keyword(kw_set: set) -> bool:
        for kw in kw_set:
            if kw in goal_lower or kw in features:
                return True
        return bool(goal_words & kw_set)

    if has_keyword(coding_keywords):
        return "coding_deploy_agent"

    if has_keyword(social_media_keywords):
        return "social_media_agent"

    if has_keyword(data_analyst_keywords):
        return "data_analyst_agent"

    if has_keyword(research_keywords):
        return "research_agent"

    if has_keyword(hr_keywords):
        return "hr_assistant"

    if has_keyword(ecommerce_keywords):
        return "ecommerce_cs"

    if has_keyword(personal_assistant_keywords):
        return "personal_assistant"

    if channel == "whatsapp" and has_keyword(cs_keywords):
        return "cs_whatsapp_basic"

    if has_keyword(faq_keywords):
        return "faq_webchat_rag"

    if has_keyword(scheduler_keywords):
        return "scheduler_assistant"

    # Default: if channel is whatsapp, use cs; otherwise general (faq_webchat_rag as fallback)
    if channel == "whatsapp":
        return "cs_whatsapp_basic"

    return "faq_webchat_rag"


def _detect_preset_from_config(tc: dict, channel_type: str) -> str:
    """Reverse-detect preset from an existing tools_config."""
    if tc.get("sandbox") or tc.get("deploy"):
        # Could be coding or social_media/data — can't distinguish without goal, use coding
        return "coding_deploy_agent"
    if tc.get("subagents") and tc.get("whatsapp_media"):
        return "social_media_agent"
    if tc.get("subagents") and not tc.get("whatsapp_media"):
        return "data_analyst_agent"
    if tc.get("rag") and tc.get("escalation"):
        return "hr_assistant"
    if tc.get("rag"):
        return "faq_webchat_rag"
    if tc.get("scheduler"):
        return "scheduler_assistant"
    if channel_type == "whatsapp" or tc.get("whatsapp_media") or tc.get("escalation"):
        return "cs_whatsapp_basic"
    return "cs_whatsapp_basic"


def _get_post_create_steps(preset_id: str, channel: str, tc: dict) -> list[str]:
    """Return required actions user/operator must take after agent creation."""
    steps = []
    if channel == "whatsapp" or tc.get("whatsapp_media"):
        steps.append("Hubungkan WhatsApp: http_post ke /v1/agents/{id}/whatsapp/connect")
        steps.append("Kirim QR ke user: gunakan send_agent_wa_qr(agent_id, caption, phone)")
        steps.append("Tunggu status 'connected': http_get ke /v1/agents/{id}/whatsapp/status")
    if tc.get("rag"):
        steps.append("Upload dokumen: POST /v1/agents/{id}/documents/upload (PDF/DOCX/TXT)")
    if preset_id == "coding_deploy_agent":
        steps.append("Pastikan Docker socket tersedia di server sebelum test deploy")
    return steps


_INSTRUCTION_WRITER_MODEL = "deepseek/deepseek-r1"
# Soul writing is structured text — doesn't need heavy reasoning, use fast model
_SOUL_WRITER_MODEL = "openai/gpt-4o-mini"

_SOUL_TEMPLATES: dict[str, str] = {
    "cs_whatsapp_basic": """\
IDENTITAS
Nama: {name}
Peran: {role} dari {business}

KEPRIBADIAN
{persona}. Bahasa Indonesia, santai tapi sopan. Gunakan sapaan yang hangat. Pesan maks 2-3 kalimat — singkat dan to the point. JANGAN pakai markdown (*, #, **).

TUGAS UTAMA
{tasks}

INFO BISNIS
{business_info}

ESKALASI
{escalation}
Cara eskalasi WAJIB: panggil tool escalate_to_human(reason, summary) DULU — baru balas user.
Sebelum eskalasi: catat nama user dan masalah ke memory.

MEMORY
Saat pertama kali ngobrol dengan user baru: catat namanya dan kebutuhannya ke memory.

LARANGAN
- Jangan pakai simbol markdown apapun
- Jangan beri janji yang tidak bisa dipenuhi
- Jangan bahas hal di luar {business}\
""",
    "coding_deploy_agent": """\
IDENTITAS
Nama: {name}
Peran: Orchestrator coding dan web deployment. Terima request dari user, delegasikan eksekusi ke sys_coder, sampaikan hasilnya.

KEPRIBADIAN
{persona}. Langsung eksekusi — tidak perlu tanya konfirmasi dulu. Jawab singkat dan berikan hasilnya.

CARA KERJA WAJIB untuk setiap task coding/web:
Delegasikan semua task coding dan deploy ke sys_coder via tool task().
Contoh: task(name="sys_coder", task="Buat landing page HTML dengan judul 'Halo Dunia', deploy, kembalikan URL")

sys_coder menangani:
- Menulis semua file kode ke workspace
- Mengecek dan menjalankan deployment
- Mendapatkan URL publik yang bisa diakses

Kamu (main agent) menangani:
- Menerima dan memahami request user
- Mendelegasikan ke sys_coder dengan instruksi yang jelas
- Menyampaikan hasil (URL atau error) ke user dengan ramah

ATURAN KERAS
- JANGAN coba eksekusi kode sendiri — delegasikan ke sys_coder
- Jangan tampilkan source code di jawaban akhir kecuali user eksplisit minta
- Task BELUM selesai sampai sys_coder konfirmasi URL
- Jika sys_coder gagal, relay BLOCKER ke user dan minta mereka coba lagi

JANGAN VERIFIKASI HASIL SUB-AGENT PAKAI TOOL SENDIRI
- Workspace dan deployment kamu TERPISAH dari sys_coder. Sandbox-mu kosong by design.
- JANGAN panggil get_deployment_status(), ls(), glob(), atau read_file() untuk "ngecek" hasil sys_coder.
  Tool itu cuma melihat session-mu sendiri — pasti kosong walaupun sub-agent sukses.
- Output dari task() ADALAH ground truth. Kalau sub-agent return string yang berisi URL → URL itu valid, langsung relay ke user.
- Kalau task() return tanpa URL atau error → BARU bilang gagal. Jangan double-check sendiri.

INGAT HASIL DEPLOY — JANGAN BIKIN ULANG
- Setiap kali sys_coder return URL, LANGSUNG simpan ke memory:
  remember(key="last_deploy_url", value="<url>")
  remember(key="last_deploy_summary", value="<deskripsi singkat web yang dibuat>")
- Sebelum delegasi ulang ke sys_coder, WAJIB recall("last_deploy_url") dulu.
- Kalau user nanya status ("udah jadi?", "mana webnya?", "URL-nya apa?") → JANGAN delegasi ulang.
  Cukup recall("last_deploy_url") dan kirim URL-nya ke user.
- Hanya delegasi ulang kalau user EKSPLISIT minta:
  (a) perubahan/edit konten ("ganti warna jadi biru", "tambahin section X")
  (b) bikin web baru yang beda total ("buatin landing page lain")
  (c) deployment lama gak bisa diakses dan user minta deploy ulang
- Kalau user minta edit, suruh sys_coder MODIFY file yang ada (bukan rebuild from scratch) dan re-deploy.

{extra_rules}\
""",
    "faq_webchat_rag": """\
IDENTITAS
Nama: {name}
Peran: Asisten FAQ dari {business}

KEPRIBADIAN
{persona}. Bahasa Indonesia, informatif dan ringkas. Jawab berdasarkan dokumen — jangan karang sendiri.

TUGAS UTAMA
- Jawab pertanyaan user menggunakan tool search_documents untuk mencari di dokumen
- Jika informasi tidak ada di dokumen: akui terus terang dan tawarkan eskalasi
- Jangan mengada-ada atau menebak jawaban

INFO BISNIS
{business_info}

ESKALASI
{escalation}
Cara eskalasi: panggil escalate_to_human(reason, summary) lalu beritahu user.

LARANGAN
- Jangan jawab di luar scope dokumen
- Jangan buat informasi yang tidak ada di dokumen\
""",
    "scheduler_assistant": """\
IDENTITAS
Nama: {name}
Peran: Asisten jadwal dan pengingat pribadi

KEPRIBADIAN
{persona}. Bahasa Indonesia, santai. Selalu konfirmasi ulang detail reminder sebelum set.

TUGAS UTAMA
- Set reminder dan pengingat sesuai permintaan user
- Catat jadwal penting ke memory
- Ingatkan user saat waktunya tiba dengan pesan yang relevan

CARA KERJA
Setelah set reminder: konfirmasi waktu, pesan, dan timezone ke user.
Sebelum set: pastikan waktu sudah jelas (tanggal, jam, timezone jika disebutkan).

LARANGAN
- Jangan set reminder tanpa konfirmasi waktu yang jelas
- Jangan lupa konfirmasi setelah berhasil set\
""",
}


async def _call_instruction_writer(prompt: str, system: str, model: str | None = None) -> str:
    """Call LLM via OpenRouter for instruction/soul writing."""
    settings = get_settings()
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    response = await client.chat.completions.create(
        model=model or _INSTRUCTION_WRITER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=1500,
        temperature=0.5,
    )
    content = response.choices[0].message.content or ""
    # Strip reasoning/thinking tags
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content


def build_builder_tools(
    db_factory: async_sessionmaker,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    api_key: str | None = None,
) -> list:
    """
    Build semua builder tools untuk system agent.

    Args:
        db_factory: async_sessionmaker factory — each tool call opens its own session
        owner_phone: external_user_id (nomor WA/JID) dari pengguna yang chat dengan Arthur.
        self_agent_id: UUID agent ini sendiri (Arthur) — untuk self-modification.
        api_key: API key platform — agar Arthur bisa panggil API platform untuk memperbaiki dirinya.
    """

    # ------------------------------------------------------------------ #
    # 0. get_self_config                                                  #
    # ------------------------------------------------------------------ #

    @tool
    async def get_self_config() -> str:
        """
        Dapatkan identitas dan kredensial agent builder ini sendiri (Arthur).
        Gunakan untuk mendapatkan agent_id dan api_key agar bisa memanggil
        API platform untuk memperbaiki atau mengupdate konfigurasi diri sendiri.
        """
        if not self_agent_id:
            return "[error] self_agent_id tidak tersedia — hubungi administrator"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(
                    Agent.id == uuid.UUID(self_agent_id),
                    Agent.is_deleted.is_(False),
                )
            )
            agent = result.scalar_one_or_none()
        instructions_preview = ""
        if agent:
            instructions_preview = (agent.instructions or "")[:500] + (
                "..." if len(agent.instructions or "") > 500 else ""
            )

        return json.dumps({
            "self_agent_id": self_agent_id,
            "api_key": api_key,
            "name": agent.name if agent else None,
            "model": agent.model if agent else None,
            "tools_config": agent.tools_config if agent else None,
            "operator_ids": agent.operator_ids if agent else [],
            "instructions_preview": instructions_preview,
            "note": (
                "Gunakan update_agent(agent_id=self_agent_id, ...) untuk mengupdate konfigurasi dirimu sendiri. "
                "Hanya nomor yang ada di operator_ids yang diizinkan melakukan self-update. "
                "Untuk menambah operator baru: update_agent(agent_id=self_agent_id, add_operator='+62xxx')."
            ),
        }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # compose_agent_instructions                                          #
    # ------------------------------------------------------------------ #

    @tool
    async def compose_agent_instructions(
        preset_id: str,
        agent_name: str,
        business_context: str,
        persona: str = "ramah dan profesional",
        channel: str = "webchat",
        escalation_info: str = "",
        extra_rules: str = "",
    ) -> str:
        """
        Tulis system prompt (instructions) berkualitas tinggi untuk agent baru
        menggunakan model reasoning khusus (deepseek-r1).

        Hasilnya lebih spesifik, lebih kontekstual, dan lebih cerdas dibanding template manual.
        Tidak ada placeholder yang tersisa — semua diisi dengan info nyata.

        WAJIB dipanggil di Fase 4 step 2, sebelum create_agent. Gunakan hasilnya
        sebagai parameter `instructions` saat memanggil create_agent.

        Args:
            preset_id: Preset yang digunakan (coding_deploy_agent, cs_whatsapp_basic, dll)
            agent_name: Nama agent
            business_context: Info bisnis lengkap: produk, layanan, jam buka, kebijakan, harga, dll.
                              Semakin detail semakin baik. Kosong hanya untuk agent coding/general.
            persona: Gaya bicara dan karakter agent (misal: "hangat, sabar, suka bercanda")
            channel: Channel: 'whatsapp' atau 'webchat'
            escalation_info: Kondisi eskalasi dan info operator (misal: "Eskalasi jika komplain besar. Operator: +62812xxx")
            extra_rules: Aturan tambahan yang diminta user
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
            "6. Bahasa Indonesia, natural, sesuai persona yang diminta\n"
            "7. Panjang ideal: 250-500 kata — cukup detail tapi tidak bloated\n"
            "8. Mulai langsung dari 'Kamu adalah...' — tanpa intro atau penjelasan\n"
            "9. SKELETON REFERENSI hanya panduan struktur kapabilitas — JANGAN copy-paste. "
            "Sesuaikan seluruh konten dengan konteks bisnis, nama, dan kebutuhan spesifik user. "
            "Dua agent dengan preset sama tapi bisnis berbeda HARUS punya instructions yang berbeda."
        )

        # Build tool hints so the instruction writer knows which tools are available
        tc_preset = preset.get("tools_config", {})
        tool_hints: list[str] = []
        if tc_preset.get("memory"):
            tool_hints.append(
                "- remember(key, value) / recall(key) / forget(key) — simpan dan ambil info user lintas sesi. "
                "Gunakan untuk menyimpan preferensi, nama, konteks penting yang perlu diingat antar percakapan."
            )
        if tc_preset.get("http"):
            tool_hints.append(
                "- http_get(url) / http_post(url, body) / http_patch(url, body) / http_delete(url) — "
                "akses API eksternal, ambil data dari web, atau kirim data ke sistem lain."
            )
        if tc_preset.get("wa_agent_manager"):
            tool_hints.append(
                "- send_agent_wa_qr(agent_id, caption, phone) — kirim QR WhatsApp ke nomor tertentu agar user bisa scan dan connect."
            )
        if tc_preset.get("scheduler"):
            tool_hints.append(
                "- set_reminder(message, run_at) / list_reminders() / cancel_reminder(id) — jadwalkan pengingat otomatis untuk user."
            )
        if tc_preset.get("rag"):
            tool_hints.append(
                "- search_documents(query) — cari jawaban dari dokumen/knowledge base yang sudah diupload."
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
                "- task description ke sys_coder harus RINGKAS (maks 3-4 kalimat): sebutkan apa yang dibuat, teknologi, port jika perlu\n"
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
            f"SKELETON REFERENSI (jadikan panduan struktur, jangan copy-paste):\n{skeleton[:600] if skeleton else 'Tidak ada'}"
            f"{tools_section}"
            f"{coder_note}\n\n"
            "Tulis system prompt lengkap sekarang. "
            "Pastikan instructions menyebutkan tools yang tersedia dan kapan/cara menggunakannya secara konkret."
        )

        try:
            instructions = await _call_instruction_writer(user_msg, system_msg)

            # Sanity check: flag remaining placeholders
            placeholders = re.findall(r"\{[a-z_]+\}|\[[A-Za-z ]+\]", instructions)

            return json.dumps({
                "instructions": instructions,
                "char_count": len(instructions),
                "remaining_placeholders": placeholders,
                "warning": (
                    f"PERINGATAN: Masih ada {len(placeholders)} placeholder yang belum diisi: {placeholders}. "
                    "Panggil compose_agent_instructions ulang dengan business_context yang lebih lengkap."
                    if placeholders else None
                ),
                "next_step": (
                    "Gunakan 'instructions' di atas sebagai parameter create_agent. "
                    "Jika remaining_placeholders tidak kosong, panggil ulang dengan info lebih lengkap."
                ),
            }, ensure_ascii=False, indent=2)

        except Exception as exc:
            logger.error("builder_tools.compose_agent_instructions.error", error=str(exc))
            fallback = skeleton.replace("{name}", agent_name) if skeleton else ""
            return json.dumps({
                "error": f"Gagal generate dengan model reasoning: {exc}",
                "fallback_skeleton": fallback[:1200] if fallback else "",
                "note": "Tulis instructions manual berdasarkan fallback_skeleton. Pastikan tidak ada placeholder.",
            }, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    # compose_agent_soul                                                  #
    # ------------------------------------------------------------------ #

    @tool
    async def compose_agent_soul(
        preset_id: str,
        agent_name: str,
        role: str,
        business: str = "",
        persona: str = "ramah dan profesional",
        tasks: str = "",
        business_info: str = "",
        escalation: str = "",
        extra_rules: str = "",
    ) -> str:
        """
        Buat soul (identitas permanen) untuk agent yang baru dibuat.
        Soul di-inject otomatis ke setiap sesi agent sebagai fondasi identitasnya.

        WAJIB dipanggil setelah create_agent berhasil, sebelum verify_agent.
        Gunakan hasilnya sebagai value saat kirim ke /v1/agents/{id}/memory dengan key="soul".

        Args:
            preset_id: Preset agent (cs_whatsapp_basic, coding_deploy_agent, dll)
            agent_name: Nama agent
            role: Peran agent (misal: "Customer Service", "Programmer", "Asisten FAQ")
            business: Nama bisnis (misal: "Toko Bunga Melati")
            persona: Karakter/gaya bicara
            tasks: Tugas-tugas utama, satu per baris
            business_info: Info bisnis singkat untuk di-inject ke soul
            escalation: Kondisi eskalasi
            extra_rules: Aturan tambahan
        """
        template = _SOUL_TEMPLATES.get(preset_id, _SOUL_TEMPLATES["cs_whatsapp_basic"])

        # Use r1 to write a rich, filled soul
        system_msg = (
            "Kamu menulis 'soul' — identitas permanen sebuah AI agent. "
            "Soul harus padat, kuat, dan bebas dari placeholder. "
            "Format: teks terstruktur dengan HURUF KAPITAL untuk judul section. "
            "Panjang: 100-180 kata. Jangan gunakan markdown. Mulai langsung dari IDENTITAS."
        )
        user_msg = (
            f"Buat soul untuk agent ini:\n\n"
            f"Nama: {agent_name}\n"
            f"Peran: {role}\n"
            f"Bisnis: {business or 'General'}\n"
            f"Preset: {preset_id}\n"
            f"Persona: {persona}\n"
            f"Tugas utama: {tasks or 'Sesuai preset'}\n"
            f"Info bisnis: {business_info or '-'}\n"
            f"Eskalasi: {escalation or 'Tidak ada'}\n"
            f"Aturan extra: {extra_rules or 'Tidak ada'}\n\n"
            f"Template referensi:\n{template[:500]}\n\n"
            "Tulis soul sekarang:"
        )

        try:
            soul = await _call_instruction_writer(user_msg, system_msg, model=_SOUL_WRITER_MODEL)
            # Strip any leftover placeholders
            placeholders = re.findall(r"\{[a-z_]+\}|\[[A-Za-z ]+\]", soul)
            return json.dumps({
                "soul": soul,
                "char_count": len(soul),
                "remaining_placeholders": placeholders,
                "next_step": "Kirim soul ini via: http_post('/v1/agents/{agent_id}/memory', {'key': 'soul', 'value': soul})",
            }, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("builder_tools.compose_agent_soul.error", error=str(exc))
            # Fallback: fill template manually
            soul_fallback = (
                template
                .replace("{name}", agent_name)
                .replace("{role}", role)
                .replace("{business}", business or "bisnis ini")
                .replace("{persona}", persona)
                .replace("{tasks}", tasks or "- Bantu user sesuai kebutuhan")
                .replace("{business_info}", business_info or "Informasi bisnis belum tersedia")
                .replace("{escalation}", escalation or "Eskalasi jika tidak bisa membantu")
                .replace("{extra_rules}", extra_rules or "")
            )
            return json.dumps({
                "soul": soul_fallback,
                "char_count": len(soul_fallback),
                "note": f"Fallback soul karena model error: {exc}",
                "next_step": "Kirim soul ini via: http_post('/v1/agents/{agent_id}/memory', {'key': 'soul', 'value': soul})",
            }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 1. get_platform_capabilities                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def get_platform_capabilities() -> str:
        """
        Dapatkan ringkasan lengkap kapabilitas platform: tools yang tersedia,
        channel yang didukung, model LLM yang bisa dipakai, dan batasan platform.
        Gunakan ini sebelum merancang konfigurasi agent untuk user.
        Untuk detail preset siap pakai, gunakan get_presets().
        """
        result = {
            "default_model": _DEFAULT_MODEL,
            "tools_config_options": _TOOLS_CONFIG_DOCS,
            "supported_channels": _PLATFORM_CHANNELS,
            "recommended_models": _RECOMMENDED_MODELS,
            "available_presets": list(AGENT_PRESETS.keys()),
            "input_types": [
                "teks — pesan tulis biasa",
                "voice_note — audio PTT, otomatis ditranskrip ke teks via Whisper",
                "gambar — bisa dianalisis jika model mendukung vision",
                "dokumen — PDF/DOCX/TXT, bisa diindeks ke RAG",
            ],
            "platform_limitations": {k: v["description"] for k, v in RUNTIME_LIMITATIONS.items()},
            "agent_optional_params": {
                "max_tokens": {
                    "description": "Batas token output LLM per reply. Lebih kecil = lebih hemat biaya.",
                    "guide": {
                        "WA CS / asisten sederhana": "512-800",
                        "Agent dengan analisis/coding": "1024-2048",
                        "Default platform": "1024",
                    },
                },
            },
            "wa_best_practices": [
                "JANGAN gunakan markdown (*bold*, # heading) — tidak dirender di WA",
                "Batasi respons 1-3 paragraf — user WA tidak suka wall of text",
                "Tentukan bahasa eksplisit (Indonesia/Inggris) di instructions",
                "Sertakan instruksi eskalasi: kapan agent harus panggil operator",
                "Tambahkan 1-2 contoh percakapan (few-shot) di instructions",
            ],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 2. list_available_wa_devices                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def list_available_wa_devices() -> str:
        """
        Lihat daftar WA device (nomor WhatsApp) yang tersedia di platform
        dan belum di-assign ke agent lain. Gunakan ini untuk membantu user
        memilih nomor WA yang akan dipakai agent mereka.
        """
        try:
            async with db_factory() as db:
                result = await db.execute(
                    select(Agent.wa_device_id, Agent.name)
                    .where(Agent.is_deleted.is_(False), Agent.wa_device_id.isnot(None))
                )
                assigned = {row.wa_device_id: row.name for row in result.all()}
            return json.dumps({
                "assigned_devices": [
                    {"device_id": did, "assigned_to_agent": name}
                    for did, name in assigned.items()
                ],
                "note": (
                    "Untuk membuat agent baru dengan WA: gunakan create_agent dengan "
                    "channel_type='whatsapp'. Platform akan generate device baru secara otomatis. "
                    "User kemudian perlu scan QR untuk menghubungkan nomor WA mereka."
                ),
            }, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("builder_tools.list_wa_devices.error", error=str(exc))
            return f"[error] Gagal mengambil daftar device: {exc}"

    # ------------------------------------------------------------------ #
    # 3. get_presets                                                      #
    # ------------------------------------------------------------------ #

    @tool
    async def get_presets(preset_id: str = "") -> str:
        """
        Dapatkan katalog preset agent siap pakai, beserta tools_config, model default,
        batasan runtime, dan panduan smoke test.

        Args:
            preset_id: ID preset spesifik (opsional). Kosong = tampilkan semua preset.
                       Pilihan: coding_deploy_agent, cs_whatsapp_basic, faq_webchat_rag, scheduler_assistant,
                                social_media_agent, data_analyst_agent, research_agent,
                                ecommerce_cs, personal_assistant, hr_assistant
        """
        if preset_id and preset_id in AGENT_PRESETS:
            preset = AGENT_PRESETS[preset_id]
            limitations = {
                lid: RUNTIME_LIMITATIONS[lid]
                for lid in preset.get("runtime_limitations", [])
                if lid in RUNTIME_LIMITATIONS
            }
            return json.dumps({
                "preset_id": preset_id,
                **{k: v for k, v in preset.items() if k != "instruction_skeleton"},
                "instruction_skeleton_preview": preset.get("instruction_skeleton", "")[:300] + "...",
                "runtime_limitations_detail": limitations,
            }, ensure_ascii=False, indent=2)

        if preset_id:
            return json.dumps({
                "error": f"Preset '{preset_id}' tidak ditemukan",
                "available": list(AGENT_PRESETS.keys()),
            }, ensure_ascii=False)

        summary = {}
        for pid, p in AGENT_PRESETS.items():
            critical_lims = [
                RUNTIME_LIMITATIONS[l]["user_message"]
                for l in p.get("runtime_limitations", [])
                if l in RUNTIME_LIMITATIONS and RUNTIME_LIMITATIONS[l]["severity"] == "critical"
            ]
            summary[pid] = {
                "label": p["label"],
                "description": p["description"],
                "default_model": p["default_model"],
                "default_channel": p["default_channel"],
                "key_tools": p["required_tools"],
                "critical_limitations": critical_lims,
                "smoke_test_strategy": p["smoke_test"]["strategy"],
            }
        return json.dumps({"presets": summary}, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 4. plan_agent                                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def plan_agent(
        user_goal: str,
        agent_name: str = "",
        channel: str = "",
        requested_features: str = "",
        persona: str = "",
        business_context: str = "",
        operator_phone: str = "",
    ) -> str:
        """
        Buat rencana agent terstruktur berdasarkan goal user sebelum create.
        Mengembalikan preset yang cocok, tools_config yang direkomendasikan,
        validation warnings, dan langkah selanjutnya.

        Gunakan ini SEBELUM create_agent untuk memastikan config sudah tepat.

        Args:
            user_goal: Deskripsi singkat apa yang user ingin agentnya lakukan
            agent_name: Nama agent yang diinginkan (opsional)
            channel: Channel yang diinginkan: 'whatsapp', 'webchat', atau kosong
            requested_features: Fitur-fitur yang diminta, dipisah koma (misal: 'coding,deploy,http')
            persona: Persona/gaya bicara agent (opsional)
            business_context: Konteks bisnis untuk agent CS/FAQ (opsional)
            operator_phone: Nomor operator/admin untuk eskalasi (opsional)
        """
        goal_lower = user_goal.lower()
        features = [f.strip().lower() for f in requested_features.split(",") if f.strip()]

        # Auto-detect preset from goal keywords
        detected_preset = _detect_preset(goal_lower, features, channel)

        preset = AGENT_PRESETS.get(detected_preset, {})
        tools_config = dict(preset.get("tools_config", {
            "memory": True, "skills": True, "escalation": True
        }))

        # Override with explicitly requested features
        feature_map = {
            "rag": "rag", "dokumen": "rag", "faq": "rag", "document": "rag",
            "scheduler": "scheduler", "reminder": "scheduler", "jadwal": "scheduler",
            "http": "http", "api": "http",
            "sandbox": "sandbox", "coding": "sandbox", "kode": "sandbox",
            "deploy": "deploy",
            "whatsapp_media": "whatsapp_media", "media": "whatsapp_media", "gambar": "whatsapp_media",
        }
        for feat in features:
            mapped = feature_map.get(feat)
            if mapped and mapped in tools_config:
                tools_config[mapped] = True

        # Validate tool dependencies
        validation_errors: list[str] = []
        validation_warnings: list[str] = []

        if tools_config.get("deploy") and not tools_config.get("sandbox"):
            tools_config["sandbox"] = True
            validation_warnings.append("deploy membutuhkan sandbox — sandbox otomatis diaktifkan")

        if tools_config.get("tool_creator") and not tools_config.get("sandbox"):
            tools_config["sandbox"] = True
            validation_warnings.append("tool_creator membutuhkan sandbox — sandbox otomatis diaktifkan")

        # Channel validation
        effective_channel = channel or preset.get("default_channel", "webchat")
        if effective_channel == "whatsapp" and not tools_config.get("escalation"):
            validation_warnings.append("Agent WhatsApp sebaiknya mengaktifkan escalation untuk operator handoff")

        # Surface critical limitations
        critical_limitations = []
        for lid in preset.get("runtime_limitations", []):
            lim = RUNTIME_LIMITATIONS.get(lid)
            if lim:
                if lim["severity"] == "critical":
                    critical_limitations.append(lim["user_message"])
                elif lim["severity"] == "warning":
                    validation_warnings.append(lim["user_message"])

        # Build recommended config
        plan = {
            "plan_status": "ready" if not validation_errors else "has_errors",
            "detected_preset": detected_preset,
            "preset_label": preset.get("label", "Custom"),
            "agent_name": agent_name or f"Agent {detected_preset.replace('_', ' ').title()}",
            "business_goal": user_goal,
            "channel": effective_channel,
            "persona": persona or "ramah dan profesional",
            "business_context": business_context,
            "recommended_config": {
                "model": preset.get("default_model", _DEFAULT_MODEL),
                "temperature": preset.get("default_temperature", 0.7),
                "max_tokens": preset.get("default_max_tokens", 1024),
                "tools_config": tools_config,
                "channel_type": effective_channel if effective_channel != "webchat" else "",
                "escalation_config": (
                    {"channel_type": "whatsapp", "operator_phone": operator_phone}
                    if operator_phone else {}
                ),
            },
            "required_post_create_steps": _get_post_create_steps(detected_preset, effective_channel, tools_config),
            "validation_errors": validation_errors,
            "validation_warnings": validation_warnings,
            "critical_limitations": critical_limitations,
            "smoke_test_guidance": preset.get("smoke_test", {}).get("steps", []),
            "next_action": (
                "Panggil create_agent dengan config di atas. Setelah create, panggil verify_agent(agent_id)."
                if not validation_errors
                else "Perbaiki validation_errors sebelum create."
            ),
        }
        return json.dumps(plan, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 5. verify_agent                                                      #
    # ------------------------------------------------------------------ #

    @tool
    async def verify_agent(agent_id: str) -> str:
        """
        Verifikasi agent yang baru dibuat: baca kembali konfigurasinya dari DB,
        cek apakah tools_config sudah benar, dan berikan panduan smoke test.

        Gunakan ini SETELAH create_agent untuk konfirmasi bahwa agent terbuat dengan benar.

        Args:
            agent_id: UUID agent yang baru dibuat
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
        if not agent:
            return f"[error] Agent dengan ID {agent_id} tidak ditemukan setelah create — kemungkinan create gagal"

        tc: dict = agent.tools_config or {}
        active_tools = [k for k, v in tc.items() if v and v is not False]

        # Detect what preset this looks like
        detected_preset = _detect_preset_from_config(tc, agent.channel_type or "")

        # Check for config issues
        config_warnings: list[str] = []
        if tc.get("deploy") and not tc.get("sandbox"):
            config_warnings.append("CRITICAL: deploy aktif tapi sandbox tidak aktif — deploy akan gagal")
        if tc.get("tool_creator") and not tc.get("sandbox"):
            config_warnings.append("CRITICAL: tool_creator aktif tapi sandbox tidak aktif")
        if agent.channel_type == "whatsapp" and not tc.get("escalation"):
            config_warnings.append("WARNING: Agent WhatsApp tanpa escalation — tidak ada operator handoff")

        # Surface applicable limitations
        preset = AGENT_PRESETS.get(detected_preset, {})
        applicable_limitations = [
            RUNTIME_LIMITATIONS[l]["user_message"]
            for l in preset.get("runtime_limitations", [])
            if l in RUNTIME_LIMITATIONS
        ]

        smoke_test = preset.get("smoke_test", {})
        post_create = _get_post_create_steps(detected_preset, agent.channel_type or "webchat", tc)

        summary = {
            "status": "verified" if not config_warnings else "verified_with_warnings",
            "agent_id": str(agent.id),
            "name": agent.name,
            "model": agent.model,
            "channel_type": agent.channel_type,
            "active_tools": active_tools,
            "max_tokens": agent.max_tokens,
            "detected_preset": detected_preset,
            "config_warnings": config_warnings,
            "applicable_limitations": applicable_limitations,
            "required_next_steps": post_create,
            "smoke_test_steps": smoke_test.get("steps", []),
            "smoke_test_expected": smoke_test.get("expected_status", ""),
            "known_failure_modes": smoke_test.get("known_failure_modes", []),
            "instructions_preview": (agent.instructions or "")[:200] + ("..." if len(agent.instructions or "") > 200 else ""),
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 6. validate_agent_config                                            #
    # ------------------------------------------------------------------ #

    @tool
    async def validate_agent_config(
        name: str,
        instructions: str,
        tools_config: str = "{}",
        model: str = "",
        channel_type: str = "",
        preset_id: str = "",
    ) -> str:
        """
        Validasi konfigurasi agent sebelum disimpan ke database.
        Cek nama, instructions, tools_config, model, channel requirements, dan best practices.

        Args:
            name: Nama agent yang akan dibuat
            instructions: System prompt yang akan divalidasi
            tools_config: JSON string dari tools_config yang direncanakan
            model: Model LLM yang akan digunakan (kosong = pakai default gpt-5.1)
            channel_type: Channel agent: 'whatsapp', 'webchat', atau kosong
            preset_id: ID preset yang digunakan (opsional — untuk validasi preset-specific rules)
        """
        warnings: list[str] = []
        errors: list[str] = []
        suggestions: list[str] = []

        effective_model = model or _DEFAULT_MODEL

        # Validasi nama
        if not name or len(name.strip()) < 2:
            errors.append("Nama agent terlalu pendek (minimal 2 karakter)")
        if len(name) > 255:
            errors.append("Nama agent terlalu panjang (maksimal 255 karakter)")

        # Validasi instructions
        instruction_len = len(instructions)
        if instruction_len < 100:
            errors.append("Instructions terlalu pendek — agent tidak akan punya cukup konteks. Gunakan compose_agent_instructions untuk generate yang baik.")
        if instruction_len > 32000:
            errors.append(f"Instructions terlalu panjang ({instruction_len} karakter) — bisa melebihi context window model")
        elif instruction_len > 16000:
            warnings.append(f"Instructions cukup panjang ({instruction_len} karakter) — pertimbangkan memindahkan detail ke RAG documents")

        # Deteksi placeholder yang belum diisi
        unfilled = re.findall(r"\{[a-z_]+\}|\[[A-Z][a-z A-Z]+\]", instructions)
        if unfilled:
            errors.append(
                f"Instructions masih mengandung {len(unfilled)} placeholder yang belum diisi: {unfilled}. "
                "Panggil compose_agent_instructions untuk generate instructions yang lengkap."
            )

        # Cek few-shot examples untuk WA agent
        if channel_type == "whatsapp" or (not channel_type and "escalat" in instructions.lower()):
            has_example = (
                "user:" in instructions.lower()
                or "contoh" in instructions.lower()
                or "example" in instructions.lower()
                or "percakapan" in instructions.lower()
            )
            if not has_example:
                warnings.append("Instructions tidak punya contoh percakapan — tambahkan 1-2 few-shot examples untuk meningkatkan kualitas respons agent")

        # Validasi tools_config
        try:
            tc = json.loads(tools_config) if isinstance(tools_config, str) else tools_config
        except json.JSONDecodeError:
            errors.append("tools_config bukan JSON yang valid")
            tc = {}

        # Dependency checks — machine-enforced
        if tc.get("tool_creator") and not tc.get("sandbox"):
            errors.append("tool_creator membutuhkan sandbox: true — aktifkan sandbox juga")

        if tc.get("deploy") and not tc.get("sandbox"):
            errors.append("deploy membutuhkan sandbox: true — agent deploy tidak akan bisa deploy tanpa sandbox aktif")

        # Coding/deploy-specific: enforce output contract in instructions
        if tc.get("sandbox") or tc.get("deploy"):
            instr_lower = instructions.lower()
            if "status:" not in instr_lower and "deploy_url" not in instr_lower:
                warnings.append(
                    "Agent coding/deploy sebaiknya memiliki output contract (STATUS: / DEPLOY_URL: / BLOCKER:) di instructions"
                )
            if "get_deployment_status" not in instructions:
                suggestions.append("Tambahkan instruksi untuk panggil get_deployment_status() sebelum deploy ulang")

        # WhatsApp-specific checks
        effective_channel = channel_type or ""
        if effective_channel == "whatsapp" or tc.get("whatsapp_media"):
            if "*" in instructions or "**" in instructions or "##" in instructions:
                warnings.append("Instructions mengandung markdown — tidak akan dirender di WhatsApp, tampil sebagai karakter literal")
            if not tc.get("escalation"):
                warnings.append("Agent WhatsApp sebaiknya mengaktifkan escalation: true untuk operator handoff")
            if "escalate_to_human" not in instructions and tc.get("escalation"):
                warnings.append("escalation aktif tapi instructions tidak menyebut escalate_to_human — agent mungkin tidak tahu cara eskalasi")

        # RAG-specific
        if tc.get("rag"):
            if "search_documents" not in instructions:
                suggestions.append("Tambahkan instruksi untuk menggunakan search_documents saat menjawab pertanyaan")
            warnings.append("Ingat: dokumen harus diupload via /v1/agents/{id}/documents/upload setelah agent dibuat")

        # Scheduler-specific
        if tc.get("scheduler"):
            if "set_reminder" not in instructions and "reminder" not in instructions.lower():
                suggestions.append("Tambahkan instruksi kapan/bagaimana agent menggunakan set_reminder")

        # General best practices
        if "eskalasi" not in instructions.lower() and "escalat" not in instructions.lower() and tc.get("escalation"):
            suggestions.append("Tambahkan instruksi eskalasi: kapan agent harus memanggil operator manusia")
        if len(instructions) > 100 and "contoh" not in instructions.lower() and "example" not in instructions.lower():
            suggestions.append("Pertimbangkan menambahkan 1-2 contoh percakapan (few-shot) untuk meningkatkan kualitas respons")

        # Preset-specific validation
        if preset_id and preset_id in AGENT_PRESETS:
            p = AGENT_PRESETS[preset_id]
            for req_tool in p.get("required_tools", []):
                if not tc.get(req_tool):
                    errors.append(f"Preset '{preset_id}' membutuhkan {req_tool}: true di tools_config")
            for forbidden_tool in p.get("forbidden_tools", []):
                if tc.get(forbidden_tool):
                    warnings.append(f"Preset '{preset_id}' sebaiknya tidak mengaktifkan {forbidden_tool}")

        # Validasi model
        known_models = [m["model"] for m in _RECOMMENDED_MODELS]
        if effective_model not in known_models:
            suggestions.append(f"Model '{effective_model}' tidak ada di daftar rekomendasi — pastikan nama model benar")

        quality_score = 100
        quality_score -= len(errors) * 25
        quality_score -= len(warnings) * 10
        quality_score -= len(suggestions) * 5
        quality_score = max(0, quality_score)

        return json.dumps({
            "valid": len(errors) == 0,
            "quality_score": quality_score,
            "effective_model": effective_model,
            "errors": errors,
            "warnings": warnings,
            "suggestions": suggestions,
            "summary": (
                "Konfigurasi siap dibuat." if len(errors) == 0
                else f"Ada {len(errors)} error yang harus diperbaiki sebelum membuat agent."
            ),
        }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 4. create_agent                                                     #
    # ------------------------------------------------------------------ #

    @tool
    async def create_agent(
        name: str,
        instructions: str,
        description: str = "",
        model: str = "openai/gpt-5.1",
        temperature: float = 0.7,
        tools_config: str = '{"memory": true, "skills": true, "escalation": true}',
        allowed_senders: str = "",
        channel_type: str = "",
        escalation_config: str = "{}",
        operator_phone: str = "",
        operator_name: str = "",
        token_quota: int = 4_000_000,
        max_tokens: int = 0,
    ) -> str:
        """
        Buat agent baru di platform dan simpan ke database.
        Agent akan otomatis dikaitkan dengan user yang sedang chat (owner_phone).

        Args:
            name: Nama agent (wajib, maks 255 karakter)
            instructions: System prompt / instructions lengkap agent
            description: Deskripsi singkat fungsi agent
            model: Model LLM (default: openai/gpt-4.1)
            temperature: Kreativitas respons, 0.0-2.0 (default: 0.7)
            tools_config: JSON string konfigurasi tools, contoh: '{"memory": true, "scheduler": true}'
            allowed_senders: JSON array nomor WA yang diizinkan, contoh: '["+62811xxx"]'. Kosong = semua.
            channel_type: Channel yang dipakai: 'whatsapp', 'webchat', atau kosong
            escalation_config: JSON string konfigurasi eskalasi, contoh: '{"channel_type": "whatsapp", "operator_phone": "+62xxx"}'
            operator_phone: Nomor WA operator/admin yang akan dapat notifikasi eskalasi
            operator_name: Nama operator/admin (misal: "Budi", "Tim CS"). Wajib diisi agar agent tahu siapa operatornya.
            token_quota: Batas token per periode (default: 4,000,000)
            max_tokens: Batas token per reply LLM. WA CS: 512-800, default platform: 1024. Isi 0 untuk pakai default.
        """
        if not name or len(name.strip()) < 2:
            return "[error] Nama agent minimal 2 karakter"

        # Duplicate check: cegah agent dengan nama sama milik user yang sama
        if owner_phone:
            async with db_factory() as db:
                dup_result = await db.execute(
                    select(Agent).where(
                        Agent.name == name.strip(),
                        Agent.is_deleted.is_(False),
                        Agent.operator_ids.contains([owner_phone]),
                    )
                )
                dup = dup_result.scalar_one_or_none()
            if dup:
                return json.dumps({
                    "error": f"Agent dengan nama '{name.strip()}' sudah ada.",
                    "existing_agent_id": str(dup.id),
                    "hint": "Gunakan update_agent(agent_id, ...) untuk mengubah agent yang sudah ada, atau pilih nama yang berbeda.",
                }, ensure_ascii=False)

        try:
            tc: dict[str, Any] = json.loads(tools_config) if tools_config else {"memory": True, "skills": True, "escalation": True}
        except json.JSONDecodeError:
            return "[error] tools_config bukan JSON yang valid"

        try:
            ec: dict[str, Any] = json.loads(escalation_config) if escalation_config else {}
        except json.JSONDecodeError:
            ec = {}

        # Parse allowed_senders
        senders: list[str] | None = None
        if allowed_senders and allowed_senders.strip():
            try:
                parsed = json.loads(allowed_senders)
                senders = parsed if isinstance(parsed, list) else None
            except json.JSONDecodeError:
                return "[error] allowed_senders harus berupa JSON array, contoh: [\"+62811xxx\"]"

        # operator_ids: selalu include owner_phone + operator_phone yang diminta
        op_ids: list[str] = []
        if owner_phone:
            op_ids.append(owner_phone)
        if operator_phone and operator_phone not in op_ids:
            op_ids.append(operator_phone)

        if ec and operator_phone and "operator_phone" not in ec:
            ec["operator_phone"] = operator_phone
        if operator_name and "operator_name" not in ec:
            ec["operator_name"] = operator_name

        try:
            from app.core.domain.subscription_service import (
                check_can_create_agent,
                get_or_create_wa_user,
            )

            logger.info("builder_tools.create_agent.start", owner_phone=owner_phone, name=name)

            async with db_factory() as db:
                # Auto-provision user + Tier 1 subscription untuk WA user
                if owner_phone:
                    _user, _sub = await get_or_create_wa_user(owner_phone, db)
                    logger.info("builder_tools.create_agent.user_provisioned", user_id=str(_user.id), sub_status=_sub.status)

                    # Cek apakah boleh buat agent (slot & status subscription)
                    _check = await check_can_create_agent(owner_phone, db)
                    logger.info("builder_tools.create_agent.slot_check", check=_check)
                    if not _check["allowed"]:
                        return json.dumps({"error": _check["reason"]}, ensure_ascii=False)

                    # Override token_quota & active_until dari subscription
                    token_quota = _sub.token_quota
                    _active_until = _sub.expires_at or _sub.grace_until
                else:
                    _active_until = None

                agent = Agent(
                    name=name.strip(),
                    description=description or None,
                    instructions=instructions,
                    model=model,
                    temperature=temperature,
                    tools_config=tc,
                    sandbox_config={},
                    safety_policy={},
                    escalation_config=ec,
                    operator_ids=op_ids,
                    allowed_senders=senders,
                    capabilities=[],
                    max_tokens=max_tokens if max_tokens > 0 else None,
                    token_quota=token_quota,
                    quota_period_days=30,
                    channel_type=channel_type or None,
                    owner_external_id=owner_phone or None,
                )
                if _active_until:
                    agent.active_until = _active_until

                db.add(agent)
                await db.flush()
                await db.refresh(agent)
                await db.commit()

            logger.info(
                "builder_tools.create_agent.success",
                agent_id=str(agent.id),
                name=agent.name,
                owner_phone=owner_phone,
            )

            return json.dumps({
                "success": True,
                "agent_id": str(agent.id),
                "name": agent.name,
                "model": agent.model,
                "channel_type": agent.channel_type,
                "api_key": agent.api_key,
                "token_quota": agent.token_quota,
                "active_until": agent.active_until.isoformat() if agent.active_until else None,
                "message": (
                    f"Agent '{agent.name}' berhasil dibuat dengan ID: {agent.id}. "
                    "PENTING: Langkah selanjutnya — panggil remember() dengan key='soul' dan value berisi "
                    "identitas lengkap agent ini (nama, peran, cara bicara, aturan kerja). "
                    "Gunakan agent_id di atas sebagai konteks. Soul wajib diisi agar agent punya identitas permanen."
                ),
            }, ensure_ascii=False, indent=2)

        except Exception as exc:
            logger.error("builder_tools.create_agent.error", error=str(exc), owner_phone=owner_phone)
            return f"[error] Gagal membuat agent: {exc}"

    # ------------------------------------------------------------------ #
    # 5. update_agent                                                     #
    # ------------------------------------------------------------------ #

    @tool
    async def update_agent(
        agent_id: str,
        name: str = "",
        instructions: str = "",
        description: str = "",
        model: str = "",
        temperature: float = -1.0,
        tools_config: str = "",
        allowed_senders: str = "",
        escalation_config: str = "",
        add_operator: str = "",
        remove_operator: str = "",
    ) -> str:
        """
        Update konfigurasi agent yang sudah ada. Hanya field yang diisi yang akan diubah.
        Hanya bisa mengupdate agent yang dimiliki oleh user ini (owner_phone).

        Args:
            agent_id: UUID agent yang akan diupdate
            name: Nama baru (opsional)
            instructions: System prompt baru (opsional)
            description: Deskripsi baru (opsional)
            model: Model LLM baru (opsional)
            temperature: Temperature baru 0.0-2.0, isi -1 untuk tidak mengubah (opsional)
            tools_config: JSON string tools_config baru (opsional)
            allowed_senders: JSON array nomor WA baru, kosong = tidak diubah (opsional)
            escalation_config: JSON string escalation_config baru (opsional)
            add_operator: Nomor WA operator baru yang ingin ditambahkan ke operator_ids (opsional)
            remove_operator: Nomor WA operator yang ingin dihapus dari operator_ids (opsional)
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"

            # Cek kepemilikan
            is_self_update = self_agent_id and str(agent_uuid) == self_agent_id
            if is_self_update:
                if owner_phone and owner_phone not in (agent.operator_ids or []):
                    return (
                        "[error] Hanya operator yang terdaftar yang boleh memodifikasi konfigurasi agent builder ini. "
                        f"Nomor kamu ({owner_phone}) tidak ada di daftar operator."
                    )
            elif owner_phone and owner_phone not in (agent.operator_ids or []):
                return f"[error] Kamu tidak punya akses ke agent ini. Hanya agent milikmu yang bisa diubah."

            updated_fields: list[str] = []

            if name and name.strip():
                agent.name = name.strip()
                updated_fields.append("name")

            if instructions:
                agent.instructions = instructions
                updated_fields.append("instructions")

            if description:
                agent.description = description
                updated_fields.append("description")

            if model:
                agent.model = model
                updated_fields.append("model")

            if temperature >= 0.0:
                agent.temperature = temperature
                updated_fields.append("temperature")

            if tools_config:
                try:
                    new_tc = json.loads(tools_config)
                    existing = dict(agent.tools_config) if agent.tools_config else {}
                    existing.update(new_tc)
                    agent.tools_config = existing
                    updated_fields.append("tools_config")
                except json.JSONDecodeError:
                    return "[error] tools_config bukan JSON yang valid"

            if allowed_senders and allowed_senders.strip():
                try:
                    parsed = json.loads(allowed_senders)
                    agent.allowed_senders = parsed if isinstance(parsed, list) else None
                    updated_fields.append("allowed_senders")
                except json.JSONDecodeError:
                    return "[error] allowed_senders harus JSON array"

            if escalation_config:
                try:
                    agent.escalation_config = json.loads(escalation_config)
                    updated_fields.append("escalation_config")
                except json.JSONDecodeError:
                    return "[error] escalation_config bukan JSON yang valid"

            if add_operator and add_operator.strip():
                current_ops: list[str] = list(agent.operator_ids or [])
                new_op = add_operator.strip()
                if new_op not in current_ops:
                    current_ops.append(new_op)
                    agent.operator_ids = current_ops
                    updated_fields.append(f"operator_ids+{new_op}")

            if remove_operator and remove_operator.strip():
                current_ops = list(agent.operator_ids or [])
                rm_op = remove_operator.strip()
                if rm_op in current_ops:
                    current_ops.remove(rm_op)
                    agent.operator_ids = current_ops
                    updated_fields.append(f"operator_ids-{rm_op}")

            if not updated_fields:
                return "[info] Tidak ada field yang diubah — kirim minimal satu field untuk diupdate"

            agent.version = (agent.version or 1) + 1
            await db.commit()

        logger.info("builder_tools.update_agent.success", agent_id=agent_id, fields=updated_fields)
        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "updated_fields": updated_fields,
            "new_version": agent.version,
            "message": f"Agent '{agent.name}' berhasil diupdate. Field yang diubah: {', '.join(updated_fields)}",
        }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 6. get_agent_detail                                                 #
    # ------------------------------------------------------------------ #

    @tool
    async def get_agent_detail(agent_id: str) -> str:
        """
        Baca konfigurasi lengkap sebuah agent. Gunakan untuk review sebelum update,
        atau untuk debugging konfigurasi agent yang sudah ada.

        Args:
            agent_id: UUID agent yang ingin dilihat
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
        if not agent:
            return f"[error] Agent dengan ID {agent_id} tidak ditemukan"

        # Cek kepemilikan — bypass jika membaca diri sendiri
        is_self = self_agent_id and str(agent_uuid) == self_agent_id
        if not is_self and owner_phone and owner_phone not in (agent.operator_ids or []):
            return f"[error] Kamu tidak punya akses ke agent ini"

        return json.dumps({
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "model": agent.model,
            "temperature": agent.temperature,
            "tools_config": agent.tools_config,
            "escalation_config": agent.escalation_config,
            "operator_ids": agent.operator_ids,
            "allowed_senders": agent.allowed_senders,
            "channel_type": agent.channel_type,
            "wa_device_id": agent.wa_device_id,
            "token_quota": agent.token_quota,
            "tokens_used": agent.tokens_used,
            "active_until": agent.active_until.isoformat() if agent.active_until else None,
            "version": agent.version,
            "instructions_preview": (agent.instructions or "")[:300] + ("..." if len(agent.instructions or "") > 300 else ""),
        }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 7. list_my_agents                                                   #
    # ------------------------------------------------------------------ #

    @tool
    async def list_my_agents() -> str:
        """
        Tampilkan semua agent yang kamu buat/miliki di platform ini.
        Agent diidentifikasi berdasarkan nomor WA yang sedang digunakan untuk chat.
        """
        if not owner_phone:
            return "[error] Tidak bisa identifikasi pemilik — pastikan kamu chat dari nomor WA yang terdaftar"

        try:
            async with db_factory() as db:
                result = await db.execute(
                    select(Agent).where(
                        Agent.is_deleted.is_(False),
                        ~Agent.capabilities.contains(["system"]),
                    )
                )
                all_agents = result.scalars().all()

            my_agents = [
                a for a in all_agents
                if owner_phone in (a.operator_ids or [])
            ]

            if not my_agents:
                return json.dumps({
                    "count": 0,
                    "agents": [],
                    "message": "Kamu belum punya agent. Mau saya bantu buatkan yang pertama?",
                }, ensure_ascii=False, indent=2)

            return json.dumps({
                "count": len(my_agents),
                "agents": [
                    {
                        "id": str(a.id),
                        "name": a.name,
                        "description": a.description,
                        "model": a.model,
                        "channel_type": a.channel_type,
                        "wa_device_id": a.wa_device_id,
                        "token_quota": a.token_quota,
                        "tokens_used": a.tokens_used,
                        "active_until": a.active_until.isoformat() if a.active_until else None,
                        "tools_active": [k for k, v in (a.tools_config or {}).items() if v],
                    }
                    for a in my_agents
                ],
            }, ensure_ascii=False, indent=2)

        except Exception as exc:
            logger.error("builder_tools.list_my_agents.error", error=str(exc))
            return f"[error] Gagal mengambil daftar agent: {exc}"

    return [
        get_self_config,
        get_platform_capabilities,
        get_presets,
        plan_agent,
        compose_agent_instructions,
        compose_agent_soul,
        verify_agent,
        list_available_wa_devices,
        validate_agent_config,
        create_agent,
        update_agent,
        get_agent_detail,
        list_my_agents,
    ]

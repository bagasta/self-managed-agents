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
import uuid
from typing import Any

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
            "subagents": {"enabled": False},
        },
        "required_tools": ["sandbox", "deploy"],
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
        "Delegasi ke sub-agent spesialis. Default OFF. "
        "Sub-agent yang tersedia: "
        "sys_coder (programmer full-stack: tulis kode Python/JS/HTML, jalankan di sandbox, deploy website ke Cloudflare public URL — kembalikan link ke user), "
        "sys_researcher (riset internet via HTTP), "
        "sys_writer (tulis/edit konten), "
        "sys_analyst (analisis data dengan pandas/numpy). "
        "Gunakan subagents:true jika agent perlu kemampuan coding+deploy SEKALIGUS tugas lain (riset, tulis, analisis). "
        "Jika hanya perlu coding+deploy saja, cukup sandbox:true tanpa subagents."
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

    goal_words = set(goal_lower.split())

    if goal_words & coding_keywords or any(f in coding_keywords for f in features):
        return "coding_deploy_agent"

    if channel == "whatsapp" and (goal_words & cs_keywords or any(f in cs_keywords for f in features)):
        return "cs_whatsapp_basic"

    if goal_words & faq_keywords or any(f in faq_keywords for f in features):
        return "faq_webchat_rag"

    if goal_words & scheduler_keywords or any(f in scheduler_keywords for f in features):
        return "scheduler_assistant"

    # Default: if channel is whatsapp, use cs; otherwise general (faq_webchat_rag as fallback)
    if channel == "whatsapp":
        return "cs_whatsapp_basic"

    return "faq_webchat_rag"


def _detect_preset_from_config(tc: dict, channel_type: str) -> str:
    """Reverse-detect preset from an existing tools_config."""
    if tc.get("sandbox") or tc.get("deploy"):
        return "coding_deploy_agent"
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
                       Pilihan: coding_deploy_agent, cs_whatsapp_basic, faq_webchat_rag, scheduler_assistant
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
        if instruction_len < 50:
            warnings.append("Instructions sangat pendek — agent mungkin tidak punya cukup konteks untuk bekerja dengan baik")
        if instruction_len > 32000:
            errors.append(f"Instructions terlalu panjang ({instruction_len} karakter) — bisa melebihi context window model")
        elif instruction_len > 16000:
            warnings.append(f"Instructions cukup panjang ({instruction_len} karakter) — pertimbangkan memindahkan detail ke RAG documents")

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
            token_quota: Batas token per periode (default: 4,000,000)
            max_tokens: Batas token per reply LLM. WA CS: 512-800, default platform: 1024. Isi 0 untuk pakai default.
        """
        if not name or len(name.strip()) < 2:
            return "[error] Nama agent minimal 2 karakter"

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

        try:
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
            )
            async with db_factory() as db:
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
                "message": f"Agent '{agent.name}' berhasil dibuat dengan ID: {agent.id}",
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
        verify_agent,
        list_available_wa_devices,
        validate_agent_config,
        create_agent,
        update_agent,
        get_agent_detail,
        list_my_agents,
    ]

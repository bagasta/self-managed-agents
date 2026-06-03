"""
builder_tools.py — Tools eksklusif untuk system agent (Agent Builder / Arthur).

Hanya dimuat jika agent memiliki capability 'builder' atau 'system'.

Tools yang di-expose:
  get_platform_capabilities()           — ringkasan kapabilitas platform
  get_user_subscription(phone)          — cek plan, slot agent, dan status subscription user
  get_presets()                         — katalog preset agent siap pakai
  plan_agent(...)                       — structured plan sebelum create
  compose_agent_blueprint(...)          — rancang workflow & knowledge plan custom per bisnis
  compose_agent_operating_manual(...)   — susun SOP/Agent Operating Manual spesifik dari blueprint
  verify_agent(agent_id)               — post-create readback + smoke test guidance
  list_available_wa_devices()           — WA devices yang belum di-assign ke agent
  validate_agent_config(...)            — validasi config sebelum create/update
  create_agent(...)                     — buat agent baru (di-scope ke owner_phone)
  create_wa_dev_trial_link(...)         — generate kode + link shared WA Arthur untuk coba agent tanpa scan QR
  set_agent_memory(...)                 — simpan soul/blueprint langsung ke memory agent
  update_agent(...)                     — update agent yang sudah ada
  get_agent_detail(agent_id)            — baca konfigurasi agent
  list_my_agents()                      — list agent milik owner_phone ini
  delete_agent(...)                     — soft delete agent milik owner_phone ini

Keamanan:
  - create_agent otomatis memasukkan owner_phone ke operator_ids → agen terisolasi per user
  - update_agent / get_agent_detail / delete_agent memverifikasi kepemilikan via operator_ids
  - list_my_agents hanya tampilkan agent yang memiliki owner_phone di operator_ids
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any
from urllib.parse import quote

import structlog
from langchain_core.tools import tool
from openai import AsyncOpenAI
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.domain.agent_sop_service import (
    build_agent_operating_manual_from_blueprint,
    ensure_operating_manual_in_tools_config,
    format_operating_manual_for_prompt,
    get_agent_operating_manual,
    get_latest_agent_operating_manual,
    operating_manual_readiness_issues,
    summarize_operating_manual,
    upsert_agent_operating_manual,
)
from app.core.engine.google_mcp_support import _is_plain_google_form_link_reference
from app.core.engine.google_mcp_support import _candidate_external_user_ids
from app.core.utils.phone_utils import normalize_phone
from app.core.utils.wa_identity import is_probable_whatsapp_lid
from app.models.agent import Agent
from app.models.document import Document

logger = structlog.get_logger(__name__)

_PROHIBITED_AGENT_POLICY_MESSAGE = (
    "Tidak bisa membuat atau mengubah agent untuk keperluan buzzer, kampanye politik, "
    "propaganda politik, atau manipulasi opini publik."
)

_PROHIBITED_AGENT_POLICY_PATTERNS = (
    re.compile(r"\bbuzzer\b", re.IGNORECASE),
    re.compile(r"\bpolitik(?:al)?\b", re.IGNORECASE),
    re.compile(r"\bpolitic(?:al|s)?\b", re.IGNORECASE),
    re.compile(r"\bpemilu\b", re.IGNORECASE),
    re.compile(r"\bpilkada\b", re.IGNORECASE),
    re.compile(r"\bpilpres\b", re.IGNORECASE),
    re.compile(r"\bcaleg\b", re.IGNORECASE),
    re.compile(r"\bcapres\b", re.IGNORECASE),
    re.compile(r"\bcawapres\b", re.IGNORECASE),
    re.compile(r"\bpartai\b", re.IGNORECASE),
    re.compile(r"\bpropaganda\b", re.IGNORECASE),
)


def _blocked_agent_policy_reason(*parts: Any) -> str:
    text = "\n".join(str(part or "") for part in parts)
    if not text.strip():
        return ""
    for pattern in _PROHIBITED_AGENT_POLICY_PATTERNS:
        if pattern.search(text):
            return _PROHIBITED_AGENT_POLICY_MESSAGE
    return ""


def _owner_variants(owner_phone: str | None) -> list[str]:
    """Return stable owner identifiers used by old and new agent rows."""
    variants: list[str] = []
    for candidate in (owner_phone, normalize_phone(owner_phone or "")):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def _is_probable_lid(value: str | None) -> bool:
    normalized = normalize_phone(value or "")
    return bool(normalized and normalized.isdigit() and len(normalized) > 15)


def _best_owner_identifier(*candidates: str | None) -> str:
    """Prefer real phone identifiers; fall back to LID only for lookup, not provisioning."""
    fallback = ""
    for candidate in candidates:
        normalized = normalize_phone(str(candidate or ""))
        if not normalized:
            continue
        if not fallback:
            fallback = normalized
        if not _is_probable_lid(normalized):
            return normalized
    return fallback


def _extract_operator_phone_from_context(*parts: Any) -> str:
    text = " ".join(str(part or "") for part in parts)
    if not text.strip():
        return ""
    patterns = (
        r"(?:admin|operator|owner|pemilik|saya)\D{0,40}(\+?62\d{8,15})",
        r"(\+?62\d{8,15})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_phone(match.group(1))
    return ""


def _agent_belongs_to_owner(agent: Agent, owner_phone: str | None) -> bool:
    """Check ownership via canonical owner field and legacy operator_ids."""
    variants = set(_owner_variants(owner_phone))
    if not variants:
        return False
    owner_external_id = getattr(agent, "owner_external_id", None)
    if owner_external_id in variants or normalize_phone(owner_external_id or "") in variants:
        return True
    for op in (getattr(agent, "operator_ids", None) or []):
        if op in variants or normalize_phone(op or "") in variants:
            return True
    return False


def _safe_agent_str_attr(agent: Agent, attr: str) -> str | None:
    value = getattr(agent, attr, None)
    if value is None:
        return None
    if value.__class__.__module__.startswith("unittest.mock"):
        return None
    text = str(value).strip()
    return text or None


def _agent_created_by_metadata(agent: Agent) -> dict[str, str | None]:
    return {
        "created_by_type": _safe_agent_str_attr(agent, "created_by_type"),
        "created_by_agent_id": _safe_agent_str_attr(agent, "created_by_agent_id"),
        "created_by_agent_name": _safe_agent_str_attr(agent, "created_by_agent_name"),
    }


def _owner_filter(owner_phone: str | None):
    variants = _owner_variants(owner_phone)
    if not variants:
        return Agent.id.is_(None)
    clauses = [Agent.owner_external_id.in_(variants)]
    clauses.extend(Agent.operator_ids.contains([variant]) for variant in variants)
    return or_(*clauses)


async def _latest_owned_agent_for_trial(
    db: AsyncSession,
    *,
    owner_phone: str | None,
    self_agent_id: str | None,
) -> Agent | None:
    """Resolve the newest user-owned agent for shared WA trial fallback."""
    if not owner_phone:
        return None
    stmt = (
        select(Agent)
        .where(Agent.is_deleted.is_(False), _owner_filter(owner_phone))
        .order_by(Agent.created_at.desc(), Agent.updated_at.desc())
        .limit(8)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    for agent in rows:
        if self_agent_id and str(getattr(agent, "id", "")) == str(self_agent_id):
            continue
        capabilities = getattr(agent, "capabilities", None) or []
        tools_config = getattr(agent, "tools_config", None) or {}
        if "builder" in capabilities or (isinstance(tools_config, dict) and tools_config.get("builder")):
            continue
        return agent
    return None

# ---------------------------------------------------------------------------
# Structured preset definitions — source of truth for agent types
# ---------------------------------------------------------------------------

AGENT_PRESETS: dict[str, dict] = {
    "coding_deploy_agent": {
        "label": "Coding & Deploy Agent",
        "description": "Agent yang bisa menulis kode, menjalankannya di sandbox Docker, dan men-deploy ke public URL via Cloudflare tunnel.",
        "default_model": "openai/gpt-4.1-mini",
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
            "1. Untuk website/web app/frontend/landing page/portfolio/dashboard prototype: gunakan vanilla HTML/CSS/JavaScript saja\n"
            "   - File wajib terpisah: index.html, styles.css, script.js jika perlu interaksi\n"
            "   - JANGAN inline CSS/JS di HTML\n"
            "   - JANGAN pakai React/Next/Vue/Svelte/Astro/Tailwind/Bootstrap/Vite/npm/npx/CDN library/framework frontend\n"
            "2. Tulis semua file ke workspace (write_file) — jangan tanya konfirmasi dulu\n"
            "3. Cek status: panggil get_deployment_status()\n"
            "   - Jika 'running' → kembalikan URL yang ada, jangan deploy ulang\n"
            "   - Jika 'not_deployed' → lanjut ke langkah 4\n"
            "4. Deploy: panggil deploy_app(command, port)\n"
            "5. Verifikasi: panggil get_deployment_status() lagi — pastikan URL ada dan status 'running'\n"
            "   - Jika URL kosong atau error → panggil get_deployment_logs() → debug → perbaiki\n"
            "6. Jawab user dengan ramah dan asisten-like (seperti asisten manusia), tapi WAJIB sertakan URL hasil deploy.\n\n"
            "ATURAN KERAS:\n"
            "- Bersikaplah seperti AI Assistant yang ramah, gunakan bahasa yang natural.\n"
            "- JANGAN menggunakan format robotik/algoritma seperti 'STATUS: SUCCESS | DEPLOY_URL:'.\n"
            "- JANGAN tampilkan source code di jawaban akhir kecuali user eksplisit minta\n"
            "- JANGAN jelaskan cara kerja kode panjang lebar — langsung eksekusi\n"
            "- Task BELUM selesai sampai deploy_app() sukses dan URL dikonfirmasi\n"
            "- Untuk static website vanilla: deploy_app('cd /workspace/src && python3 -m http.server 8080', 8080)\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai tapi sopan.\n"
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
                "Pastikan agent merespons mengikuti bahasa user tanpa markdown",
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
    "approval_gated_service_agent": {
        "label": "Approval-Gated Service Agent",
        "description": (
            "Agent WhatsApp untuk layanan/order berbayar yang mengumpulkan kebutuhan, meminta pembayaran, "
            "meneruskan bukti ke admin/operator, menunggu approval, lalu mengirim hasil layanan."
        ),
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.4,
        "default_max_tokens": 2048,
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
            "http": True,
            "whatsapp_media": True,
            "subagents": {"enabled": False},
        },
        "required_tools": ["escalation"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": ["whatsapp_device_required"],
        "runtime_limitations": [
            "markdown_not_rendered_on_whatsapp",
            "one_wa_number_per_agent",
            "wa_device_scan_required_before_use",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten WhatsApp untuk layanan berbayar dari {business}.\n\n"
            "STATE WAJIB:\n"
            "1. intake: sambut user, jelaskan proses singkat, kumpulkan data kebutuhan layanan/order.\n"
            "2. waiting_payment: setelah data cukup, minta user transfer biaya jasa dan kirim bukti transfer.\n"
            "3. payment_review: saat bukti transfer diterima, panggil escalate_to_human(reason, summary) untuk approval admin. "
            "JANGAN lanjut fulfillment atau mengirim hasil final sebelum admin approve.\n"
            "4. approved: setelah operator/admin approve, lanjutkan fulfillment layanan.\n"
            "5. delivery: kirim hasil layanan ke customer. Jika hasilnya file/PDF/DOCX, buat lewat subagent yang punya sandbox lalu kirim via send_whatsapp_document.\n"
            "6. aftercare: bantu revisi ringan sesuai kebijakan bisnis.\n\n"
            "ATURAN KERAS:\n"
            "- Jangan klaim hasil layanan sudah selesai jika belum ada output nyata dari tool/subagent/proses bisnis.\n"
            "- Jangan klaim file sudah terkirim sebelum send_whatsapp_document sukses atau sub-agent melaporkan TERKIRIM.\n"
            "- Jangan menyarankan user download manual jika delivery WhatsApp media aktif; kirim langsung via WhatsApp.\n"
            "- Jika pembayaran belum approved, berhenti di payment_review dan tunggu admin.\n\n"
            "CONTOH UNTUK JASA CV ATS:\n"
            "- Kumpulkan nama, kontak, posisi target, pengalaman kerja, pendidikan, skill, proyek, sertifikasi, link portfolio/LinkedIn, dan preferensi bahasa.\n"
            "- Jika user punya CV lama/dokumen referensi, gunakan itu untuk mengurangi pertanyaan.\n"
            "- Boleh riset posisi/keyword ATS sebagai pendukung, tetapi fulfillment CV tetap mengikuti state pembayaran dan approval.\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Mau pesan layanan ini\n"
            "{name}: Bisa. Saya bantu dari pengumpulan kebutuhan sampai hasil final. Kebutuhan utamanya apa dulu?\n"
            "User: Saya sudah transfer, ini buktinya\n"
            "{name}: Terima kasih, saya teruskan bukti transfernya ke admin dulu untuk dicek. Hasil final baru saya kirim setelah pembayaran disetujui.\n\n"
            "INFO BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Kirim: 'Mau pesan jasa ini'",
                "Pastikan agent masuk intake dan tanya kebutuhan layanan/order, bukan langsung mengarang hasil.",
                "Kirim bukti transfer.",
                "Pastikan agent memanggil escalate_to_human sebelum menjanjikan delivery.",
                "Setelah operator approve, pastikan hasil layanan dikirim. Untuk hasil file, pastikan memakai send_whatsapp_document.",
            ],
            "expected_status": "Agent mengikuti state payment approval dan delivery setelah admin approve.",
            "known_failure_modes": [
                "escalation=false → admin tidak menerima bukti transfer",
                "subagents/whatsapp_media=false pada workflow file → file final tidak bisa dibuat/dikirim langsung",
            ],
        },
    },
    "faq_webchat_rag": {
        "label": "FAQ & RAG Webchat Agent",
        "description": "Agent yang menjawab pertanyaan berdasarkan dokumen yang diupload (PDF, DOCX). Cocok untuk FAQ produk, kebijakan, manual.",
        "default_model": "openai/gpt-4.1-mini",
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
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai.\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "- Generate file PDF atau Excel dengan sys_coder, lalu parent agent mengirim file ke user\n"
            "- Buat draft caption, hashtag, dan ide visual konten\n\n"
            "CARA GENERATE DAN KIRIM FILE (WAJIB IKUTI):\n"
            "Saat user minta file (PDF, Excel, gambar):\n"
            "1. Riset dulu jika perlu (http_get)\n"
            "2. Delegate ke sys_coder: task('sys_coder', task='Buat file [format] berisi [konten]. "
            "Simpan file final ke /workspace/shared/[filename]. Output akhir wajib menyebut path /workspace/shared/[filename] "
            "dan status SIAP_DIKIRIM_PARENT. Jangan kirim WhatsApp dari sub-agent.')\n"
            "3. Setelah task() return path shared, parent agent wajib panggil send_whatsapp_document/send_whatsapp_image sendiri.\n"
            "4. Setelah tool parent sukses, relay hasil ke user — jangan bilang 'file perlu didownload manual'\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya energik dan kreatif.\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "Buat grafik dan laporan ringkas. Simpan file final ke /workspace/shared/[filename]. "
            "Output akhir wajib menyebut path /workspace/shared/[filename] dan status SIAP_DIKIRIM_PARENT. "
            "Jangan kirim WhatsApp dari sub-agent.')\n"
            "3. Setelah task() return path shared, parent agent wajib panggil send_whatsapp_document/send_whatsapp_image sendiri.\n"
            "4. Relay insight ke user dalam bahasa sederhana\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya jelas dan berbasis data.\n"
            "Selalu sertakan angka dan fakta dalam jawaban\n\n"
            "KONTEKS BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim file CSV sederhana",
                "Minta: 'Analisis data ini dan buat grafiknya'",
                "Pastikan agent delegate ke sys_coder, lalu parent agent mengirim hasil grafik",
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
        "default_model": "openai/gpt-4.1-mini",
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
            "CARA MENYERAHKAN HASIL:\n"
            "- Default: jawab hasil riset langsung di chat dengan ringkasan, insight, rekomendasi, dan sumber\n"
            "- Simpan ringkasan penting ke memory dengan remember/update_longterm jika tool memory tersedia\n"
            "- Jangan membuat file laporan dengan write_file kecuali user eksplisit minta file/export atau laporan sangat panjang\n"
            "- Jika file laporan sudah dibuat, jangan tulis ulang path yang sama; gunakan read_file + edit_file atau beri jawaban final\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya ramah dan sabar.\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai seperti asisten pribadi.\n"
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
        "default_model": "openai/gpt-4.1-mini",
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
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya profesional tapi ramah.\n"
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

for _preset in AGENT_PRESETS.values():
    _preset_tools_config = _preset.get("tools_config")
    if isinstance(_preset_tools_config, dict):
        _preset_tools_config.setdefault("tavily", True)

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
    "tavily": "Web browsing/search via Tavily (tavily_search/tavily_extract). Default ON jika TAVILY_API_KEY tersedia.",
    "mcp": "Integrasi eksternal tingkat lanjut seperti Google Workspace/Docs/Sheets/Drive. Default OFF.",
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
    {"model": "openai/gpt-4.1-mini", "use_case": "Budget default — cukup kuat untuk mayoritas agent, lebih hemat"},
    {"model": "openai/gpt-4.1", "use_case": "Balance cost & quality (generasi sebelumnya)"},
    {"model": "openai/gpt-4.1-nano", "use_case": "Ultra-fast response"},
    {"model": "anthropic/claude-sonnet-4-6", "use_case": "Reasoning kompleks, nuanced"},
    {"model": "openai/gpt-4o", "use_case": "Analisis gambar/dokumen (vision)"},
]

_DEFAULT_MODEL = "openai/gpt-4.1-mini"


def _google_workspace_mcp_server_config() -> dict[str, str]:
    settings = get_settings()
    return {
        "url": settings.workspace_mcp_url or "https://msj90wr2-8002.asse.devtunnels.ms/mcp",
        "transport": "streamable_http",
    }


def _enable_google_workspace_tools(tools_config: dict[str, Any] | None) -> dict[str, Any]:
    """Enable Google Workspace tooling without clobbering other tool config."""
    merged = dict(tools_config or {})
    raw_mcp = merged.get("mcp")
    mcp_cfg = dict(raw_mcp) if isinstance(raw_mcp, dict) else {}

    if "servers" in mcp_cfg or "enabled" in mcp_cfg:
        servers = dict(mcp_cfg.get("servers") or {})
    else:
        servers = {
            name: dict(cfg)
            for name, cfg in mcp_cfg.items()
            if isinstance(cfg, dict)
        }

    existing_google = dict(servers.get("google_workspace") or {})
    google_cfg = _google_workspace_mcp_server_config()
    existing_google.setdefault("url", google_cfg["url"])
    existing_google.setdefault("transport", google_cfg["transport"])
    servers["google_workspace"] = existing_google

    mcp_cfg["enabled"] = True
    mcp_cfg["servers"] = servers
    merged["mcp"] = mcp_cfg
    merged.setdefault("tavily", True)
    return merged


def _has_google_workspace_tools(tools_config: dict[str, Any] | None) -> bool:
    if not isinstance(tools_config, dict):
        return False
    mcp_cfg = tools_config.get("mcp")
    if not isinstance(mcp_cfg, dict):
        return False
    if "servers" in mcp_cfg or "enabled" in mcp_cfg:
        return bool(mcp_cfg.get("enabled")) and "google_workspace" in (mcp_cfg.get("servers") or {})
    return isinstance(mcp_cfg.get("google_workspace"), dict)


def _tool_config_enabled(tools_config: dict[str, Any] | None, key: str, *, default: bool = False) -> bool:
    if not isinstance(tools_config, dict):
        return default
    cfg = tools_config.get(key)
    if cfg is None:
        return default
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", default))
    return bool(cfg)


def _rag_enabled(tools_config: dict[str, Any] | None) -> bool:
    return _tool_config_enabled(tools_config, "rag", default=False)


async def _count_agent_documents(db: AsyncSession, agent_id: uuid.UUID) -> int | None:
    try:
        result = await db.execute(select(func.count()).where(Document.agent_id == agent_id))
        return int(result.scalar_one() or 0)
    except Exception as exc:
        logger.warning("builder_tools.verify_agent.document_count_failed", error=str(exc))
        return None


def _build_owner_setup_status(
    *,
    launch_status: str,
    owner_present: bool,
    created_by_present: bool,
    operating_manual: dict[str, Any] | None = None,
    google_workspace_enabled: bool,
    rag_enabled: bool,
    document_count: int | None,
    whatsapp_channel: bool,
    whatsapp_ready: bool,
    escalation_enabled: bool,
    readiness_blockers: list[str],
    readiness_warnings: list[str],
) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    next_steps: list[str] = []

    def add_item(key: str, status: str, message: str) -> None:
        items.append({"key": key, "status": status, "message": message})

    add_item(
        "owner",
        "ready" if owner_present else "needs_setup",
        "Owner agent sudah tercatat." if owner_present else "Owner agent belum tercatat. Simpan nomor Owner dulu agar agent tahu siapa pemiliknya.",
    )
    if not owner_present:
        next_steps.append("Tambahkan nomor Owner agent.")

    add_item(
        "platform_identity",
        "ready" if created_by_present else "needs_review",
        "Sumber pembuatan agent sudah tercatat." if created_by_present else "Agent lama belum punya metadata pembuat. Runtime tetap aman, tapi data ini perlu dirapikan.",
    )
    if not created_by_present:
        next_steps.append("Review metadata pembuat agent lama.")

    manual = operating_manual or {}
    manual_present = bool(manual.get("present"))
    manual_maturity = str(manual.get("maturity") or "missing")
    if not manual_present:
        add_item(
            "operating_manual",
            "needs_setup",
            "SOP kerja agent belum dibuat terpisah. Buat Agent Operating Manual dulu agar cara kerja agent bisa dicek.",
        )
        next_steps.append("Buat atau review SOP kerja agent.")
    elif manual_maturity in {"draft", "needs_review"}:
        add_item(
            "operating_manual",
            "needs_review",
            "SOP kerja agent masih draft. Agent aman untuk tanya kebutuhan dan membuat ringkasan, tapi belum boleh mengambil keputusan final.",
        )
        next_steps.append("Lengkapi dan review SOP kerja agent sebelum full launch.")
    elif manual_maturity == "verified":
        add_item("operating_manual", "ready", "SOP kerja agent sudah verified.")
    else:
        add_item(
            "operating_manual",
            "ready",
            "SOP kerja agent sudah tersedia dan bisa dipakai untuk workflow utama.",
        )

    if google_workspace_enabled:
        add_item(
            "google_workspace",
            "needs_setup",
            "Google sudah dipilih, tapi Owner perlu login atau sambungkan ulang Google sebelum agent boleh menjalankan tugas Google.",
        )
        next_steps.append("Minta Owner login atau sambungkan ulang Google Workspace.")
    else:
        add_item("google_workspace", "not_used", "Google Workspace tidak dipakai untuk agent ini.")

    if rag_enabled:
        if document_count is None:
            add_item("knowledge_base", "needs_review", "Knowledge base aktif, tapi jumlah dokumen belum bisa dicek.")
            next_steps.append("Cek dokumen knowledge base agent.")
        elif document_count > 0:
            add_item("knowledge_base", "ready", f"Knowledge base sudah berisi {document_count} dokumen.")
        else:
            add_item(
                "knowledge_base",
                "needs_setup",
                "Knowledge base aktif, tapi belum ada dokumen. Upload SOP/FAQ/dokumen dulu agar agent bisa menjawab dari dokumen.",
            )
            next_steps.append("Upload dokumen knowledge base untuk agent.")
    else:
        add_item("knowledge_base", "not_used", "Knowledge base/RAG tidak dipakai untuk agent ini.")

    if whatsapp_channel:
        add_item(
            "whatsapp",
            "ready" if whatsapp_ready else "needs_setup",
            "WhatsApp agent sudah punya device/nomor." if whatsapp_ready else "WhatsApp belum punya device/nomor aktif. Pasang nomor WhatsApp atau pakai nomor demo dulu.",
        )
        if not whatsapp_ready:
            next_steps.append("Hubungkan nomor WhatsApp agent atau buat trial lewat nomor demo Arthur.")
    else:
        add_item("whatsapp", "not_used", "Agent ini bukan channel WhatsApp.")

    add_item(
        "human_handoff",
        "ready" if escalation_enabled else "not_used",
        "Handoff ke manusia aktif." if escalation_enabled else "Handoff ke manusia tidak aktif. Aktifkan jika workflow butuh approval admin/operator.",
    )

    if readiness_blockers:
        summary = "Agent belum siap launch. Ada setup yang perlu dibereskan dulu."
    elif readiness_warnings:
        summary = "Agent bisa dites, tapi ada data lama yang sebaiknya dirapikan."
    else:
        summary = "Agent siap dites atau digunakan."

    return {
        "status": launch_status,
        "summary_for_owner": summary,
        "items": items,
        "next_steps": next_steps,
    }


def _normalize_refresh_memory_mode(value: str | None) -> str:
    mode = str(value or "selective").strip().lower()
    if mode in {"none", "selective", "major"}:
        return mode
    return "selective"


def _build_refreshed_agent_memory_values(
    *,
    agent: Agent,
    context_version: int,
    mode: str,
    updated_fields: list[str],
) -> dict[str, str]:
    tools_config = agent.tools_config if isinstance(agent.tools_config, dict) else {}
    agent_name = _safe_agent_str_attr(agent, "name") or "Agent"
    description = _safe_agent_str_attr(agent, "description") or ""
    channel_type = _safe_agent_str_attr(agent, "channel_type") or ""
    owner_external_id = _safe_agent_str_attr(agent, "owner_external_id")
    summary = {
        "agent_id": str(agent.id),
        "agent_name": agent_name,
        "context_version": context_version,
        "refresh_mode": mode,
        "change_level": "major" if mode == "major" else "selective",
        "updated_fields": updated_fields,
        "description": description,
        "channel_type": channel_type,
        "active_tools": [key for key, value in tools_config.items() if value and value is not False],
        "owner_external_id": owner_external_id,
    }
    blueprint = {
        "agent_name": agent_name,
        "description": description,
        "channel_type": channel_type,
        "tools_config": tools_config,
        "escalation_config": agent.escalation_config if isinstance(agent.escalation_config, dict) else {},
        "updated_fields": updated_fields,
        "context_version": context_version,
        "source": "update_agent_refresh",
    }
    return {
        f"soul:v{context_version}": agent.instructions or "",
        f"agent_blueprint:v{context_version}": json.dumps(blueprint, ensure_ascii=False, indent=2),
        f"setup_summary:v{context_version}": json.dumps(summary, ensure_ascii=False, indent=2),
        "agent_context_version": str(context_version),
    }


async def _refresh_agent_context_memory(
    *,
    db: AsyncSession,
    agent: Agent,
    mode: str,
    updated_fields: list[str],
) -> dict[str, Any]:
    normalized_mode = _normalize_refresh_memory_mode(mode)
    if normalized_mode == "none":
        return {"mode": "none", "updated": False, "keys": []}

    context_version = int((getattr(agent, "version", None) or 1))
    values = _build_refreshed_agent_memory_values(
        agent=agent,
        context_version=context_version,
        mode=normalized_mode,
        updated_fields=updated_fields,
    )

    from app.core.domain.memory_service import upsert_memory

    for key, value in values.items():
        await upsert_memory(agent.id, key, value, db, scope=None)

    return {
        "mode": normalized_mode,
        "updated": True,
        "context_version": context_version,
        "keys": list(values.keys()),
    }


def _append_google_workspace_instruction(instructions: str | None) -> tuple[str, bool]:
    base = (instructions or "").rstrip()
    if "Google Workspace tools aktif" in base or "Google Docs" in base and "Google Drive" in base:
        return base, False
    block = (
        "\n\nKEMAMPUAN GOOGLE WORKSPACE\n"
        "Jika user meminta membuat atau mengedit Google Docs, Google Sheets, Google Drive, Gmail, Calendar, Slides, atau Forms, "
        "gunakan integrasi Google Workspace yang tersedia. Jangan mengatakan tidak punya akses jika integrasi Google aktif. "
        "Untuk laporan riset di Google Docs, lakukan riset terlebih dahulu, susun konten lengkap, lalu buat dokumen Google Docs dan kirim link dokumennya. "
        "Jika akun Google Owner belum terhubung atau perlu izin ulang, jelaskan secara natural bahwa Owner perlu menghubungkan Google lagi dan berikan link otentikasi jika tersedia. "
        "Jangan menyebut istilah teknis internal/protokol tool kepada user."
    )
    return f"{base}{block}" if base else block.strip(), True


def _platform_staff_identity_block(
    *,
    owner_phone: str | None,
    operator_phone: str = "",
    operator_name: str = "",
) -> str:
    owner_id = normalize_phone(owner_phone or "") or str(owner_phone or "").strip() or "Owner platform"
    operator_bits: list[str] = []
    if operator_name.strip():
        operator_bits.append(operator_name.strip())
    if operator_phone.strip():
        operator_bits.append(operator_phone.strip())
    operator_label = " / ".join(operator_bits)

    owner_line = f"Owner agent ini adalah {owner_id}."
    if operator_label and operator_label != owner_id:
        owner_line += f" Operator/admin yang bisa dihubungi: {operator_label}."

    return (
        "IDENTITAS PLATFORM DAN OWNER\n"
        "Kamu adalah staff AI yang dibuat dan dikonfigurasi oleh Arthur, Agent Builder di platform ini.\n"
        f"{owner_line}\n"
        "Owner adalah bos dan superadmin untuk agent ini. Saat Owner memberi arahan, perlakukan itu sebagai instruksi kerja utama selama tidak melanggar keamanan atau kebijakan platform.\n"
        "Jika kamu tidak tahu jawaban, kekurangan data, butuh keputusan manusia, atau ada masalah yang tidak bisa kamu selesaikan sendiri, minta bantuan Owner/operator dengan jujur.\n"
        "Jika kamu butuh akses akun atau integrasi milik Owner seperti Google tetapi akses belum terhubung, expired, atau ditolak, jangan mengarang hasil. Minta Owner menghubungkan atau memberi izin ulang lewat link yang disediakan platform.\n"
        "Saat bicara ke pelanggan akhir, tetap gunakan bahasa sederhana dan jangan menyebut istilah teknis internal."
    )


def _append_platform_staff_identity_instruction(
    instructions: str | None,
    *,
    owner_phone: str | None,
    operator_phone: str = "",
    operator_name: str = "",
) -> tuple[str, bool]:
    base = (instructions or "").rstrip()
    if "IDENTITAS PLATFORM DAN OWNER" in base and "dibuat dan dikonfigurasi oleh Arthur" in base:
        return base, False
    block = _platform_staff_identity_block(
        owner_phone=owner_phone,
        operator_phone=operator_phone,
        operator_name=operator_name,
    )
    return f"{base}\n\n{block}" if base else block, True


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
    faq_keywords = {"faq", "rag", "knowledge base", "pertanyaan umum",
                    "manual", "kebijakan", "katalog", "produk info",
                    "baca dokumen", "upload dokumen", "dokumen referensi"}
    scheduler_keywords = {"reminder", "jadwal", "pengingat", "schedule", "alarm",
                          "kalkulator", "timer", "tanggal", "waktu"}
    social_media_keywords = {"sosmed", "social media", "konten", "content", "instagram", "tiktok",
                              "facebook", "linkedin", "posting", "caption", "content planner",
                              "jadwal konten", "copywriting", "copywriter", "content creator",
                              "social media specialist", "content calendar", "engagement"}
    data_analyst_keywords = {"analisis data", "data analyst", "analyst", "analitik",
                              "dashboard", "grafik", "chart", "excel", "csv", "statistik",
                              "visualisasi", "insight data", "metrics", "kpi", "pandas", "numpy"}
    research_keywords = {"riset", "research", "penelitian", "cari informasi", "kompetitor",
                          "market research", "trend", "analisis pasar", "survei", "literatur",
                          "referensi", "ringkasan artikel", "summarize", "web search"}
    ecommerce_keywords = {"ecommerce", "e-commerce", "marketplace", "toko online", "online shop", "jualan online",
                           "pesanan", "order", "checkout", "produk", "katalog", "katalog online",
                           "shopee", "tokopedia", "lazada", "inventory"}
    personal_assistant_keywords = {"asisten pribadi", "personal assistant", "pa", "sekretaris",
                                    "to-do", "todo", "task", "agenda", "manajemen waktu",
                                    "time management", "kalender", "email", "meeting",
                                    "liburan", "travel", "perjalanan", "itinerary",
                                    "rencana perjalanan", "checklist", "barang bawaan",
                                    "packing", "visa", "paspor", "budget", "h-7", "h-1"}
    hr_keywords = {"hr", "hrd", "rekrutmen", "recruitment", "karyawan", "onboarding",
                   "sdm", "human resource", "interview", "cv", "resume", "absensi",
                   "cuti", "gaji", "payroll", "training", "performa"}

    import re

    goal_words = set(goal_lower.split())

    def has_keyword(kw_set: set) -> bool:
        for kw in kw_set:
            # Word-boundary match to avoid "app" matching "whatsapp", "web" matching "webhook"
            if re.search(r'\b' + re.escape(kw) + r'\b', goal_lower):
                return True
            if kw in features:
                return True
        return False

    def has_data_analyst_signal() -> bool:
        if has_keyword(data_analyst_keywords):
            return True
        # "data" is a generic business word ("data acara", "data pelanggan").
        # Treat it as analyst intent only when paired with analysis/reporting artifacts.
        data_analysis_pairs = (
            r"\bdata\b.{0,32}\b(olah|analisa|analisis|excel|csv|grafik|chart|dashboard|statistik|visualisasi|insight|laporan|report)\b",
            r"\b(olah|analisa|analisis|excel|csv|grafik|chart|dashboard|statistik|visualisasi|insight|laporan|report)\b.{0,32}\bdata\b",
        )
        return any(re.search(pattern, goal_lower) for pattern in data_analysis_pairs)

    def has_ecommerce_signal() -> bool:
        if has_keyword(ecommerce_keywords):
            return True
        # "stok", "harga", and "barang" are common in rentals, services, booking, and logistics.
        # Only treat them as ecommerce when paired with actual store/order/checkout/product language.
        ecommerce_pairs = (
            r"\b(toko|shop|online|marketplace|checkout|order|pesanan|produk|katalog)\b.{0,48}\b(stok|harga|barang|varian|refund|ongkir)\b",
            r"\b(stok|harga|barang|varian|refund|ongkir)\b.{0,48}\b(toko|shop|online|marketplace|checkout|order|pesanan|produk|katalog)\b",
        )
        return any(re.search(pattern, goal_lower) for pattern in ecommerce_pairs)

    if _looks_like_approval_gated_service(goal_lower, " ".join(features), channel):
        return "approval_gated_service_agent"

    if has_keyword(personal_assistant_keywords):
        return "personal_assistant"

    if has_keyword(coding_keywords):
        return "coding_deploy_agent"

    if has_keyword(social_media_keywords):
        return "social_media_agent"

    if has_data_analyst_signal():
        return "data_analyst_agent"

    if has_keyword(research_keywords):
        return "research_agent"

    if has_keyword(hr_keywords):
        return "hr_assistant"

    if has_ecommerce_signal():
        return "ecommerce_cs"

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


def _google_workspace_option(feature_text: str, explicit_google: bool) -> dict[str, Any]:
    text = (feature_text or "").lower()
    app_reasons: list[tuple[str, str]] = []

    def add(app: str, reason: str) -> None:
        if not any(existing == app for existing, _ in app_reasons):
            app_reasons.append((app, reason))

    if any(k in text for k in ("gmail", "email", "inbox", "kirim email", "balas email")):
        add("Gmail", "membaca atau mengirim email dari akun user")
    if any(k in text for k in ("calendar", "kalender", "jadwal", "reminder", "pengingat", "meeting", "deadline", "h-7", "h-1")):
        add("Google Calendar", "membuat jadwal dan pengingat langsung di kalender user")
    if any(k in text for k in ("docs", "google docs", "laporan", "notulen", "proposal", "surat", "itinerary", "checklist")):
        add("Google Docs", "membuat atau memperbarui dokumen yang bisa dibuka user")
    if any(k in text for k in ("sheets", "spreadsheet", "excel", "tabel", "budget", "anggaran", "laporan angka")):
        add("Google Sheets", "menyimpan data, budget, atau tabel dalam spreadsheet")
    if any(k in text for k in ("drive", "file", "folder", "upload", "lampiran", "dokumen referensi")):
        add("Google Drive", "menyimpan dan membaca file dari Drive user")

    should_offer = bool(app_reasons)
    apps = [app for app, _ in app_reasons]
    reasons = [reason for _, reason in app_reasons]
    if explicit_google and not apps:
        apps = ["Google Workspace"]
        reasons = ["menghubungkan agent ke akun Google user"]
        should_offer = True

    if not should_offer:
        return {
            "should_offer": False,
            "enabled": False,
            "suggested_apps": [],
            "reasons": [],
            "user_facing_pitch": "",
            "if_user_declines": "Lanjutkan tanpa integrasi Google.",
        }

    app_text = ", ".join(apps)
    pitch = (
        f"Kebutuhan ini bisa lebih praktis kalau agent terhubung ke {app_text}: "
        f"{'; '.join(reasons)}. Mau saya konekkan ke Google, atau dibuat tanpa Google dulu?"
    )
    if explicit_google:
        pitch = (
            f"Karena kamu sudah minta pakai {app_text}, agent akan saya siapkan dengan integrasi Google. "
            "Nanti kamu tinggal buka link login Google supaya agent bisa akses akunmu."
        )

    return {
        "should_offer": should_offer and not explicit_google,
        "enabled": explicit_google,
        "suggested_apps": apps,
        "reasons": reasons,
        "user_facing_pitch": pitch,
        "if_user_accepts": "Panggil plan_agent lagi dengan requested_features memuat google, lalu create/update dengan integrasi Google aktif.",
        "if_user_declines": "Lanjutkan tanpa integrasi Google; agent tetap bisa berjalan dengan memory/reminder internal sesuai tools yang tersedia.",
    }


def _negates_google_workspace(text: str) -> bool:
    lowered = (text or "").lower()
    patterns = (
        r"\b(tanpa|jangan|tidak|ga|gak|nggak|enggak|belum|nanti)\b.{0,32}\b(google|workspace|gmail|calendar|drive|docs|sheets)\b",
        r"\b(google|workspace|gmail|calendar|drive|docs|sheets)\b.{0,32}\b(tanpa|jangan|tidak|ga|gak|nggak|enggak|belum|nanti)\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _get_post_create_steps(preset_id: str, channel: str, tc: dict) -> list[str]:
    """Return required actions user/operator must take after agent creation."""
    steps = []
    if channel == "whatsapp" or tc.get("whatsapp_media"):
        steps.append("Kirim QR ke user: gunakan send_agent_wa_qr(agent_id, caption, phone)")
        steps.append("Tunggu user scan QR, lalu cek ulang dengan send_agent_wa_qr jika butuh QR baru")
    if tc.get("rag"):
        steps.append("Upload dokumen: POST /v1/agents/{id}/documents/upload (PDF/DOCX/TXT)")
    if preset_id == "coding_deploy_agent":
        steps.append("Pastikan Docker socket tersedia di server sebelum test deploy")
    return steps


_INSTRUCTION_WRITER_MODEL = "deepseek/deepseek-v4-pro"
# Soul writing is structured text — doesn't need heavy reasoning, use fast model
_SOUL_WRITER_MODEL = "openai/gpt-4o-mini"
_BLUEPRINT_WRITER_MODEL = "deepseek/deepseek-v4-pro"


def _find_unfilled_placeholders(text: str) -> list[str]:
    """Find only real template placeholders, not examples like [instruksi]."""
    if not text:
        return []
    patterns = [
        r"\{(?:name|role|business|business_info|tasks|persona|escalation|extra_rules|agent_name|operator_phone)\}",
        r"\[(?:xxx|nama|nama [^\]]+|bisnis|produk|harga|operator|isi [^\]]+|contoh [^\]]+)\]",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return found


def _combined_context_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).lower()


_GENERIC_PAYMENT_APPROVAL_FALLBACK_TEXTS = (
    "kasus membutuhkan keputusan, akses, pembayaran, atau persetujuan manusia",
    "kasus membutuhkan keputusan, akses, pembayaran, atau persetujuan manusia",
    "blueprint fallback dibuat karena output json generator tidak bisa dipulihkan",
)


def _payment_workflow_detection_text(*parts: Any) -> str:
    text = _combined_context_text(*parts)
    for marker in _GENERIC_PAYMENT_APPROVAL_FALLBACK_TEXTS:
        text = text.replace(marker, " ")
    return text


def _looks_like_approval_gated_service(*parts: Any) -> bool:
    text = _payment_workflow_detection_text(*parts)
    service_markers = (
        "jasa",
        "layanan",
        "service",
        "order",
        "pesanan",
        "fulfillment",
        "hasil",
        "deliverable",
        "produk digital",
        "revisi",
        "bikin cv",
        "buat cv",
        "cv ats",
        "jasa cv",
        "pembuatan cv",
        "resume ats",
        "dokumen",
        "file",
        "pdf",
        "report",
        "laporan",
    )
    payment_markers = (
        "bayar",
        "pembayaran",
        "payment",
        "transfer",
        "tf",
        "bukti transfer",
        "bukti tf",
        "bukti bayar",
        "cek pembayaran",
        "review pembayaran",
    )
    approval_gate_markers = (
        "bukti transfer",
        "bukti tf",
        "bukti bayar",
        "cek pembayaran",
        "review pembayaran",
        "admin approve",
        "admin approval",
        "operator approve",
        "operator approval",
        "pembayaran disetujui",
        "pembayaran diapprove",
        "setelah pembayaran disetujui",
        "setelah admin approve",
        "jangan lanjut sebelum approve",
        "jangan kirim sebelum approved",
    )
    return (
        any(marker in text for marker in service_markers)
        and any(marker in text for marker in payment_markers)
        and any(marker in text for marker in approval_gate_markers)
    )


def _looks_like_file_delivery_workflow(*parts: Any) -> bool:
    text = _combined_context_text(*parts)
    file_markers = (
        "file final",
        "hasil berupa file",
        "output file",
        "pdf",
        "docx",
        "excel",
        "xlsx",
        "csv",
        "dokumen final",
        "kirim dokumen",
        "kirim file",
        "send_whatsapp_document",
        "cv dikirim",
        "kirim cv",
        "laporan final",
        "report final",
    )
    return any(marker in text for marker in file_markers)


def _looks_like_generated_file_workflow(*parts: Any) -> bool:
    text = _combined_context_text(*parts)
    generation_markers = (
        "bikin",
        "buat",
        "generate",
        "susun",
        "render",
        "export",
        "draft",
        "cv ats",
        "resume ats",
        "laporan",
        "report",
        "proposal",
        "dokumen final",
    )
    return _looks_like_file_delivery_workflow(text) and any(marker in text for marker in generation_markers)


def _looks_like_payment_approval_workflow(*parts: Any) -> bool:
    text = _payment_workflow_detection_text(*parts)
    payment = any(marker in text for marker in ("bayar", "pembayaran", "payment", "transfer", "tf", "bukti transfer", "bukti tf", "bukti bayar"))
    payment_proof = any(marker in text for marker in ("bukti transfer", "bukti tf", "bukti bayar", "cek pembayaran", "review pembayaran"))
    approval = any(
        marker in text
        for marker in (
            "admin approve",
            "admin approval",
            "operator approve",
            "operator approval",
            "approve",
            "approved",
            "acc",
            "disetujui",
        )
    )
    if payment_proof:
        return True
    return payment and approval


def _has_approval_state_contract(text: str) -> bool:
    lowered = (text or "").lower()
    required = ("intake", "waiting_payment", "payment_review", "approved", "delivery", "aftercare")
    return all(state in lowered for state in required)


def _business_context_has_explicit_name(context: str | None) -> bool:
    raw = str(context or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if re.search(r"\b(nama|brand|merek)\s+(bisnis|usaha|toko|brand|merek)?\s*(saya|kami|ini)?\s*(adalah|namanya|:)", lowered):
        return True
    if re.search(r"\b(bisnis|usaha|toko|perusahaan|restoran|cafe|kafe|warung|klinik|salon|laundry|bengkel)\s+(saya|kami|ini)\s+(bernama|namanya|adalah|:)", lowered):
        return True
    if re.search(r"\b(PT|CV|Toko|Cafe|Kafe|Restoran|Warung|Klinik|Salon|Laundry|Bengkel)\s+[A-Z][A-Za-z0-9&.' -]{2,40}", raw):
        return True
    return False


def _sanitize_unverified_business_name(
    text: str,
    *,
    business_context: str | None,
) -> tuple[str, bool]:
    """Replace likely model-invented brand names when the owner did not provide one."""
    if _business_context_has_explicit_name(business_context):
        return text, False

    sanitized = str(text or "")
    patterns = (
        (
            r"(Kamu adalah [^.\n]{0,120}?\bdari\s+)([A-Z][A-Za-z0-9&.' -]{2,40})(?=,|\.|\n|\s+yang\b|\s+jasa\b|\s+layanan\b)",
            r"\1bisnis ini",
        ),
        (
            r"(\bPeran:\s*[^\n]{0,80}?\bdari\s+)([A-Z][A-Z0-9&.' -]{2,40})(?=\n|$)",
            r"\1BISNIS INI",
        ),
        (
            r"(\bCS\s+)([A-Z][A-Za-z0-9&.' -]{2,40})(?=,|\.|\n)",
            r"\1bisnis ini",
        ),
    )
    for pattern, repl in patterns:
        sanitized = re.sub(pattern, repl, sanitized)
    return sanitized, sanitized != text


def _subagents_enabled(tools_config: dict[str, Any]) -> bool:
    subagents_cfg = tools_config.get("subagents", {})
    return bool(subagents_cfg.get("enabled") if isinstance(subagents_cfg, dict) else subagents_cfg)


def file_delivery_contract_issues(instructions: str, *, file_delivery: bool) -> list[str]:
    """Validasi kontrak parent-delivery untuk agent yang menghasilkan file.
    Kontrak benar: subagent tulis ke /workspace/shared, return SIAP_DIKIRIM_PARENT,
    subagent tidak kirim WA, parent yang memanggil media-send."""
    if not file_delivery:
        return []
    text = (instructions or "").lower()
    issues: list[str] = []
    if "/workspace/shared" not in text:
        issues.append("Instruksi file harus menyuruh subagent menyimpan ke /workspace/shared/<file>.")
    if "siap_dikirim_parent" not in text:
        issues.append("Instruksi file harus mewajibkan subagent return penanda SIAP_DIKIRIM_PARENT.")
    parent_sends = ("send_whatsapp_document" in text) or ("send_whatsapp_image" in text)
    if not parent_sends:
        issues.append("Instruksi harus menyebut parent memanggil send_whatsapp_document/send_whatsapp_image setelah artifact kembali.")
    return issues


def _critical_workflow_config_errors(
    *,
    name: str = "",
    description: str = "",
    instructions: str = "",
    tools_config: dict[str, Any] | str | None = None,
    soul: str = "",
    blueprint: str = "",
    preset_id: str = "",
) -> list[str]:
    if isinstance(tools_config, str):
        try:
            tc = json.loads(tools_config) if tools_config.strip() else {}
        except json.JSONDecodeError:
            tc = {}
    else:
        tc = dict(tools_config or {})

    context_parts = (name, description, instructions, json.dumps(tc, ensure_ascii=False), soul, blueprint, preset_id)
    approval_gated_service = _looks_like_approval_gated_service(*context_parts)
    payment_approval_workflow = (
        _looks_like_payment_approval_workflow(*context_parts)
        or preset_id == "approval_gated_service_agent"
        or approval_gated_service
    )
    file_delivery_workflow = _looks_like_file_delivery_workflow(*context_parts)
    generated_file_workflow = _looks_like_generated_file_workflow(*context_parts)

    errors: list[str] = []
    if payment_approval_workflow:
        if len((instructions or "").strip()) < 1200:
            errors.append("Instructions terlalu pendek untuk workflow pembayaran/admin approval.")
        if not _has_approval_state_contract(instructions):
            errors.append(
                "Instructions wajib memuat state intake, waiting_payment, payment_review, approved, delivery, dan aftercare."
            )
        if not tc.get("escalation"):
            errors.append("Workflow pembayaran/admin approval wajib escalation=true.")
        if "escalate_to_human" not in (instructions or ""):
            errors.append("Instructions wajib menyebut escalate_to_human untuk bukti transfer/admin approval.")
    if file_delivery_workflow:
        if not tc.get("whatsapp_media"):
            errors.append("Workflow delivery file wajib whatsapp_media=true.")
        errors.extend(file_delivery_contract_issues(instructions or "", file_delivery=True))
    if generated_file_workflow and (not tc.get("sandbox") or not _subagents_enabled(tc)):
        errors.append("Workflow pembuatan file final wajib sandbox=true dan subagents.enabled=true.")
    return errors


def _looks_like_destructive_instruction_shrink(
    current_instructions: str | None,
    new_instructions: str,
) -> bool:
    """Reject accidental summary-only overwrites of established agent prompts."""
    current_len = len(current_instructions or "")
    new_len = len(new_instructions or "")
    if current_len < 1000:
        return False
    minimum_len = max(500, int(current_len * 0.35))
    return new_len < minimum_len


def _parse_json_arg(value: Any, default: Any, *, expected: type | tuple[type, ...]) -> tuple[Any, str | None]:
    """Accept tool-call args as already-parsed objects or JSON strings."""
    if value is None or value == "":
        return default, None
    if isinstance(value, expected):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            try:
                import ast

                parsed = ast.literal_eval(value)
            except Exception:
                return default, str(exc)
        if isinstance(parsed, expected):
            return parsed, None
        return default, f"expected {expected}, got {type(parsed).__name__}"
    return default, f"expected JSON string or {expected}, got {type(value).__name__}"


def _strip_json_wrapper(raw: str) -> str:
    """Remove common LLM wrappers before parsing a JSON object."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def _extract_balanced_json_object(raw: str) -> str:
    """Extract the first balanced JSON object, even when the JSON is not fully valid."""
    text = _strip_json_wrapper(raw)
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model output")

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]

    return text[start:]


def _repair_llm_json_text(text: str) -> str:
    """Repair conservative JSON mistakes common in model output."""
    repaired = text.strip().lstrip("\ufeff")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    # Missing comma between object fields:
    # {"a": "x"\n "b": "y"} -> {"a": "x",\n "b": "y"}
    repaired = re.sub(
        r'(?<=[}\]"0-9eE])\s*\n\s*(?="[^"\n]+"\s*:)',
        ",\n",
        repaired,
    )
    for literal in ("true", "false", "null"):
        repaired = re.sub(
            rf'(?<={literal})\s*\n\s*(?="[^"\n]+"\s*:)',
            ",\n",
            repaired,
        )

    # Missing comma between array values, especially object/string entries.
    repaired = re.sub(r'(?<=[}\]"])\s*\n\s*(?=\{)', ",\n", repaired)
    repaired = re.sub(r'(?<=")\s*\n\s*(?=")', ",\n", repaired)
    return repaired


def _complete_truncated_json(text: str) -> str:
    """Best-effort completion of JSON truncated mid-output (e.g. token limit).

    Closes an open string, drops a dangling trailing comma/key, and balances
    any still-open objects/arrays so json.loads can recover the partial blueprint.
    """
    # Per-open-object state:
    #   'key'      awaiting a key (object start or right after a comma)
    #   'afterkey' key string done, awaiting ':'
    #   'colon'    ':' seen, awaiting a value
    #   'value'    a value is present / complete
    stack: list[str] = []          # '{' or '[' currently open
    obj_state: list[str] = []
    in_string = False
    escaped = False

    def _saw_value() -> None:
        if stack and stack[-1] == "{" and obj_state and obj_state[-1] == "colon":
            obj_state[-1] = "value"

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                if stack and stack[-1] == "{" and obj_state and obj_state[-1] == "key":
                    obj_state[-1] = "afterkey"
                else:
                    _saw_value()
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            _saw_value()
            stack.append("{")
            obj_state.append("key")
        elif char == "[":
            _saw_value()
            stack.append("[")
        elif char in "}]":
            if stack:
                if stack.pop() == "{" and obj_state:
                    obj_state.pop()
                _saw_value()
        elif char == ":":
            if obj_state and stack and stack[-1] == "{" and obj_state[-1] == "afterkey":
                obj_state[-1] = "colon"
        elif char == ",":
            if obj_state and stack and stack[-1] == "{":
                obj_state[-1] = "key"
        elif char not in " \t\r\n":
            # Start of a bare value (number, true/false/null).
            _saw_value()

    result = text
    if in_string:
        result += '"'
        # An open string is a complete value (or key) once closed.
        if stack and stack[-1] == "{" and obj_state and obj_state[-1] == "key":
            obj_state[-1] = "afterkey"
        else:
            _saw_value()
    result = result.rstrip()

    # Complete a truncated bare literal (e.g. "tru" -> "true").
    literal_match = re.search(r"(?<![\w.])(t|tr|tru|f|fa|fal|fals|n|nu|nul)$", result)
    if literal_match:
        frag = literal_match.group(1)
        for full in ("true", "false", "null"):
            if full.startswith(frag):
                result = result[: literal_match.start(1)] + full
                break

    while stack:
        opener = stack.pop()
        if opener == "[":
            result = re.sub(r",\s*$", "", result.rstrip())
            result += "]"
            continue
        state = obj_state.pop() if obj_state else "value"
        result = result.rstrip()
        if state == "key" and result.endswith(","):
            # Trailing comma with no following member: the prior member is complete.
            result = re.sub(r",\s*$", "", result)
        elif state in ("key", "afterkey"):
            # Dangling key (no colon/value): drop it.
            result = re.sub(r'\s*"(?:[^"\\]|\\.)*"\s*$', "", result)
            result = re.sub(r",\s*$", "", result.rstrip())
        elif state == "colon":
            # Key + colon but no value yet: fill a null so the object parses.
            result += " null"
        result += "}"
    return result


def _parse_llm_json_object(raw: str) -> tuple[dict[str, Any], bool]:
    """Parse model JSON with a small deterministic repair pass."""
    candidate = _extract_balanced_json_object(raw)
    try:
        parsed = json.loads(candidate)
        repaired = False
    except json.JSONDecodeError:
        repaired_text = _repair_llm_json_text(candidate)
        try:
            parsed = json.loads(repaired_text)
        except json.JSONDecodeError:
            # Output was cut off (e.g. token limit) — recover the partial object.
            repaired_text = _complete_truncated_json(repaired_text)
            parsed = json.loads(repaired_text)
        repaired = repaired_text != candidate

    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")
    return parsed, repaired


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


_SOUL_TEMPLATES: dict[str, str] = {
    "cs_whatsapp_basic": """\
IDENTITAS
Nama: {name}
Peran: {role} dari {business}

KEPRIBADIAN
{persona}. Ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai tapi sopan. Gunakan sapaan yang hangat. Pesan maks 2-3 kalimat — singkat dan to the point. JANGAN pakai markdown (*, #, **).

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
Contoh: task(name="sys_coder", task="Buat landing page vanilla HTML/CSS/JS terpisah dengan judul 'Halo Dunia', tanpa framework/inline CSS/JS, deploy, kembalikan URL")

sys_coder menangani:
- Menulis semua file kode ke workspace
- Mengecek dan menjalankan deployment
- Mendapatkan URL publik yang bisa diakses

ATURAN WEB RINGAN
- Untuk website/web app/frontend/landing page/portfolio/dashboard prototype, instruksikan sys_coder memakai vanilla HTML/CSS/JavaScript saja.
- File wajib terpisah: index.html, styles.css, script.js jika butuh interaksi.
- Jangan pakai inline CSS/JS.
- Jangan pakai React, Next.js, Vue, Svelte, Astro, Tailwind, Bootstrap, Vite, npm/npx, CDN library, atau framework/package frontend lain.
- Tujuan aturan ini: task lebih cepat, sandbox lebih ringan, dan deploy cukup pakai python http.server.

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

DELIVERY FILE DARI SUB-AGENT HARUS LEWAT PARENT
- Sub-agent TIDAK boleh kirim file WhatsApp langsung. Sub-agent membuat file di /workspace/shared/.
- Kalau output task() menyebut path /workspace/shared/<filename> atau SIAP_DIKIRIM_PARENT → parent WAJIB panggil send_whatsapp_document/send_whatsapp_image sendiri.
- JANGAN tanya "mau saya kirim lagi?", "udah nyampe?", "file-nya udah ada?", atau "bisa dibuka?" sebelum parent mencoba tool send.
- JANGAN balas final sebelum tool parent send_whatsapp_document/send_whatsapp_image sukses atau mengembalikan error nyata.
- Setelah tool parent sukses, simpan info ini ke memory: remember(key="last_file_sent", value="<nama_file> — TERKIRIM via parent")

INGAT HASIL DEPLOY DAN FILE — JANGAN BIKIN ULANG
- Setiap kali sys_coder return URL, LANGSUNG simpan ke memory:
  remember(key="last_deploy_url", value="<url>")
  remember(key="last_deploy_summary", value="<deskripsi singkat web yang dibuat>")
- Setiap kali parent berhasil kirim file dari hasil sub-agent, LANGSUNG simpan ke memory:
  remember(key="last_file_sent", value="<nama_file> dikirim <tanggal/waktu>")
- Sebelum delegasi ulang ke sys_coder, WAJIB recall("last_deploy_url") dulu.
- Kalau user nanya status ("udah jadi?", "mana webnya?", "URL-nya apa?") → JANGAN delegasi ulang.
  Cukup recall("last_deploy_url") dan kirim URL-nya ke user.
- Kalau user nanya "udah dikirim?", "mana filenya?" → recall("last_file_sent") dulu sebelum buat ulang.
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
{persona}. Ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya informatif dan ringkas. Jawab berdasarkan dokumen — jangan karang sendiri.

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
{persona}. Ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai. Selalu konfirmasi ulang detail reminder sebelum set.

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


async def _call_instruction_writer(
    prompt: str,
    system: str,
    model: str | None = None,
    *,
    max_tokens: int = 1500,
    temperature: float = 0.5,
    json_mode: bool = False,
) -> str:
    """Call LLM via OpenRouter for instruction/soul writing."""
    settings = get_settings()
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    create_kwargs: dict[str, Any] = dict(
        model=model or _INSTRUCTION_WRITER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if json_mode:
        # Force a JSON object so the model can't wrap the blueprint in prose.
        # Some models reject response_format; fall back to a plain call.
        try:
            async with asyncio.timeout(45):
                response = await client.chat.completions.create(
                    **create_kwargs,
                    response_format={"type": "json_object"},
                )
        except asyncio.TimeoutError:
            raise
        except Exception:
            async with asyncio.timeout(45):
                response = await client.chat.completions.create(**create_kwargs)
    else:
        async with asyncio.timeout(45):
            response = await client.chat.completions.create(**create_kwargs)
    content = response.choices[0].message.content or ""
    # Strip reasoning/thinking tags
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content


async def _compose_semantic_operating_manual_from_context(
    *,
    name: str,
    description: str,
    instructions: str,
    business_context: str,
    domain: str,
    channel_type: str,
    tools_config: dict[str, Any],
) -> dict[str, Any] | None:
    """Ask the writer to infer a SOP semantically when Arthur skipped manual/blueprint."""
    context = "\n".join(
        part
        for part in (
            f"agent_name: {name}" if name else "",
            f"description: {description}" if description else "",
            f"business_context: {business_context}" if business_context else "",
            f"instructions: {instructions}" if instructions else "",
        )
        if part.strip()
    )
    if not context.strip():
        return None

    system_msg = (
        "Kamu adalah senior operations designer untuk AI agent bisnis. "
        "Tugasmu menyusun Agent Operating Manual/SOP dari makna konteks user, bukan dari pencocokan keyword. "
        "Baca niat bisnis, aktor yang terlibat, data yang perlu dikumpulkan, keputusan yang harus ditahan untuk manusia, "
        "risiko salah janji, dan definisi selesai. "
        "Jangan bergantung pada nama domain atau kata kunci eksplisit; kalau user menjelaskan alur dengan bahasa sehari-hari, infer workflow dari alur itu. "
        "Return HANYA JSON valid, tanpa markdown dan tanpa penjelasan di luar JSON."
    )
    user_msg = (
        "Susun Agent Operating Manual/SOP dari konteks ini.\n\n"
        f"{context}\n"
        f"domain_hint: {domain or '-'}\n"
        f"channel_type: {channel_type or '-'}\n"
        f"tools_config: {json.dumps(tools_config, ensure_ascii=False)}\n\n"
        "Schema JSON wajib:\n"
        "{\n"
        '  "manual_id": "agent_operating_manual",\n'
        '  "version": 1,\n'
        '  "source": "arthur_operating_manual_writer_auto",\n'
        '  "domain": "domain hasil inferensi semantik",\n'
        '  "domain_confidence": "high|medium|low",\n'
        '  "maturity": "usable|needs_review|draft",\n'
        '  "owner_review_required": false,\n'
        '  "missing_context": ["hanya data kritis yang benar-benar belum ada"],\n'
        '  "assumptions": ["asumsi operasional"],\n'
        '  "workflows": [{\n'
        '    "workflow_id": "snake_case_id",\n'
        '    "name": "Nama workflow",\n'
        '    "trigger": "Kapan workflow dimulai",\n'
        '    "goal": "Tujuan workflow",\n'
        '    "required_inputs": ["data wajib"],\n'
        '    "steps": ["langkah konkret berurutan"],\n'
        '    "decision_points": ["kondisi dan keputusan"],\n'
        '    "allowed_tools": ["tool relevan"],\n'
        '    "escalation_rules": ["kapan harus eskalasi ke manusia"],\n'
        '    "prohibited_actions": ["hal yang tidak boleh dilakukan"],\n'
        '    "final_output": "Definisi selesai yang nyata",\n'
        '    "examples": ["contoh pendek jika perlu"]\n'
        "  }],\n"
        '  "knowledge_plan": {"must_have": ["..."], "nice_to_have": ["..."], "needs_upload": false},\n'
        '  "memory_plan": [{"key": "...", "value_to_store": "..."}],\n'
        '  "validation_checklist": ["..."]\n'
        "}\n\n"
        "Aturan kualitas:\n"
        "- Buat SOP spesifik dari alur user, bukan SOP generic customer service.\n"
        "- Untuk agent yang bicara dengan customer, minimal ada intake data, boundary keputusan manusia, dan follow-up/closing.\n"
        "- Jika ada harga final, ketersediaan, booking, pembayaran, refund, komplain, atau approval, agent wajib berhenti dan eskalasi sebelum menjanjikan keputusan.\n"
        "- Jika konteks cukup untuk bekerja aman, set maturity usable. Jangan needs_review hanya karena daftar harga/detail lengkap belum diberikan."
    )
    try:
        raw = await _call_instruction_writer(
            user_msg,
            system_msg,
            model=_BLUEPRINT_WRITER_MODEL,
            max_tokens=7000,
            temperature=0.1,
            json_mode=True,
        )
        manual, _ = _parse_llm_json_object(raw)
    except Exception as exc:
        logger.warning(
            "builder_tools.semantic_operating_manual.failed",
            error=str(exc),
            agent_name=name,
            domain=domain,
        )
        return None

    if not isinstance(manual.get("workflows"), list) or not manual["workflows"]:
        logger.warning(
            "builder_tools.semantic_operating_manual.empty_workflows",
            agent_name=name,
            domain=domain,
        )
        return None
    manual.setdefault("manual_id", "agent_operating_manual")
    manual.setdefault("version", 1)
    manual.setdefault("source", "arthur_operating_manual_writer_auto")
    manual.setdefault("domain", domain or "semantic_business_workflow")
    manual.setdefault("domain_confidence", "medium")
    manual.setdefault("maturity", "usable")
    manual.setdefault("owner_review_required", manual.get("maturity") in {"draft", "needs_review"})
    manual.setdefault("missing_context", [])
    manual.setdefault("assumptions", [])
    manual.setdefault("knowledge_plan", {"must_have": [], "nice_to_have": [], "needs_upload": bool(tools_config.get("rag"))})
    manual.setdefault("memory_plan", [])
    manual.setdefault("validation_checklist", [])
    return manual


def build_builder_tools(
    db_factory: async_sessionmaker,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    device_id: str = "",
    default_target: str = "",
) -> list:
    """
    Build semua builder tools untuk system agent.

    Args:
        db_factory: async_sessionmaker factory — each tool call opens its own session
        owner_phone: external_user_id (nomor WA/JID) dari pengguna yang chat dengan Arthur.
        self_agent_id: UUID agent ini sendiri (Arthur) — untuk self-modification.
        device_id/default_target: konteks WhatsApp saat Arthur dipanggil dari WA.
    """

    async def _preview_agent_creation_entitlement(
        *,
        tools_config: dict[str, Any],
        model: str,
        channel_type: str | None,
    ) -> dict[str, Any]:
        """Check owner tier/slot before Arthur invests in creating the agent."""
        target_phone = _best_owner_identifier(owner_phone, default_target)
        if not target_phone:
            return {
                "checked": True,
                "allowed": False,
                "reason": "owner_external_id tidak tersedia.",
                "user_message": (
                    "Saya belum bisa cek paket kamu karena nomor/owner sesi ini belum terbaca. "
                    "Kirim dari session WhatsApp user yang valid dulu."
                ),
            }

        if not hasattr(Agent, "__table__"):
            return {
                "checked": False,
                "allowed": True,
                "reason": "agent_model_unavailable",
            }

        try:
            from app.core.domain.subscription_service import (
                check_can_create_agent,
                get_or_create_wa_user,
                get_subscription_by_external_id,
                validate_agent_entitlements,
            )

            async with db_factory() as db:
                sub_details = None
                if _is_probable_lid(target_phone):
                    sub_details = await get_subscription_by_external_id(target_phone, db)
                    if sub_details is None:
                        return {
                            "checked": True,
                            "allowed": False,
                            "owner": target_phone,
                            "identifier_type": "lid",
                            "reason": "nomor WhatsApp asli belum tersedia.",
                            "user_message": (
                                "Saya belum bisa cek paket kamu karena yang terbaca masih ID WhatsApp internal, "
                                "bukan nomor asli. Kirim pesan dari nomor yang sudah terhubung dulu."
                            ),
                        }
                else:
                    await get_or_create_wa_user(target_phone, db)
                    await db.commit()

                create_check = await check_can_create_agent(target_phone, db)
                if not create_check.get("allowed"):
                    return {
                        "checked": True,
                        "allowed": False,
                        "owner": target_phone,
                        "reason": create_check.get("reason") or "Plan tidak mengizinkan agent baru.",
                        "user_message": create_check.get("reason") or "Paket kamu belum bisa membuat agent baru.",
                        "plan": create_check.get("plan"),
                        "agents_used": create_check.get("agents_used"),
                        "agents_limit": create_check.get("max_agents"),
                    }

                if sub_details is None:
                    sub_details = await get_subscription_by_external_id(target_phone, db)
                if sub_details is None:
                    return {
                        "checked": True,
                        "allowed": False,
                        "owner": target_phone,
                        "reason": "Subscription tidak ditemukan.",
                        "user_message": "Subscription kamu belum ditemukan, jadi agent belum bisa dibuat.",
                    }

                _, sub, plan = sub_details
                entitlement_errors = validate_agent_entitlements(
                    plan,
                    model=model,
                    tools_config=tools_config,
                    channel_type=channel_type or None,
                )
                if entitlement_errors:
                    return {
                        "checked": True,
                        "allowed": False,
                        "owner": target_phone,
                        "reason": "Konfigurasi agent melebihi entitlement plan.",
                        "user_message": "Paket kamu belum mendukung konfigurasi agent ini.",
                        "plan": getattr(plan, "label", None),
                        "plan_code": getattr(plan, "code", None),
                        "violations": entitlement_errors,
                        "agents_used": create_check.get("agents_used"),
                        "agents_limit": create_check.get("max_agents"),
                    }

                return {
                    "checked": True,
                    "allowed": True,
                    "owner": target_phone,
                    "plan": getattr(plan, "label", None),
                    "plan_code": getattr(plan, "code", None),
                    "agents_used": create_check.get("agents_used"),
                    "agents_limit": create_check.get("max_agents"),
                    "tokens_remaining": getattr(sub, "tokens_remaining", None),
                    "expires_at": create_check.get("expires_at"),
                }
        except Exception as exc:
            logger.warning(
                "builder_tools.plan_agent.entitlement_preview_failed",
                owner_phone=target_phone,
                error=str(exc),
            )
            return {
                "checked": False,
                "allowed": True,
                "owner": target_phone,
                "reason": "entitlement_check_unavailable",
                "detail": str(exc),
            }

    # ------------------------------------------------------------------ #
    # 0. get_self_config                                                  #
    # ------------------------------------------------------------------ #

    @tool
    async def get_self_config() -> str:
        """
        Dapatkan identitas dan kredensial agent builder ini sendiri (Arthur).
        Gunakan untuk mendapatkan agent_id dan konfigurasi agar bisa mengupdate
        diri sendiri lewat update_agent(), tanpa memanggil API platform.
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
    # compose_agent_blueprint                                             #
    # ------------------------------------------------------------------ #

    @tool
    async def compose_agent_blueprint(
        preset_id: str,
        user_goal: str,
        agent_name: str = "",
        business_context: str = "",
        target_users: str = "",
        channel: str = "webchat",
        requested_features: str = "",
        known_constraints: str = "",
    ) -> str:
        """
        Rancang blueprint agent yang spesifik untuk kebutuhan user sebelum menulis instructions.

        Blueprint berisi workflow, data yang wajib dikumpulkan, knowledge yang dibutuhkan,
        aturan eskalasi, tool plan, dan checklist validasi. Gunakan ini untuk agent bisnis
        yang butuh SOP/custom workflow, terutama CS, ecommerce, HR, data, dan personal assistant.
        Jangan tampilkan blueprint ke user untuk minta approval mikro. Setelah tool ini sukses,
        lanjutkan langsung ke compose_agent_instructions kecuali ada data kritis yang benar-benar
        tidak bisa diinfer dari pesan, dokumen, atau konteks percakapan.

        Args:
            preset_id: Preset yang dipilih dari plan_agent
            user_goal: Tujuan utama user
            agent_name: Nama agent jika sudah ada
            business_context: Detail bisnis/produk/SOP yang user sudah jelaskan
            target_users: Siapa yang akan ngobrol dengan agent ini
            channel: whatsapp, webchat, atau channel lain
            requested_features: Fitur yang diminta user, dipisah koma
            known_constraints: Batasan penting, compliance, gaya komunikasi, atau larangan
        """
        preset = AGENT_PRESETS.get(preset_id, {})
        tc = preset.get("tools_config", {})

        system_msg = (
            "Kamu adalah solution architect untuk AI agent bisnis. "
            "Tugasmu membuat blueprint yang operasional, spesifik, dan tidak generik. "
            "Rancang agent seperti pekerja manusia sungguhan: punya role, SOP, state kerja, data wajib, batas wewenang, "
            "handoff manusia, dan kriteria selesai yang terukur. "
            "Untuk agent bisnis/jasa, wajib pikirkan alur pembayaran, approval manusia, deliverable, dan after-sales jika relevan. "
            "Return HANYA JSON valid, tanpa markdown dan tanpa penjelasan di luar JSON. "
            "Pakai double quote, koma antar-field yang valid, tanpa trailing comma, dan jangan potong objek JSON."
        )
        user_msg = (
            "Buat Agent Blueprint dari data berikut.\n\n"
            f"preset_id: {preset_id}\n"
            f"preset_label: {preset.get('label', 'Custom')}\n"
            f"agent_name: {agent_name or 'belum ditentukan'}\n"
            f"user_goal: {user_goal}\n"
            f"business_context: {business_context or 'belum ada detail bisnis'}\n"
            f"target_users: {target_users or 'belum jelas'}\n"
            f"channel: {channel}\n"
            f"requested_features: {requested_features or '-'}\n"
            f"known_constraints: {known_constraints or '-'}\n"
            f"available_tools_config: {json.dumps(tc, ensure_ascii=False)}\n\n"
            "Schema JSON wajib:\n"
            "{\n"
            '  "agent_summary": "...",\n'
            '  "assumptions": ["..."],\n'
            '  "workflow_steps": [{"step": 1, "name": "...", "agent_action": "...", "required_user_data": ["..."], "success_criteria": "..."}],\n'
            '  "knowledge_plan": {"must_have": ["..."], "nice_to_have": ["..."], "needs_upload": true},\n'
            '  "tool_plan": [{"tool": "...", "why": "...", "when_to_use": "..."}],\n'
            '  "memory_plan": [{"key": "...", "value_to_store": "..."}],\n'
            '  "state_plan": [{"state": "...", "entry_condition": "...", "allowed_actions": ["..."], "exit_condition": "..."}],\n'
            '  "human_approval_points": [{"when": "...", "operator_action": "...", "agent_next_action": "..."}],\n'
            '  "escalation_rules": [{"condition": "...", "action": "..."}],\n'
            '  "conversation_examples_needed": ["..."],\n'
            '  "validation_checklist": ["..."],\n'
            '  "missing_info_questions": ["maks 3 pertanyaan paling penting jika data belum cukup"]\n'
            "}\n\n"
            "Pastikan workflow berbeda untuk tiap konteks bisnis. Jangan isi generik seperti 'jawab pertanyaan user' saja. "
            "Jika ada pembayaran/approval/deliverable, state_plan harus memuat minimal: intake, waiting_payment, payment_review, approved, delivery, aftercare. "
            "Jika tidak relevan, buat state_plan yang sesuai preset dan tujuan user."
        )

        def _fallback_response(parse_status: str) -> str:
            fallback = _fallback_agent_blueprint(
                preset_id=preset_id,
                user_goal=user_goal,
                agent_name=agent_name,
                business_context=business_context,
                target_users=target_users,
                channel=channel,
                requested_features=requested_features,
                known_constraints=known_constraints,
                tools_config=tc,
            )
            return json.dumps({
                "blueprint": fallback,
                "parse_status": parse_status,
                "next_step": "Gunakan blueprint fallback ini untuk compose_agent_instructions jika konteks user sudah cukup; jangan minta approval mikro.",
            }, ensure_ascii=False, indent=2)

        try:
            raw = await _call_instruction_writer(
                user_msg,
                system_msg,
                model=_BLUEPRINT_WRITER_MODEL,
                max_tokens=6000,
                temperature=0.2,
                json_mode=True,
            )
        except Exception as exc:
            logger.error("builder_tools.compose_agent_blueprint.error", error=str(exc))
            return _fallback_response("deterministic_fallback")

        try:
            blueprint, repaired_json = _parse_llm_json_object(raw)
        except Exception as exc:
            logger.warning(
                "builder_tools.compose_agent_blueprint.parse_failed",
                error=str(exc),
                preset_id=preset_id,
                agent_name=agent_name,
                output_preview=(raw or "")[:240],
            )
            return _fallback_response("deterministic_fallback")

        payload = {
            "blueprint": blueprint,
            "next_step": (
                "Gunakan blueprint ini sebagai agent_blueprint saat compose_agent_instructions. "
                "Jangan minta user menyetujui blueprint. Tanya user hanya jika missing_info_questions "
                "berisi blocker kritis yang tidak bisa diinfer; selain itu lanjutkan create flow."
            ),
        }
        if repaired_json:
            logger.warning(
                "builder_tools.compose_agent_blueprint.json_repaired",
                preset_id=preset_id,
                agent_name=agent_name,
            )
            payload["parse_status"] = "json_repaired"
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # compose_agent_operating_manual                                      #
    # ------------------------------------------------------------------ #

    @tool
    async def compose_agent_operating_manual(
        preset_id: str,
        user_goal: str,
        agent_name: str = "",
        business_context: str = "",
        agent_blueprint: str = "",
        target_users: str = "",
        channel: str = "webchat",
        requested_features: str = "",
        known_constraints: str = "",
        domain: str = "",
    ) -> str:
        """
        Susun Agent Operating Manual/SOP terstruktur dari kebutuhan user dan blueprint.

        SOP ini adalah kontrak kerja runtime agent: workflow, data wajib, state,
        eskalasi, approval manusia, larangan, dan definisi selesai. Gunakan hasil
        `operating_manual` sebagai parameter create_agent/update_agent.

        Args:
            preset_id: Preset yang digunakan dari plan_agent.
            user_goal: Tujuan utama user.
            agent_name: Nama agent.
            business_context: Detail bisnis/SOP/kebijakan yang user berikan.
            agent_blueprint: JSON/string hasil compose_agent_blueprint.
            target_users: Siapa yang akan menggunakan agent.
            channel: whatsapp, webchat, atau channel lain.
            requested_features: Fitur yang diminta user.
            known_constraints: Batasan penting/compliance.
            domain: Domain bisnis jika diketahui.
        """
        preset = AGENT_PRESETS.get(preset_id, {})
        tc = preset.get("tools_config", {})

        def _fallback_response(parse_status: str) -> str:
            manual = build_agent_operating_manual_from_blueprint(
                agent_blueprint,
                name=agent_name or "Agent",
                description=user_goal,
                business_context=business_context,
                domain=domain,
                tools_config=tc,
            )
            if manual is None:
                manual = build_agent_operating_manual_from_blueprint(
                    _fallback_agent_blueprint(
                        preset_id=preset_id,
                        user_goal=user_goal,
                        agent_name=agent_name,
                        business_context=business_context,
                        target_users=target_users,
                        channel=channel,
                        requested_features=requested_features,
                        known_constraints=known_constraints,
                        tools_config=tc,
                    ),
                    name=agent_name or "Agent",
                    description=user_goal,
                    business_context=business_context,
                    domain=domain,
                    tools_config=tc,
                )
            manual = manual or {}
            return json.dumps({
                "operating_manual": manual,
                "summary": summarize_operating_manual(manual),
                "parse_status": parse_status,
                "prompt_preview": format_operating_manual_for_prompt(manual)[:1800],
                "next_step": (
                    "Gunakan operating_manual ini sebagai parameter create_agent/update_agent. "
                    "Jangan membuat agent bisnis tanpa SOP ini kecuali user hanya meminta agent coding sederhana."
                ),
            }, ensure_ascii=False, indent=2)

        system_msg = (
            "Kamu adalah senior operations designer untuk AI agent. "
            "Tugasmu mengubah kebutuhan user dan Agent Blueprint menjadi Agent Operating Manual/SOP yang konkret, spesifik, dan siap dipakai runtime. "
            "Jangan membuat SOP generik. Tulis seperti SOP pekerja manusia: state kerja, data wajib, langkah tindakan, decision points, handoff manusia, larangan, dan output akhir. "
            "Jika ada pembayaran, bukti transfer, approval admin, booking, refund, deliverable, file, atau integrasi akun, SOP harus menyebut kapan agent boleh lanjut dan kapan wajib berhenti/eskalasi. "
            "Return HANYA JSON valid, tanpa markdown dan tanpa penjelasan di luar JSON."
        )
        user_msg = (
            "Buat Agent Operating Manual/SOP dari data berikut.\n\n"
            f"preset_id: {preset_id}\n"
            f"preset_label: {preset.get('label', 'Custom')}\n"
            f"agent_name: {agent_name or 'belum ditentukan'}\n"
            f"user_goal: {user_goal}\n"
            f"business_context: {business_context or 'belum ada detail bisnis'}\n"
            f"target_users: {target_users or 'belum jelas'}\n"
            f"channel: {channel}\n"
            f"requested_features: {requested_features or '-'}\n"
            f"known_constraints: {known_constraints or '-'}\n"
            f"domain: {domain or '-'}\n"
            f"tools_config: {json.dumps(tc, ensure_ascii=False)}\n"
            f"agent_blueprint: {agent_blueprint or '-'}\n\n"
            "Schema JSON wajib:\n"
            "{\n"
            '  "manual_id": "agent_operating_manual",\n'
            '  "version": 1,\n'
            '  "source": "arthur_operating_manual_writer",\n'
            '  "domain": "domain bisnis spesifik",\n'
            '  "domain_confidence": "high|medium|low",\n'
            '  "maturity": "usable",\n'
            '  "owner_review_required": false,\n'
            '  "missing_context": [],\n'
            '  "assumptions": ["asumsi operasional yang dibuat"],\n'
            '  "workflows": [{\n'
            '    "workflow_id": "snake_case_id",\n'
            '    "name": "Nama workflow",\n'
            '    "trigger": "Kapan workflow dimulai",\n'
            '    "goal": "Tujuan workflow",\n'
            '    "required_inputs": ["data wajib"],\n'
            '    "steps": ["langkah konkret berurutan"],\n'
            '    "decision_points": ["kondisi dan pilihan keputusan"],\n'
            '    "allowed_tools": ["tool yang boleh dipakai"],\n'
            '    "escalation_rules": ["kapan dan cara eskalasi"],\n'
            '    "prohibited_actions": ["hal yang tidak boleh dilakukan"],\n'
            '    "final_output": "Definisi selesai yang nyata",\n'
            '    "examples": ["contoh pendek jika perlu"]\n'
            "  }],\n"
            '  "knowledge_plan": {"must_have": ["..."], "nice_to_have": ["..."], "needs_upload": false},\n'
            '  "memory_plan": [{"key": "...", "value_to_store": "..."}],\n'
            '  "validation_checklist": ["..."]\n'
            "}\n\n"
            "Aturan kualitas:\n"
            "- workflows minimal 2 untuk agent bisnis/custom, kecuali agent sangat sederhana.\n"
            "- Untuk payment/approval/delivery, workflow wajib memisahkan intake, payment_review, approved/fulfillment, delivery, dan aftercare.\n"
            "- Jika business_context cukup, maturity harus usable dan owner_review_required false.\n"
            "- Jika ada data kritis belum ada, isi missing_context dengan data itu dan set maturity needs_review.\n"
            "- Jangan menaruh SOP lengkap di instructions; SOP ini disimpan sebagai operating_manual terpisah."
        )

        try:
            raw = await _call_instruction_writer(
                user_msg,
                system_msg,
                model=_BLUEPRINT_WRITER_MODEL,
                max_tokens=7000,
                temperature=0.15,
                json_mode=True,
            )
        except Exception as exc:
            logger.error("builder_tools.compose_agent_operating_manual.error", error=str(exc))
            return _fallback_response("deterministic_fallback")

        try:
            manual, repaired_json = _parse_llm_json_object(raw)
        except Exception as exc:
            logger.warning(
                "builder_tools.compose_agent_operating_manual.parse_failed",
                error=str(exc),
                preset_id=preset_id,
                agent_name=agent_name,
                output_preview=(raw or "")[:240],
            )
            return _fallback_response("deterministic_fallback")

        manual.setdefault("manual_id", "agent_operating_manual")
        manual.setdefault("version", 1)
        manual.setdefault("source", "arthur_operating_manual_writer")
        manual.setdefault("domain", domain or "generic")
        manual.setdefault("domain_confidence", "medium")
        manual.setdefault("maturity", "usable")
        manual.setdefault("owner_review_required", manual.get("maturity") in {"draft", "needs_review"})
        manual.setdefault("missing_context", [])
        manual.setdefault("assumptions", [])
        if not isinstance(manual.get("workflows"), list) or not manual["workflows"]:
            logger.warning(
                "builder_tools.compose_agent_operating_manual.empty_workflows",
                preset_id=preset_id,
                agent_name=agent_name,
            )
            return _fallback_response("deterministic_fallback")

        summary = summarize_operating_manual(manual)
        payload = {
            "operating_manual": manual,
            "summary": summary,
            "prompt_preview": format_operating_manual_for_prompt(manual)[:1800],
            "next_step": (
                "Gunakan operating_manual ini sebagai parameter create_agent/update_agent. "
                "Setelah itu validate_agent_config dan create_agent/update_agent tanpa minta approval mikro."
            ),
        }
        if repaired_json:
            logger.warning(
                "builder_tools.compose_agent_operating_manual.json_repaired",
                preset_id=preset_id,
                agent_name=agent_name,
            )
            payload["parse_status"] = "json_repaired"
        return json.dumps(payload, ensure_ascii=False, indent=2)

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
        agent_blueprint: str = "",
    ) -> str:
        """
        Tulis system prompt (instructions) berkualitas tinggi untuk agent baru atau agent existing
        menggunakan model reasoning khusus (deepseek-r1).

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
            channel: Channel: 'whatsapp' atau 'webchat'
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
            "- create_wa_dev_trial_link(agent_id, phone, force_new_code, send_contact) — "
            "buat kode 6 karakter + link wa.me untuk user mencoba agent lewat nomor WhatsApp shared Arthur tanpa scan QR."
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
                logger.warning(
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
                logger.warning(
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
            logger.error("builder_tools.compose_agent_instructions.error", error=str(exc))
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
        Buat soul (identitas permanen) untuk agent.
        Soul di-inject otomatis ke setiap sesi agent sebagai fondasi identitasnya.

        Untuk agent baru: panggil setelah create_agent berhasil jika soul belum dikirim saat create.
        Untuk agent existing/update: jangan panggil sebelum update_agent; panggil hanya setelah
        update_agent berhasil jika soul juga perlu disimpan via set_agent_memory(agent_id, key="soul", value=soul).

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
            " Wajib sebut bahwa agent dibuat oleh Arthur, punya Owner, dan Owner adalah bos/superadmin yang harus dihubungi saat butuh keputusan, izin, atau akses integrasi."
            " Jangan mengarang nama brand/bisnis. Jika nama bisnis tidak diberikan eksplisit, tulis 'bisnis ini' atau 'usaha ini'."
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
            soul, business_name_sanitized = _sanitize_unverified_business_name(
                soul,
                business_context=business_info or business,
            )
            # Strip any leftover placeholders
            placeholders = _find_unfilled_placeholders(soul)
            payload = {
                "soul": soul,
                "char_count": len(soul),
                "remaining_placeholders": placeholders,
                "next_step": (
                    "Untuk agent baru, kirim soul ini lewat parameter soul saat create_agent jika belum dibuat. "
                    "Untuk agent existing, jangan berhenti di sini: update_agent dulu, lalu panggil "
                    "set_agent_memory(agent_id, key='soul', value=soul) hanya jika soul perlu diperbarui."
                ),
            }
            if business_name_sanitized:
                payload["business_name_sanitized"] = True
            return json.dumps(payload, ensure_ascii=False, indent=2)
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
                "next_step": (
                    "Untuk agent baru, kirim soul ini lewat parameter soul saat create_agent jika belum dibuat. "
                    "Untuk agent existing, jangan berhenti di sini: update_agent dulu, lalu panggil "
                    "set_agent_memory(agent_id, key='soul', value=soul) hanya jika soul perlu diperbarui."
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
            "important_tools": {
                "builder": "create_agent/update_agent/delete_agent/get_agent_detail/list_my_agents/set_agent_memory",
                "whatsapp": "send_agent_wa_qr untuk kirim QR agent baru",
                "coding": "sandbox + deploy + subagents sys_coder",
                "browsing": "tavily_search/tavily_extract untuk web search dan baca URL",
                "productivity": "scheduler untuk reminder, integrasi Google Workspace untuk Docs/Sheets/Drive/Gmail/Calendar",
            },
            "input_types": [
                "teks — pesan tulis biasa",
                "voice_note — audio PTT, otomatis ditranskrip ke teks via Whisper",
                "gambar — bisa dianalisis jika model mendukung vision",
                "dokumen — PDF/DOCX/TXT, bisa diindeks ke RAG",
            ],
            "critical_limitations": [
                RUNTIME_LIMITATIONS["wa_device_scan_required_before_use"]["user_message"],
                RUNTIME_LIMITATIONS["deploy_requires_docker_socket"]["user_message"],
                RUNTIME_LIMITATIONS["deploy_ttl_4h_max"]["user_message"],
                RUNTIME_LIMITATIONS["one_wa_number_per_agent"]["user_message"],
            ],
            "prohibited_agent_purposes": [
                "buzzer",
                "kampanye politik",
                "propaganda politik",
                "manipulasi opini publik",
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
            "next": "Gunakan get_presets(preset_id) untuk detail preset spesifik. Jangan panggil tool ini berulang dalam sesi yang sama.",
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 2. list_available_wa_devices                                        #
    # ------------------------------------------------------------------ #
    # get_user_subscription                                              #
    # ------------------------------------------------------------------ #

    @tool
    async def get_user_subscription(phone: str = "") -> str:
        """
        Cek status subscription dan kuota agent user berdasarkan nomor telepon.
        Gunakan ini sebelum atau sesudah create_agent untuk memberikan info
        akurat tentang plan, sisa slot agent, dan status subscription.

        Args:
            phone: Nomor telepon user (format: 628xxx). Kosong = gunakan owner agent saat ini.
        """
        try:
            from app.core.domain.subscription_service import (
                get_or_create_wa_user,
                get_subscription_by_external_id,
                check_can_create_agent,
            )
            from app.models.agent import Agent
            from app.models.subscription import SubscriptionPlan

            target_phone = _best_owner_identifier(phone, owner_phone, default_target)
            if not target_phone:
                return json.dumps({"error": "phone tidak tersedia"}, ensure_ascii=False)

            async with db_factory() as db:
                if _is_probable_lid(target_phone):
                    details = await get_subscription_by_external_id(target_phone, db)
                    if details is None:
                        return json.dumps({
                            "error": (
                                "Nomor WhatsApp asli belum tersedia di session ini. "
                                "Kirim pesan dari nomor WA yang sudah ter-resolve, atau pastikan profil user punya phone_number."
                            ),
                            "identifier": target_phone,
                            "identifier_type": "lid",
                        }, ensure_ascii=False)
                    user, sub, plan = details
                else:
                    # Auto-provision only for real phone identifiers, never for LID.
                    user, sub = await get_or_create_wa_user(target_phone, db)
                    await db.commit()

                    plan = (
                        await db.execute(
                            select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
                        )
                    ).scalar_one()

                # Hitung agent aktif
                active_count_result = await db.execute(
                    select(Agent).where(
                        Agent.is_deleted.is_(False),
                        _owner_filter(target_phone),
                    )
                )
                active_agents = active_count_result.scalars().all()
                used = len(active_agents)
                limit = plan.max_agents
                remaining = None if limit is None else max(0, limit - used)

                return json.dumps({
                    "phone": target_phone,
                    "plan_code": plan.code,
                    "plan_label": plan.label,
                    "status": sub.status,
                    "is_active": sub.is_usable,
                    "agents_used": used,
                    "agents_limit": limit,
                    "agents_remaining": remaining,
                    "active_agent_names": [a.name for a in active_agents],
                    "token_quota": sub.token_quota,
                    "tokens_used": getattr(sub, "tokens_used", 0),
                    "tokens_remaining": getattr(sub, "tokens_remaining", max(0, sub.token_quota - getattr(sub, "tokens_used", 0))),
                    "active_until": sub.expires_at.isoformat() if sub.expires_at else None,
                }, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("builder_tools.get_user_subscription.error", error=str(exc))
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

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
        Ini bukan approval gate. Setelah plan siap, lanjutkan ke compose_agent_blueprint,
        compose_agent_instructions, validate_agent_config, lalu create_agent tanpa bertanya
        "setuju/lanjut?" kecuali ada validation_errors atau data kritis yang benar-benar
        wajib dari user.

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
        policy_reason = _blocked_agent_policy_reason(
            user_goal,
            agent_name,
            requested_features,
            persona,
            business_context,
        )
        if policy_reason:
            return json.dumps({
                "plan_status": "blocked_by_policy",
                "validation_errors": [policy_reason],
                "next_action": "Tolak permintaan ini dengan singkat dan tawarkan jenis agent non-politik/non-buzzer.",
            }, ensure_ascii=False, indent=2)

        features = [f.strip().lower() for f in requested_features.split(",") if f.strip()]
        feature_text = _combined_context_text(user_goal, requested_features, business_context)
        google_context_text = f"{user_goal} {requested_features} {business_context}"

        # Auto-detect preset from goal keywords
        detected_preset = _detect_preset(feature_text, features, channel)

        preset = AGENT_PRESETS.get(detected_preset, {})
        tools_config = dict(preset.get("tools_config", {
            "memory": True, "skills": True, "escalation": True
        }))
        tools_config.setdefault("tavily", True)

        # Override with explicitly requested features
        feature_map = {
            "rag": "rag", "dokumen": "rag", "faq": "rag", "document": "rag",
            "scheduler": "scheduler", "reminder": "scheduler", "jadwal": "scheduler",
            "http": "http", "api": "http",
            "tavily": "tavily", "browse": "tavily", "browser": "tavily", "search": "tavily",
            "sandbox": "sandbox", "coding": "sandbox", "kode": "sandbox", "prototype": "sandbox", "website": "sandbox",
            "deploy": "deploy",
            "whatsapp_media": "whatsapp_media", "media": "whatsapp_media", "gambar": "whatsapp_media",
            "file": "whatsapp_media", "pdf": "whatsapp_media", "excel": "whatsapp_media", "docx": "whatsapp_media",
        }
        for feat in features:
            mapped = feature_map.get(feat)
            if mapped and mapped in tools_config:
                tools_config[mapped] = True

        approval_gated_service = _looks_like_approval_gated_service(feature_text)
        payment_approval_workflow = _looks_like_payment_approval_workflow(feature_text)
        file_delivery_workflow = _looks_like_file_delivery_workflow(feature_text)
        generated_file_workflow = _looks_like_generated_file_workflow(feature_text)
        wants_coding = any(k in feature_text for k in ("coding", "kode", "prototype", "website", "deploy", "sandbox"))
        wants_cv_document = any(
            k in feature_text
            for k in (
                "bikin cv",
                "buat cv",
                "cv ats",
                "resume ats",
                "bikin resume",
                "buat resume",
                "kirim cv",
                "kirim resume",
            )
        )
        wants_files = file_delivery_workflow or wants_cv_document
        wants_generated_files = generated_file_workflow or wants_cv_document
        plain_google_form_link = _is_plain_google_form_link_reference(google_context_text)
        google_negated = _negates_google_workspace(feature_text)
        wants_google = (
            any(k in feature_text for k in ("google", "gmail", "calendar", "drive", "docs", "sheets", "workspace"))
            and not plain_google_form_link
            and not google_negated
        )
        google_workspace_option = (
            {
                "should_offer": False,
                "enabled": False,
                "suggested_apps": [],
                "reasons": [],
                "user_facing_pitch": "",
                "if_user_declines": "Lanjutkan tanpa integrasi Google.",
            }
            if google_negated
            else _google_workspace_option(feature_text, wants_google)
        )
        if wants_coding:
            tools_config["sandbox"] = True
            tools_config["deploy"] = True
            tools_config["subagents"] = {"enabled": True}
        if wants_files:
            tools_config["whatsapp_media"] = True
        if wants_generated_files:
            tools_config["sandbox"] = True
            tools_config["subagents"] = {"enabled": True}
        explicit_media_request = any(
            feat in features
            for feat in ("media", "gambar", "foto", "file", "pdf", "excel", "xlsx", "docx", "dokumen")
        )
        if not wants_files and not wants_generated_files and not explicit_media_request:
            tools_config["whatsapp_media"] = False
        if approval_gated_service or payment_approval_workflow:
            tools_config["escalation"] = True
            tools_config["whatsapp_media"] = True
        needs_human_handoff = bool(operator_phone) or any(
            k in feature_text
            for k in (
                "admin",
                "operator",
                "owner",
                "pemilik",
                "eskalasi",
                "approval",
                "approve",
                "harga final",
                "stok",
                "booking",
                "kepastian",
                "komplain",
                "refund",
                "bukti transfer",
                "dp",
                "pelunasan",
            )
        )
        if needs_human_handoff:
            tools_config["escalation"] = True
        if wants_google:
            tools_config = _enable_google_workspace_tools(tools_config)

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

        effective_model = preset.get("default_model", _DEFAULT_MODEL)
        creation_entitlement_check = await _preview_agent_creation_entitlement(
            tools_config=tools_config,
            model=effective_model,
            channel_type=effective_channel if effective_channel != "webchat" else "",
        )
        entitlement_blocked = bool(
            creation_entitlement_check.get("checked")
            and not creation_entitlement_check.get("allowed", True)
        )
        if entitlement_blocked:
            entitlement_message = (
                creation_entitlement_check.get("user_message")
                or creation_entitlement_check.get("reason")
                or "Paket kamu belum bisa membuat agent ini."
            )
            validation_errors.append(entitlement_message)
            for violation in creation_entitlement_check.get("violations") or []:
                validation_errors.append(str(violation))
        elif not creation_entitlement_check.get("checked"):
            validation_warnings.append(
                "Cek tier/slot awal belum bisa diverifikasi; create_agent tetap akan melakukan hard gate sebelum menyimpan agent."
            )

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
        plan_status = (
            "blocked_by_subscription"
            if entitlement_blocked
            else "ready" if not validation_errors else "has_errors"
        )
        plan = {
            "plan_status": plan_status,
            "detected_preset": detected_preset,
            "preset_label": preset.get("label", "Custom"),
            "agent_name": agent_name or f"Agent {detected_preset.replace('_', ' ').title()}",
            "business_goal": user_goal,
            "channel": effective_channel,
            "persona": persona or "ramah dan profesional",
            "business_context": business_context,
            "blueprint_seed": {
                "agent_summary": f"{agent_name or 'Agent ini'} dibuat untuk {user_goal}",
                "customization_goal": (
                    "Gunakan compose_agent_blueprint jika agent perlu SOP/workflow spesifik per bisnis, "
                    "produk, tim, atau industri. Jangan hanya mengandalkan persona generik."
                ),
                "known_business_context": business_context,
                "requested_features": features,
                "design_considerations": [
                    "Langkah kerja ideal agent dari awal sampai selesai.",
                    "Data yang wajib dikumpulkan dari user/pelanggan.",
                    "Pengetahuan produk/SOP yang wajib agent tahu.",
                    "Kapan agent harus eskalasi ke manusia.",
                    "Apakah ada pembayaran, approval admin, atau deliverable yang baru boleh dikirim setelah disetujui.",
                ],
            },
            "recommended_config": {
                "model": effective_model,
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
            "creation_entitlement_check": creation_entitlement_check,
            "google_workspace_option": google_workspace_option,
            "smoke_test_guidance": preset.get("smoke_test", {}).get("steps", []),
            "next_action": (
                "Jelaskan limit paket dengan bahasa sederhana dan tawarkan upgrade/top up sebelum lanjut membuat agent."
                if entitlement_blocked
                else
                (
                    "Tawarkan opsi integrasi Google Workspace dengan bahasa awam memakai google_workspace_option.user_facing_pitch. "
                    "Jika user setuju, panggil plan_agent lagi dengan requested_features berisi google sebelum create. "
                    "Jika user menolak, lanjutkan compose_agent_blueprint/compose_agent_instructions tanpa Google."
                )
                if google_workspace_option.get("should_offer") and not validation_errors
                else "Untuk agent bisnis/custom, panggil compose_agent_blueprint lalu compose_agent_instructions. "
                "Setelah itu validate_agent_config dan create_agent tanpa minta approval mikro."
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
            document_count = (
                await _count_agent_documents(db, agent.id)
                if agent and _rag_enabled(agent.tools_config or {})
                else None
            )
            operating_manual = (
                await get_latest_agent_operating_manual(
                    agent.id,
                    db,
                    fallback_tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                )
                if agent
                else None
            )
        if not agent:
            return f"[error] Agent dengan ID {agent_id} tidak ditemukan setelah create — kemungkinan create gagal"

        tc: dict = agent.tools_config or {}
        active_tools = [k for k, v in tc.items() if v and v is not False]
        google_workspace_enabled = _has_google_workspace_tools(tc)
        rag_is_enabled = _rag_enabled(tc)
        operating_manual_summary = summarize_operating_manual(operating_manual)
        instructions_text = agent.instructions or ""
        created_by_metadata = _agent_created_by_metadata(agent)

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

        owner_present = bool(getattr(agent, "owner_external_id", None) or (getattr(agent, "operator_ids", None) or []))
        created_by_present = bool(created_by_metadata["created_by_type"])
        platform_identity_present = (
            created_by_metadata["created_by_type"] == "arthur_builder"
            or "IDENTITAS PLATFORM DAN OWNER" in instructions_text
            or "dibuat dan dikonfigurasi oleh Arthur" in instructions_text
        )
        readiness_blockers: list[str] = []
        readiness_warnings: list[str] = []
        if not owner_present:
            readiness_blockers.append("owner_missing: agent belum punya owner_external_id/operator_ids yang jelas.")
        if not created_by_present:
            readiness_warnings.append(
                "created_by_metadata_missing: agent lama belum punya metadata created_by_* dari DB."
            )
        if not platform_identity_present:
            readiness_warnings.append(
                "platform_identity_missing: runtime tetap inject Owner/tools, tapi instructions lama belum punya identitas dibuat Arthur."
            )
        if google_workspace_enabled:
            readiness_blockers.append(
                "google_auth_required: Google Workspace aktif; Owner harus login/reauth sebelum agent boleh mengklaim aksi Google berhasil."
            )
        if rag_is_enabled:
            if document_count == 0:
                readiness_blockers.append(
                    "rag_documents_required: Knowledge base/RAG aktif, tapi belum ada dokumen. Owner harus upload dokumen sebelum agent boleh mengklaim jawaban berdasarkan dokumen."
                )
            elif document_count is None:
                readiness_warnings.append(
                    "rag_documents_unknown: Knowledge base/RAG aktif, tapi jumlah dokumen belum bisa dicek."
                )
        if agent.channel_type == "whatsapp" and not getattr(agent, "wa_device_id", None):
            readiness_blockers.append(
                "whatsapp_setup_required: agent WhatsApp belum punya wa_device_id/nomor yang siap dipakai."
            )
        readiness_blockers.extend(
            f"workflow_invalid: {error}"
            for error in _critical_workflow_config_errors(
                name=agent.name or "",
                description=agent.description or "",
                instructions=instructions_text,
                tools_config=tc,
                preset_id=detected_preset,
            )
        )
        manual_blockers, manual_warnings = operating_manual_readiness_issues(operating_manual)
        readiness_blockers.extend(manual_blockers)
        readiness_warnings.extend(manual_warnings)
        readiness_warnings.extend(config_warnings)
        launch_status = "launch_blocked" if readiness_blockers else (
            "launch_ready_with_warnings" if readiness_warnings else "launch_ready"
        )

        # Surface applicable limitations
        preset = AGENT_PRESETS.get(detected_preset, {})
        applicable_limitations = [
            RUNTIME_LIMITATIONS[l]["user_message"]
            for l in preset.get("runtime_limitations", [])
            if l in RUNTIME_LIMITATIONS
        ]

        smoke_test = preset.get("smoke_test", {})
        post_create = _get_post_create_steps(detected_preset, agent.channel_type or "webchat", tc)
        setup_status_for_owner = _build_owner_setup_status(
            launch_status=launch_status,
            owner_present=owner_present,
            created_by_present=created_by_present,
            operating_manual=operating_manual_summary,
            google_workspace_enabled=google_workspace_enabled,
            rag_enabled=rag_is_enabled,
            document_count=document_count,
            whatsapp_channel=agent.channel_type == "whatsapp",
            whatsapp_ready=bool(getattr(agent, "wa_device_id", None)),
            escalation_enabled=_tool_config_enabled(tc, "escalation", default=False),
            readiness_blockers=readiness_blockers,
            readiness_warnings=readiness_warnings,
        )

        summary = {
            "status": launch_status,
            "agent_id": str(agent.id),
            "name": agent.name,
            "model": agent.model,
            "channel_type": agent.channel_type,
            "owner_external_id": getattr(agent, "owner_external_id", None),
            "operator_ids": getattr(agent, "operator_ids", None) or [],
            **created_by_metadata,
            "active_tools": active_tools,
            "google_workspace_enabled": google_workspace_enabled,
            "needs_google_auth": google_workspace_enabled,
            "rag_enabled": rag_is_enabled,
            "document_count": document_count,
            "operating_manual": operating_manual_summary,
            "max_tokens": agent.max_tokens,
            "detected_preset": detected_preset,
            "config_warnings": config_warnings,
            "launch_readiness": {
                "status": launch_status,
                "owner_present": owner_present,
                "created_by_present": created_by_present,
                "platform_identity_present": platform_identity_present,
                "created_by_metadata": created_by_metadata,
                "needs_google_auth": google_workspace_enabled,
                "rag_enabled": rag_is_enabled,
                "document_count": document_count,
                "operating_manual": operating_manual_summary,
                "blockers": readiness_blockers,
                "warnings": readiness_warnings,
            },
            "setup_status_for_owner": setup_status_for_owner,
            "applicable_limitations": applicable_limitations,
            "required_next_steps": post_create,
            "smoke_test_steps": smoke_test.get("steps", []),
            "smoke_test_expected": smoke_test.get("expected_status", ""),
            "known_failure_modes": smoke_test.get("known_failure_modes", []),
            "instructions_preview": instructions_text[:200] + ("..." if len(instructions_text) > 200 else ""),
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
            model: Model LLM yang akan digunakan (kosong = pakai default gpt-4.1-mini)
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

        policy_reason = _blocked_agent_policy_reason(name, instructions, tools_config, preset_id)
        if policy_reason:
            errors.append(policy_reason)

        # Validasi instructions
        instruction_len = len(instructions)
        if instruction_len < 100:
            errors.append("Instructions terlalu pendek — agent tidak akan punya cukup konteks. Gunakan compose_agent_instructions untuk generate yang baik.")
        if instruction_len > 32000:
            errors.append(f"Instructions terlalu panjang ({instruction_len} karakter) — bisa melebihi context window model")
        elif instruction_len > 16000:
            warnings.append(f"Instructions cukup panjang ({instruction_len} karakter) — pertimbangkan memindahkan detail ke RAG documents")

        # Deteksi placeholder yang belum diisi
        unfilled = _find_unfilled_placeholders(instructions)
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
        if isinstance(tc, dict):
            tc.setdefault("tavily", True)

        approval_gated_service = _looks_like_approval_gated_service(
            name,
            instructions,
            tools_config,
            preset_id,
        )
        payment_approval_workflow = _looks_like_payment_approval_workflow(
            name,
            instructions,
            tools_config,
            preset_id,
        ) or preset_id == "approval_gated_service_agent" or approval_gated_service
        file_delivery_workflow = _looks_like_file_delivery_workflow(
            name,
            instructions,
            tools_config,
            preset_id,
        )
        generated_file_workflow = _looks_like_generated_file_workflow(
            name,
            instructions,
            tools_config,
            preset_id,
        )
        if payment_approval_workflow:
            if instruction_len < 1200:
                errors.append(
                    "Instructions terlalu pendek untuk workflow pembayaran/admin approval — "
                    "wajib memuat state intake, waiting_payment, payment_review, approved, delivery, dan aftercare."
                )
            if not _has_approval_state_contract(instructions):
                errors.append(
                    "Workflow pembayaran belum lengkap — instructions wajib memuat state "
                    "intake -> waiting_payment -> payment_review -> approved -> delivery -> aftercare."
                )
            if not tc.get("escalation"):
                errors.append("Workflow pembayaran/admin approval wajib mengaktifkan escalation: true.")
            if "escalate_to_human" not in instructions:
                errors.append("Workflow bukti transfer wajib menginstruksikan pemanggilan escalate_to_human.")
        if file_delivery_workflow:
            if not tc.get("whatsapp_media"):
                errors.append("Workflow delivery file via WhatsApp wajib mengaktifkan whatsapp_media: true.")
            errors.extend(file_delivery_contract_issues(instructions, file_delivery=True))
        if generated_file_workflow:
            subagents_cfg = tc.get("subagents", {})
            subagents_enabled = bool(
                subagents_cfg.get("enabled") if isinstance(subagents_cfg, dict) else subagents_cfg
            )
            if not tc.get("sandbox") or not subagents_enabled:
                errors.append("Workflow pembuatan file final wajib mengaktifkan sandbox dan subagents.")

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
        model: str = "openai/gpt-4.1-mini",
        temperature: float = 0.7,
        tools_config: Any = '{"memory": true, "skills": true, "escalation": true}',
        allowed_senders: Any = "",
        channel_type: str = "",
        escalation_config: Any = "{}",
        operator_phone: str = "",
        operator_name: str = "",
        token_quota: int = 4_000_000,
        max_tokens: int = 0,
        soul: str = "",
        blueprint: str = "",
        business_context: str = "",
        domain: str = "",
        operating_manual: Any = None,
    ) -> str:
        """
        Buat agent baru di platform dan simpan ke database.
        Agent akan otomatis dikaitkan dengan user yang sedang chat (owner_phone).

        Args:
            name: Nama agent (wajib, maks 255 karakter)
            instructions: System prompt / instructions lengkap agent
            description: Deskripsi singkat fungsi agent
            model: Model LLM (default: openai/gpt-4.1-mini)
            temperature: Kreativitas respons, 0.0-2.0 (default: 0.7)
            tools_config: JSON string atau object konfigurasi tools, contoh: '{"memory": true, "scheduler": true}'
            allowed_senders: JSON array/string nomor WA yang diizinkan, contoh: '["+62811xxx"]'. Kosong = semua.
            channel_type: Channel yang dipakai: 'whatsapp', 'webchat', atau kosong
            escalation_config: JSON string konfigurasi eskalasi, contoh: '{"channel_type": "whatsapp", "operator_phone": "+62xxx"}'
            operator_phone: Nomor WA operator/admin yang akan dapat notifikasi eskalasi
            operator_name: Nama operator/admin (misal: "Budi", "Tim CS"). Wajib diisi agar agent tahu siapa operatornya.
            token_quota: Batas token per periode (default: 4,000,000)
            max_tokens: Batas token per reply LLM. WA CS: 512-800, default platform: 1024. Isi 0 untuk pakai default.
            soul: Identitas permanen agent hasil compose_agent_soul. Jika diisi, disimpan otomatis ke memory key='soul'.
            blueprint: Agent Blueprint hasil compose_agent_blueprint. Jika diisi, disimpan otomatis ke memory key='agent_blueprint'.
            business_context: Ringkasan konteks bisnis Owner dari interview Arthur. Dipakai untuk membuat SOP terpisah.
            domain: Bidang bisnis jika sudah diketahui, misal food_beverage, travel, ecommerce, local_service, clinic_wellness, education, property.
            operating_manual: Agent Operating Manual/SOP artifact terstruktur. Jika kosong, Arthur/runtime membuat draft dari konteks.
        """
        if not name or len(name.strip()) < 2:
            return "[error] Nama agent minimal 2 karakter"
        policy_reason = _blocked_agent_policy_reason(
            name,
            description,
            instructions,
            tools_config,
            escalation_config,
            soul,
            blueprint,
        )
        if policy_reason:
            return json.dumps({"error": policy_reason}, ensure_ascii=False)
        if not owner_phone:
            return (
                "[error] Tidak bisa membuat agent karena owner_external_id tidak tersedia. "
                "Pastikan Arthur dijalankan dari session user yang memiliki external_user_id."
            )

        tc, tc_error = _parse_json_arg(
            tools_config,
            {"memory": True, "skills": True, "escalation": True},
            expected=dict,
        )
        if tc_error:
            return f"[error] tools_config bukan JSON/object yang valid: {tc_error}"
        tc.setdefault("tavily", True)
        inferred_operator_phone = _extract_operator_phone_from_context(
            operator_phone,
            escalation_config,
            business_context,
            description,
            instructions,
            blueprint,
            soul,
        )
        if not operator_phone and inferred_operator_phone:
            operator_phone = inferred_operator_phone
        if operator_phone:
            tc["escalation"] = True
        operating_manual_input = operating_manual
        if operating_manual_input in (None, "", {}) and str(blueprint or "").strip():
            if _blueprint_needs_semantic_operating_manual(blueprint):
                operating_manual_input = await _compose_semantic_operating_manual_from_context(
                    name=name,
                    description=description,
                    instructions=instructions,
                    business_context=business_context,
                    domain=domain,
                    channel_type=channel_type,
                    tools_config=tc,
                )
            if operating_manual_input in (None, "", {}):
                operating_manual_input = build_agent_operating_manual_from_blueprint(
                    blueprint,
                    name=name,
                    description=description,
                    business_context=business_context,
                    domain=domain,
                    tools_config=tc,
                )
        if operating_manual_input in (None, "", {}) and (business_context.strip() or description.strip()):
            operating_manual_input = await _compose_semantic_operating_manual_from_context(
                name=name,
                description=description,
                instructions=instructions,
                business_context=business_context,
                domain=domain,
                channel_type=channel_type,
                tools_config=tc,
            )
        if operating_manual_input in (None, "", {}) and (business_context.strip() or description.strip()):
            preset_for_manual = _detect_preset_from_config(tc, channel_type or "")
            fallback_blueprint = _fallback_agent_blueprint(
                preset_id=preset_for_manual,
                user_goal=description or name,
                agent_name=name,
                business_context=business_context or instructions,
                target_users="customer" if channel_type == "whatsapp" else "",
                channel=channel_type or "",
                requested_features=json.dumps(tc, ensure_ascii=False),
                known_constraints="",
                tools_config=tc,
            )
            operating_manual_input = build_agent_operating_manual_from_blueprint(
                fallback_blueprint,
                name=name,
                description=description,
                business_context=business_context or instructions,
                domain=domain,
                tools_config=tc,
            )
        tc, generated_operating_manual = ensure_operating_manual_in_tools_config(
            tc,
            name=name,
            description=description,
            instructions=instructions,
            business_context=business_context or blueprint or soul,
            domain=domain,
            operating_manual=operating_manual_input,
        )
        if _has_google_workspace_tools(tc):
            instructions, _ = _append_google_workspace_instruction(instructions)
        instructions, business_name_sanitized = _sanitize_unverified_business_name(
            instructions,
            business_context=business_context or description,
        )
        if soul:
            soul, soul_business_name_sanitized = _sanitize_unverified_business_name(
                soul,
                business_context=business_context or description,
            )
        else:
            soul_business_name_sanitized = False
        platform_identity_added = False

        critical_errors: list[str] = []
        approval_gated_service = _looks_like_approval_gated_service(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        )
        payment_approval_workflow = _looks_like_payment_approval_workflow(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        ) or approval_gated_service
        file_delivery_workflow = _looks_like_file_delivery_workflow(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        )
        generated_file_workflow = _looks_like_generated_file_workflow(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        )
        if payment_approval_workflow:
            if len((instructions or "").strip()) < 1200:
                critical_errors.append("Instructions terlalu pendek untuk workflow pembayaran/admin approval.")
            if not _has_approval_state_contract(instructions):
                critical_errors.append(
                    "Instructions wajib memuat state intake, waiting_payment, payment_review, approved, delivery, dan aftercare."
                )
            if not tc.get("escalation"):
                critical_errors.append("Workflow pembayaran/admin approval wajib escalation=true.")
            if "escalate_to_human" not in instructions:
                critical_errors.append("Instructions wajib menyebut escalate_to_human untuk bukti transfer/admin approval.")
        if file_delivery_workflow:
            if not tc.get("whatsapp_media"):
                critical_errors.append("Workflow delivery file wajib whatsapp_media=true.")
            critical_errors.extend(file_delivery_contract_issues(instructions, file_delivery=True))
        if generated_file_workflow:
            subagents_cfg = tc.get("subagents", {})
            subagents_enabled = bool(
                subagents_cfg.get("enabled") if isinstance(subagents_cfg, dict) else subagents_cfg
            )
            if not tc.get("sandbox") or not subagents_enabled:
                critical_errors.append("Workflow pembuatan file final wajib sandbox=true dan subagents.enabled=true.")
        if critical_errors:
            return json.dumps({
                "error": "Konfigurasi agent belum aman untuk dibuat.",
                "validation_errors": critical_errors,
                "hint": "Panggil compose_agent_blueprint dan compose_agent_instructions ulang, lalu validate_agent_config sebelum create_agent.",
            }, ensure_ascii=False, indent=2)
        instructions, platform_identity_added = _append_platform_staff_identity_instruction(
            instructions,
            owner_phone=owner_phone,
            operator_phone=operator_phone,
            operator_name=operator_name,
        )
        owner_ids = _owner_variants(owner_phone)

        ec, ec_error = _parse_json_arg(escalation_config, {}, expected=dict)
        if ec_error:
            return f"[error] escalation_config bukan JSON/object yang valid: {ec_error}"

        # Parse allowed_senders
        senders: list[str] | None = None
        if allowed_senders:
            parsed_senders, sender_error = _parse_json_arg(allowed_senders, None, expected=list)
            if sender_error:
                return f"[error] allowed_senders harus berupa JSON array/list, contoh: [\"+62811xxx\"] ({sender_error})"
            senders = parsed_senders

        # Duplicate check: cegah agent dengan nama sama milik user yang sama
        if owner_phone and hasattr(Agent, "__table__"):
            async with db_factory() as db:
                dup_result = await db.execute(
                    select(Agent).where(
                        func.lower(Agent.name) == name.strip().lower(),
                        Agent.is_deleted.is_(False),
                        _owner_filter(owner_phone),
                    )
                )
                dup = dup_result.scalar_one_or_none()
            if dup:
                return json.dumps({
                    "error": f"Agent dengan nama '{name.strip()}' sudah ada.",
                    "existing_agent_id": str(dup.id),
                    "hint": "Gunakan update_agent(agent_id, ...) untuk mengubah agent yang sudah ada, atau pilih nama yang berbeda.",
                }, ensure_ascii=False)

        # operator_ids: selalu include owner_phone + operator_phone yang diminta
        op_ids: list[str] = []
        for owner_id in owner_ids:
            if owner_id and owner_id not in op_ids:
                op_ids.append(owner_id)
        if operator_phone and operator_phone not in op_ids:
            op_ids.append(operator_phone)

        if ec and operator_phone and "operator_phone" not in ec:
            ec["operator_phone"] = operator_phone
        if operator_name and "operator_name" not in ec:
            ec["operator_name"] = operator_name

        try:
            from app.core.domain.subscription_service import (
                check_can_create_agent,
                get_subscription_by_external_id,
                get_or_create_wa_user,
                validate_agent_entitlements,
            )

            logger.info("builder_tools.create_agent.start", owner_phone=owner_phone, name=name)

            async with db_factory() as db:
                # Auto-provision user + Tier 1 subscription untuk WA user.
                # Saat unit test mem-patch Agent menjadi mock, skip integrasi subscription.
                if owner_phone and hasattr(Agent, "__table__"):
                    _user, _sub = await get_or_create_wa_user(owner_phone, db)
                    logger.info("builder_tools.create_agent.user_provisioned", user_id=str(_user.id), sub_status=_sub.status)

                    # Cek apakah boleh buat agent (slot & status subscription)
                    _check = await check_can_create_agent(owner_phone, db)
                    logger.info("builder_tools.create_agent.slot_check", check=_check)
                    if not _check["allowed"]:
                        return json.dumps({"error": _check["reason"]}, ensure_ascii=False)

                    sub_details = await get_subscription_by_external_id(owner_phone, db)
                    if sub_details is None:
                        return json.dumps({"error": "Subscription tidak ditemukan."}, ensure_ascii=False)
                    _, _, plan = sub_details
                    entitlement_errors = validate_agent_entitlements(
                        plan,
                        model=model,
                        tools_config=tc,
                        channel_type=channel_type or None,
                    )
                    if entitlement_errors:
                        return json.dumps(
                            {
                                "error": "Konfigurasi agent melebihi entitlement plan.",
                                "plan": plan.label,
                                "violations": entitlement_errors,
                            },
                            ensure_ascii=False,
                        )

                    # Override token_quota & active_until dari subscription
                    token_quota = _sub.token_quota
                    _active_until = _sub.expires_at or _sub.grace_until
                else:
                    _active_until = None

                wa_device_id = str(uuid.uuid4()) if channel_type == "whatsapp" else None

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
                    wa_device_id=wa_device_id,
                    owner_external_id=owner_phone,
                    created_by_type="arthur_builder",
                    created_by_agent_id=str(self_agent_id) if self_agent_id else None,
                    created_by_agent_name="Arthur",
                )
                if _active_until:
                    agent.active_until = _active_until

                db.add(agent)
                await db.flush()
                await db.refresh(agent)
                if hasattr(Agent, "__table__"):
                    await upsert_agent_operating_manual(
                        agent.id,
                        generated_operating_manual,
                        db,
                        created_by_agent_id=str(self_agent_id) if self_agent_id else None,
                        version=int(generated_operating_manual.get("version") or 1),
                    )

                memory_keys_seeded: list[str] = []
                builder_memory_updated = False
                if soul.strip() or blueprint.strip() or (platform_identity_added and hasattr(Agent, "__table__")):
                    from app.core.domain.memory_service import upsert_memory

                    if soul.strip():
                        await upsert_memory(agent.id, "soul", soul.strip(), db, scope=None)
                        memory_keys_seeded.append("soul")
                    if blueprint.strip():
                        await upsert_memory(agent.id, "agent_blueprint", blueprint.strip(), db, scope=None)
                        memory_keys_seeded.append("agent_blueprint")
                    if platform_identity_added and hasattr(Agent, "__table__"):
                        await upsert_memory(
                            agent.id,
                            "platform_identity",
                            _platform_staff_identity_block(
                                owner_phone=owner_phone,
                                operator_phone=operator_phone,
                                operator_name=operator_name,
                            ),
                            db,
                            scope=None,
                        )
                        memory_keys_seeded.append("platform_identity")

                if self_agent_id:
                    try:
                        from app.core.domain.memory_service import upsert_memory

                        self_uuid = uuid.UUID(str(self_agent_id))
                        await upsert_memory(self_uuid, "last_agent_id", str(agent.id), db, scope=owner_phone)
                        await upsert_memory(
                            self_uuid,
                            f"agent_id:{agent.name.strip().lower()}",
                            str(agent.id),
                            db,
                            scope=owner_phone,
                        )
                        builder_memory_updated = True
                    except Exception as exc:
                        logger.warning(
                            "builder_tools.create_agent.builder_memory_update_failed",
                            error=str(exc),
                            self_agent_id=self_agent_id,
                            owner_phone=owner_phone,
                        )

                await db.commit()

            logger.info(
                "builder_tools.create_agent.success",
                agent_id=str(agent.id),
                name=agent.name,
                owner_phone=owner_phone,
            )
            created_by_metadata = _agent_created_by_metadata(agent)

            return json.dumps({
                "success": True,
                "agent_id": str(agent.id),
                "name": agent.name,
                "model": agent.model,
                "channel_type": agent.channel_type,
                "google_workspace_enabled": _has_google_workspace_tools(tc),
                "needs_google_auth": _has_google_workspace_tools(tc),
                **created_by_metadata,
                "platform_identity_added": platform_identity_added,
                "operating_manual": summarize_operating_manual(generated_operating_manual),
                "whatsapp_onboarding_required": agent.channel_type == "whatsapp",
                "api_key": agent.api_key,
                "token_quota": agent.token_quota,
                "active_until": agent.active_until.isoformat() if agent.active_until else None,
                "memory_keys_seeded": memory_keys_seeded,
                "builder_memory_updated": builder_memory_updated,
                "message": (
                    f"Agent '{agent.name}' berhasil dibuat dengan ID: {agent.id}. "
                    "Simpan agent_id ini sebagai target utama untuk aksi lanjutan pada percakapan ini. "
                    "Jika channel_type adalah whatsapp, jawaban ke user WAJIB langsung lanjut ke onboarding: "
                    "'Mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, atau dicoba dulu lewat nomor demo Arthur yang sudah siap pakai?' "
                    "Jangan berhenti hanya dengan menyebut agent sudah jadi atau ID agent. "
                    "Jika google_workspace_enabled=true, langkah berikutnya adalah generate_google_auth_link lalu kirim link login Google; "
                    "jangan menunggu user bertanya cara koneknya. "
                    "Jika user meminta nomor trial/link coba setelah ini, panggil create_wa_dev_trial_link "
                    "dengan agent_id ini, bukan agent lama dari history. "
                    "Jangan panggil compose_agent_soul setelah create hanya untuk melengkapi memory; itu opsional dan boleh ditunda. "
                    "Lebih efisien: untuk create berikutnya, isi parameter soul dan blueprint langsung saat create_agent jika sudah tersedia."
                ),
            }, ensure_ascii=False, indent=2)

        except Exception as exc:
            logger.error("builder_tools.create_agent.error", error=str(exc), owner_phone=owner_phone)
            return f"[error] Gagal membuat agent: {exc}"

    # ------------------------------------------------------------------ #
    # 4a. create_wa_dev_trial_link                                        #
    # ------------------------------------------------------------------ #

    @tool
    async def create_wa_dev_trial_link(
        agent_id: str = "",
        phone: str = "",
        force_new_code: bool = False,
        send_contact: bool = True,
    ) -> str:
        """
        Generate kode 6 karakter untuk mencoba agent lewat nomor demo Arthur.

        Gunakan setelah create_agent saat user ingin mencoba agent di WhatsApp tanpa
        punya nomor khusus. Kirimkan hasilnya ke user sebagai opsi:
        "Mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, atau
        dicoba dulu lewat nomor demo Arthur yang sudah siap pakai?"

        Args:
            agent_id: UUID agent yang akan dicoba di nomor shared Arthur. Jika kosong,
                      tool memilih agent non-builder terbaru milik user saat ini.
            phone: Nomor/JID tujuan untuk dikirimi vCard. Kosong = user saat ini.
            force_new_code: True untuk rotate kode lama
            send_contact: True untuk kirim contact card nomor shared Arthur ke user
        """
        agent_uuid: uuid.UUID | None = None
        if agent_id and self_agent_id and str(agent_id) == str(self_agent_id):
            agent_id = ""
        if agent_id:
            try:
                agent_uuid = uuid.UUID(agent_id)
            except ValueError:
                return f"[error] agent_id tidak valid: {agent_id}"

        target = phone or default_target or owner_phone or ""
        settings = get_settings()
        contact_name = settings.wa_dev_public_name or "Arthur AI Dev"

        async with db_factory() as db:
            if agent_uuid:
                result = await db.execute(
                    select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
                )
                agent = result.scalar_one_or_none()
            else:
                agent = await _latest_owned_agent_for_trial(
                    db,
                    owner_phone=owner_phone,
                    self_agent_id=self_agent_id,
                )
            if not agent:
                return (
                    f"[error] Agent dengan ID {agent_id} tidak ditemukan"
                    if agent_id
                    else "[error] Tidak menemukan agent terbaru milik user untuk dibuatkan trial link"
                )
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses ke agent ini"
            resolved_agent_id = str(agent.id)
            resolved_agent_name = agent.name

            from app.core.domain.wa_dev_trial_service import ensure_wa_dev_trial_code

            code = await ensure_wa_dev_trial_code(db, agent, force_new=force_new_code)
            await db.commit()

        shared_phone = normalize_phone(settings.wa_dev_public_phone)
        wa_status_error = ""
        if not shared_phone:
            try:
                from app.core.infra.wa_client import get_wa_dev_status

                status = await get_wa_dev_status()
                shared_phone = normalize_phone(status.get("phone_number") or "")
            except Exception as exc:
                wa_status_error = str(exc)

        if not shared_phone:
            return json.dumps({
                "success": True,
                "agent_id": resolved_agent_id,
                "agent_name": resolved_agent_name,
                "code": code,
                "contact_sent": False,
                "warning": (
                    "Kode berhasil dibuat, tapi WA_DEV_PUBLIC_PHONE belum dikonfigurasi "
                    "dan nomor wa-dev-service tidak bisa dibaca."
                ),
                "wa_status_error": wa_status_error,
            }, ensure_ascii=False, indent=2)

        prefill = f"Halo Arthur, saya mau coba agent saya. Kode saya: {code}"
        wa_me_url = f"https://wa.me/{shared_phone}?text={quote(prefill)}"

        contact_sent = False
        contact_error = ""
        if send_contact and target:
            if device_id and not device_id.startswith("wadev_"):
                try:
                    from app.core.infra.wa_client import send_wa_contact

                    await send_wa_contact(device_id, target, contact_name, shared_phone)
                    contact_sent = True
                except Exception as exc:
                    contact_error = str(exc)
            elif device_id and device_id.startswith("wadev_"):
                contact_error = (
                    "Arthur sedang berjalan lewat nomor shared wa-dev, jadi vCard tidak dikirim "
                    "agar kontak tidak terlihat dikirim dari nomor trial itu sendiri."
                )
            else:
                contact_error = "Arthur session tidak punya device_id WhatsApp, jadi vCard tidak bisa dikirim dari nomor Arthur."

        return json.dumps({
            "success": True,
            "agent_id": resolved_agent_id,
            "agent_name": resolved_agent_name,
            "code": code,
            "shared_whatsapp_name": contact_name,
            "shared_whatsapp_phone": f"+{shared_phone}",
            "wa_me_url": wa_me_url,
            "contact_sent": contact_sent,
            "contact_error": contact_error,
            "instruction_for_user": (
                f"Simpan kontak {contact_name}, atau buka link wa.me. "
                f"Kirim kode {code} untuk menghubungkan WhatsApp ke agent ini. "
                "Kode bisa dipakai ulang; kirim /stop di WhatsApp kalau ingin disconnect. "
                "Untuk switch agent, minta kode baru dari Arthur lalu kirim kode baru itu."
            ),
        }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 4b. set_agent_memory                                                #
    # ------------------------------------------------------------------ #

    @tool
    async def set_agent_memory(agent_id: str, key: str, value: str) -> str:
        """
        Simpan/update memory global untuk agent milik user ini secara langsung ke database.
        Gunakan ini sebagai fallback jika soul/blueprint belum dikirim saat create_agent.

        Args:
            agent_id: UUID agent yang memory-nya akan diubah
            key: Nama memory, misalnya "soul" atau "agent_blueprint"
            value: Isi memory
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"
        if not key.strip():
            return "[error] key memory wajib diisi"
        if not value.strip():
            return "[error] value memory wajib diisi"

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses ke agent ini"

            from app.core.domain.memory_service import upsert_memory

            await upsert_memory(agent.id, key.strip(), value.strip(), db, scope=None)
            await db.commit()

        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "key": key.strip(),
            "message": f"Memory '{key.strip()}' berhasil disimpan.",
        }, ensure_ascii=False, indent=2)

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
        enable_google_workspace: bool = False,
        refresh_memory_mode: str = "selective",
        business_context: str = "",
        domain: str = "",
        operating_manual: Any = None,
    ) -> str:
        """
        Update konfigurasi agent yang sudah ada. Hanya field yang diisi yang akan diubah.
        Hanya bisa mengupdate agent yang dimiliki oleh user ini (owner_phone).
        Untuk mengaktifkan kemampuan Google Docs/Sheets/Drive/Gmail/Calendar, gunakan
        enable_google_workspace=True agar tools_config dan instruksi agent diperbarui sekaligus.

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
            enable_google_workspace: True untuk mengaktifkan integrasi Google Workspace
                                     dan menambahkan instruksi operasional Google ke agent.
            refresh_memory_mode: "none" | "selective" | "major".
                                 Default selective: update workflow/persona menulis memory versi aktif baru.
            business_context: Konteks bisnis terbaru untuk refresh SOP agent jika workflow berubah.
            domain: Domain bisnis terbaru jika SOP perlu dibuat ulang.
            operating_manual: Agent Operating Manual/SOP artifact baru. Jika diisi, disimpan sebagai versi baru.
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        google_workspace_enabled = False
        normalized_refresh_memory_mode = _normalize_refresh_memory_mode(refresh_memory_mode)
        memory_refresh_result: dict[str, Any] = {"mode": normalized_refresh_memory_mode, "updated": False, "keys": []}
        operating_manual_result: dict[str, Any] = {"updated": False}

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
                if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                    return (
                        "[error] Hanya operator yang terdaftar yang boleh memodifikasi konfigurasi agent builder ini. "
                        f"Nomor kamu ({owner_phone}) tidak ada di daftar operator."
                    )
            elif owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return f"[error] Kamu tidak punya akses ke agent ini. Hanya agent milikmu yang bisa diubah."

            updated_fields: list[str] = []

            policy_reason = _blocked_agent_policy_reason(
                name,
                instructions,
                description,
                tools_config,
                escalation_config,
            )
            if policy_reason:
                return json.dumps({"error": policy_reason}, ensure_ascii=False)

            if name and name.strip():
                dup_result = await db.execute(
                    select(Agent).where(
                        Agent.id != agent_uuid,
                        func.lower(Agent.name) == name.strip().lower(),
                        Agent.is_deleted.is_(False),
                        _owner_filter(owner_phone),
                    )
                )
                duplicate = dup_result.scalar_one_or_none()
                if duplicate and getattr(duplicate, "id", None) != agent_uuid:
                    return json.dumps({
                        "error": f"Agent lain dengan nama '{name.strip()}' sudah ada.",
                        "existing_agent_id": str(duplicate.id),
                        "hint": "Pilih nama agent yang unik atau update agent tersebut memakai existing_agent_id.",
                    }, ensure_ascii=False)
                agent.name = name.strip()
                updated_fields.append("name")

            if instructions:
                clean_instructions = instructions.strip()
                if _looks_like_destructive_instruction_shrink(agent.instructions, clean_instructions):
                    return json.dumps(
                        {
                            "error": "Instruksi baru terlalu pendek dibanding instruksi agent yang sudah ada.",
                            "current_instructions_len": len(agent.instructions or ""),
                            "new_instructions_len": len(clean_instructions),
                            "hint": (
                                "Panggil get_agent_detail(agent_id, include_instructions=true), "
                                "gabungkan kebutuhan baru ke instruksi lama, lalu update ulang dengan instruksi lengkap."
                            ),
                        },
                        ensure_ascii=False,
                    )
                agent.instructions = clean_instructions
                updated_fields.append("instructions")
                if _has_google_workspace_tools(agent.tools_config if isinstance(agent.tools_config, dict) else {}):
                    google_workspace_enabled = True
                    updated_instructions, changed_instructions = _append_google_workspace_instruction(
                        agent.instructions or ""
                    )
                    if changed_instructions:
                        agent.instructions = updated_instructions
                        updated_fields.append("instructions+google_workspace")

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
                    existing.setdefault("tavily", True)
                    agent.tools_config = existing
                    updated_fields.append("tools_config")
                    if _has_google_workspace_tools(agent.tools_config):
                        google_workspace_enabled = True
                        updated_instructions, changed_instructions = _append_google_workspace_instruction(
                            agent.instructions or ""
                        )
                        if changed_instructions:
                            agent.instructions = updated_instructions
                            updated_fields.append("instructions+google_workspace")
                except json.JSONDecodeError:
                    return "[error] tools_config bukan JSON yang valid"

            if enable_google_workspace:
                before_google = _has_google_workspace_tools(
                    agent.tools_config if isinstance(agent.tools_config, dict) else {}
                )
                agent.tools_config = _enable_google_workspace_tools(
                    agent.tools_config if isinstance(agent.tools_config, dict) else {}
                )
                google_workspace_enabled = True
                if not before_google and "tools_config" not in updated_fields:
                    updated_fields.append("tools_config")
                if before_google:
                    updated_fields.append("google_workspace_already_enabled")

                updated_instructions, changed_instructions = _append_google_workspace_instruction(
                    agent.instructions or ""
                )
                if changed_instructions:
                    agent.instructions = updated_instructions
                    updated_fields.append("instructions+google_workspace")

            if _has_google_workspace_tools(agent.tools_config if isinstance(agent.tools_config, dict) else {}):
                google_workspace_enabled = True

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

            workflow_sensitive_update = any(
                field in updated_fields
                for field in (
                    "name",
                    "instructions",
                    "description",
                    "tools_config",
                    "escalation_config",
                    "instructions+google_workspace",
                )
            )
            if workflow_sensitive_update:
                critical_errors = _critical_workflow_config_errors(
                    name=agent.name or "",
                    description=agent.description or "",
                    instructions=agent.instructions or "",
                    tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                    soul="",
                    blueprint="",
                )
                if critical_errors:
                    return json.dumps({
                        "error": "Konfigurasi agent belum aman untuk diupdate.",
                        "validation_errors": critical_errors,
                        "hint": (
                            "Panggil get_agent_detail(agent_id, include_instructions=true), "
                            "compose_agent_blueprint dan compose_agent_instructions ulang, lalu update_agent "
                            "dengan instructions dan tools_config lengkap."
                        ),
                    }, ensure_ascii=False, indent=2)

            manual_update_requested = bool(
                operating_manual not in (None, "", {})
                or business_context.strip()
                or domain.strip()
            )
            if workflow_sensitive_update or manual_update_requested:
                existing_manual = await get_latest_agent_operating_manual(
                    agent.id,
                    db,
                    fallback_tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                )
                target_version = (agent.version or 1) + 1
                if manual_update_requested or not existing_manual:
                    updated_tc, next_manual = ensure_operating_manual_in_tools_config(
                        agent.tools_config if isinstance(agent.tools_config, dict) else {},
                        name=agent.name or "",
                        description=agent.description or "",
                        instructions=agent.instructions or "",
                        business_context=business_context,
                        domain=domain,
                        operating_manual=operating_manual,
                    )
                    agent.tools_config = updated_tc
                    if "tools_config" not in updated_fields:
                        updated_fields.append("tools_config")
                else:
                    next_manual = dict(existing_manual)
                    next_manual["version"] = target_version
                    next_manual["maturity"] = "needs_review"
                    next_manual["owner_review_required"] = True
                    missing_context = list(next_manual.get("missing_context") or [])
                    review_note = "SOP perlu review ulang karena workflow/config agent berubah."
                    if review_note not in missing_context:
                        missing_context.append(review_note)
                    next_manual["missing_context"] = missing_context
                next_manual["version"] = target_version
                if hasattr(Agent, "__table__"):
                    await upsert_agent_operating_manual(
                        agent.id,
                        next_manual,
                        db,
                        created_by_agent_id=str(self_agent_id) if self_agent_id else None,
                        version=target_version,
                    )
                operating_manual_result = summarize_operating_manual(next_manual)
                operating_manual_result["updated"] = True

            identity_sensitive_update = bool(
                instructions
                or description
                or tools_config
                or escalation_config
                or enable_google_workspace
            )
            if identity_sensitive_update:
                owner_for_identity = (
                    owner_phone
                    or getattr(agent, "owner_external_id", None)
                    or next(iter(getattr(agent, "operator_ids", None) or []), "")
                )
                if owner_for_identity:
                    agent_ec = agent.escalation_config if isinstance(agent.escalation_config, dict) else {}
                    updated_instructions, changed_instructions = _append_platform_staff_identity_instruction(
                        agent.instructions or "",
                        owner_phone=owner_for_identity,
                        operator_phone=str(agent_ec.get("operator_phone", "") or ""),
                        operator_name=str(agent_ec.get("operator_name", "") or ""),
                    )
                    if changed_instructions:
                        agent.instructions = updated_instructions
                        updated_fields.append("instructions+platform_identity")

            entitlement_sensitive_update = any(
                field in updated_fields for field in ("model", "tools_config")
            )
            if entitlement_sensitive_update and not is_self_update and owner_phone and hasattr(Agent, "__table__"):
                from app.core.domain.subscription_service import (
                    get_subscription_by_external_id,
                    validate_agent_entitlements,
                )

                sub_details = await get_subscription_by_external_id(owner_phone, db)
                if sub_details is None:
                    return json.dumps({"error": "Subscription tidak ditemukan."}, ensure_ascii=False)
                _, _, plan = sub_details
                entitlement_errors = validate_agent_entitlements(
                    plan,
                    model=agent.model,
                    tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                    channel_type=agent.channel_type,
                )
                if entitlement_errors:
                    return json.dumps(
                        {
                            "error": "Konfigurasi agent melebihi entitlement plan.",
                            "plan": plan.label,
                            "violations": entitlement_errors,
                        },
                        ensure_ascii=False,
                    )

            agent.version = (agent.version or 1) + 1
            if identity_sensitive_update:
                memory_refresh_result = await _refresh_agent_context_memory(
                    db=db,
                    agent=agent,
                    mode=normalized_refresh_memory_mode,
                    updated_fields=updated_fields,
                )
            await db.commit()

        logger.info("builder_tools.update_agent.success", agent_id=agent_id, fields=updated_fields)
        response = {
            "success": True,
            "agent_id": agent_id,
            "agent_name": agent.name,
            "updated_fields": updated_fields,
            "new_version": agent.version,
            "memory_refresh": memory_refresh_result,
            "operating_manual": operating_manual_result,
            "message": f"Agent '{agent.name}' sudah saya edit.",
        }
        if google_workspace_enabled:
            response["google_workspace_enabled"] = True
            response["needs_google_auth"] = True
            response["readback"] = {
                "tools_config_has_google_workspace": _has_google_workspace_tools(
                    agent.tools_config if isinstance(agent.tools_config, dict) else {}
                ),
                "instructions_include_google_workspace": "Google Workspace" in (agent.instructions or ""),
            }
            response["next_step"] = (
                "Panggil get_agent_detail(agent_id) untuk verifikasi readback. "
                "Setelah readback benar, panggil generate_google_auth_link(agent_id, external_user_id=nomor user saat ini) "
                "dan kirim link otentikasi Google ke user jika tersedia. "
                "Saat menjelaskan ke user, sebut 'integrasi Google/Google Docs', jangan sebut istilah teknis internal/protokol tool."
            )
        return json.dumps(response, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 6. delete_agent                                                     #
    # ------------------------------------------------------------------ #

    @tool
    async def delete_agent(
        agent_id: str,
        confirm_name: str = "",
    ) -> str:
        """
        Hapus agent milik user ini secara soft-delete.

        Gunakan hanya setelah user eksplisit meminta hapus/delete agent dan sudah
        mengonfirmasi nama agent. Jika user belum menyebut agent mana, panggil
        list_my_agents() dulu. Jika confirm_name kosong atau tidak sama dengan
        nama agent, tool akan meminta konfirmasi dan tidak menghapus.

        Args:
            agent_id: UUID agent yang akan dihapus
            confirm_name: Nama agent persis sebagai konfirmasi hapus
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        if self_agent_id and str(agent_uuid) == self_agent_id:
            return "[error] Arthur tidak boleh menghapus dirinya sendiri."

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"
            if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return "[error] Kamu tidak punya akses untuk menghapus agent ini"

            expected_name = (agent.name or "").strip()
            if not confirm_name or confirm_name.strip() != expected_name:
                return json.dumps({
                    "success": False,
                    "needs_confirmation": True,
                    "agent_id": str(agent.id),
                    "agent_name": expected_name,
                    "message": (
                        f"Konfirmasi dulu sebelum menghapus agent '{expected_name}'. "
                        "Panggil delete_agent lagi dengan confirm_name persis sama dengan nama agent."
                    ),
                }, ensure_ascii=False, indent=2)

            wa_device_id = agent.wa_device_id
            wa_disconnect_error = ""
            if wa_device_id and not str(wa_device_id).startswith("wadev_"):
                try:
                    from app.core.infra.wa_client import delete_wa_device

                    await delete_wa_device(wa_device_id)
                except Exception as exc:
                    wa_disconnect_error = str(exc)
                    logger.warning(
                        "builder_tools.delete_agent.wa_disconnect_failed",
                        agent_id=str(agent.id),
                        error=wa_disconnect_error,
                    )

            agent.is_deleted = True
            agent.version = (agent.version or 1) + 1
            await db.commit()

        logger.info("builder_tools.delete_agent.success", agent_id=agent_id, owner_phone=owner_phone)
        return json.dumps({
            "success": True,
            "agent_id": agent_id,
            "agent_name": expected_name,
            "wa_device_id": wa_device_id,
            "wa_disconnect_error": wa_disconnect_error,
            "message": f"Agent '{expected_name}' berhasil dihapus.",
        }, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 7. get_agent_detail                                                 #
    # ------------------------------------------------------------------ #

    @tool
    async def get_agent_detail(agent_id: str, include_instructions: bool = False) -> str:
        """
        Baca konfigurasi lengkap sebuah agent. Gunakan untuk review sebelum update,
        atau untuk debugging konfigurasi agent yang sudah ada.

        Args:
            agent_id: UUID agent yang ingin dilihat
            include_instructions: True jika perlu membaca full instructions sebelum update.
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
        if not is_self and owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
            return f"[error] Kamu tidak punya akses ke agent ini"

        memory_summary: dict[str, str] = {}
        try:
            from app.core.domain.memory_service import get_memory

            async with db_factory() as db:
                soul_mem = await get_memory(agent.id, "soul", db, scope=None)
                blueprint_mem = await get_memory(agent.id, "agent_blueprint", db, scope=None)
                platform_identity_mem = await get_memory(agent.id, "platform_identity", db, scope=None)
            if soul_mem:
                soul_value = getattr(soul_mem, "value_data", "")
                if isinstance(soul_value, str):
                    memory_summary["soul_preview"] = soul_value[:500] + (
                        "..." if len(soul_value) > 500 else ""
                    )
            if blueprint_mem:
                blueprint_value = getattr(blueprint_mem, "value_data", "")
                if isinstance(blueprint_value, str):
                    memory_summary["agent_blueprint_preview"] = blueprint_value[:800] + (
                        "..." if len(blueprint_value) > 800 else ""
                    )
            if platform_identity_mem:
                platform_identity_value = getattr(platform_identity_mem, "value_data", "")
                if isinstance(platform_identity_value, str):
                    memory_summary["platform_identity_preview"] = platform_identity_value[:500] + (
                        "..." if len(platform_identity_value) > 500 else ""
                    )
        except Exception as exc:
            memory_summary["memory_warning"] = f"Gagal membaca memory agent: {exc}"

        instructions_text = agent.instructions or ""
        created_by_metadata = _agent_created_by_metadata(agent)
        operating_manual = None
        try:
            async with db_factory() as db:
                operating_manual = await get_latest_agent_operating_manual(
                    agent.id,
                    db,
                    fallback_tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                )
        except Exception as exc:
            memory_summary["operating_manual_warning"] = f"Gagal membaca SOP agent: {exc}"
        payload = {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "model": agent.model,
            "temperature": agent.temperature,
            "tools_config": agent.tools_config,
            "google_workspace_enabled": _has_google_workspace_tools(
                agent.tools_config if isinstance(agent.tools_config, dict) else {}
            ),
            "instructions_include_google_workspace": "Google Workspace" in (agent.instructions or ""),
            "escalation_config": agent.escalation_config,
            "operator_ids": agent.operator_ids,
            "allowed_senders": agent.allowed_senders,
            **created_by_metadata,
            "launch_metadata": {
                "owner_present": bool(getattr(agent, "owner_external_id", None) or (getattr(agent, "operator_ids", None) or [])),
                "created_by_present": bool(created_by_metadata["created_by_type"]),
                "created_by_arthur": created_by_metadata["created_by_type"] == "arthur_builder",
                "operating_manual": summarize_operating_manual(operating_manual),
            },
            "channel_type": agent.channel_type,
            "wa_device_id": agent.wa_device_id,
            "token_quota": agent.token_quota,
            "tokens_used": agent.tokens_used,
            "active_until": agent.active_until.isoformat() if agent.active_until else None,
            "version": agent.version,
            "instructions_len": len(instructions_text),
            "instructions_preview": instructions_text[:300] + ("..." if len(instructions_text) > 300 else ""),
            "memory": memory_summary,
        }
        if include_instructions:
            payload["instructions"] = instructions_text
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------ #
    # 8. list_my_agents                                                   #
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

                my_agents = [a for a in all_agents if _agent_belongs_to_owner(a, owner_phone)]

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
                        **_agent_created_by_metadata(a),
                        "launch_metadata": {
                            "owner_present": bool(getattr(a, "owner_external_id", None) or (getattr(a, "operator_ids", None) or [])),
                            "created_by_present": bool(_agent_created_by_metadata(a)["created_by_type"]),
                            "created_by_arthur": _agent_created_by_metadata(a)["created_by_type"] == "arthur_builder",
                        },
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

    @tool
    async def generate_google_auth_link(
        agent_id: str,
        external_user_id: str,
    ) -> str:
        """
        Generate link untuk user connect akun Google mereka ke agent tertentu.
        Gunakan tool ini setiap kali user minta link auth Google, atau setelah
        create/update agent yang punya integrasi Google Workspace.

        Setelah dapat auth_url, kirimkan HANYA link-nya ke user — jangan tampilkan
        endpoint, parameter teknis, atau istilah internal/protokol tool.

        Args:
            agent_id: ID agent yang akan dihubungkan ke Google
            external_user_id: ID user saat ini (dari session yang sedang berjalan)
        """
        import httpx

        settings = get_settings()
        integration_url = str(settings.google_integration_service_url).rstrip("/")
        if not integration_url:
            return "[error] GOOGLE_INTEGRATION_SERVICE_URL belum dikonfigurasi; auth Google Workspace harus memakai URL dev tunnel."

        if is_probable_whatsapp_lid(external_user_id):
            return (
                "[error] Nomor WhatsApp asli user belum tersedia, jadi link login Google belum bisa dibuat. "
                "Minta user chat dari nomor WhatsApp biasa atau pastikan wa-service mengirim phone_from, bukan LID."
            )
        candidate_user_ids = [
            candidate
            for candidate in _candidate_external_user_ids(external_user_id, external_user_id)
            if not is_probable_whatsapp_lid(candidate)
        ]
        if not candidate_user_ids:
            return (
                "[error] Nomor WhatsApp asli user belum tersedia, jadi link login Google belum bisa dibuat. "
                "Minta user chat dari nomor WhatsApp biasa atau pastikan wa-service mengirim phone_from, bukan LID."
            )

        last_status = ""
        last_body = ""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                for candidate in candidate_user_ids:
                    resp = await client.post(
                        f"{integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": candidate, "agent_id": agent_id},
                        headers={"X-API-Key": settings.api_key},
                    )
                    last_status = str(resp.status_code)
                    last_body = resp.text[:300]
                    if resp.status_code != 200:
                        continue
                    data = resp.json() if resp.text else {}
                    auth_url = data.get("auth_url") or data.get("authorization_url", "")
                    if auth_url:
                        return json.dumps(
                            {
                                "auth_url": auth_url,
                                "external_user_id": candidate,
                                "integration_url_used": integration_url,
                            },
                            ensure_ascii=False,
                        )
                    last_body = resp.text[:300] or "response JSON tidak mengandung auth_url"
            return (
                f"[error] Gagal generate link Google. status={last_status or 'no_response'} "
                f"body={last_body or '-'}"
            )
        except httpx.TimeoutException as exc:
            logger.error(
                "builder_tools.generate_google_auth_link.error",
                error_type=type(exc).__name__,
                error=repr(exc),
                integration_url=integration_url,
                candidates=candidate_user_ids,
            )
            return (
                f"[error] Timeout saat menghubungi Google integration service di {integration_url}. "
                "Pastikan service integration jalan dan GOOGLE_INTEGRATION_SERVICE_URL/WORKSPACE_MCP_PREFER_LOCAL benar."
            )
        except httpx.HTTPError as exc:
            logger.error(
                "builder_tools.generate_google_auth_link.error",
                error_type=type(exc).__name__,
                error=repr(exc),
                integration_url=integration_url,
                candidates=candidate_user_ids,
            )
            return f"[error] Gagal menghubungi Google integration service ({type(exc).__name__}): {exc!r}"
        except Exception as exc:
            logger.error(
                "builder_tools.generate_google_auth_link.error",
                error_type=type(exc).__name__,
                error=repr(exc),
                integration_url=integration_url,
                candidates=candidate_user_ids,
            )
            return f"[error] Gagal generate link Google ({type(exc).__name__}): {exc!r}"

    return [
        get_self_config,
        get_platform_capabilities,
        get_user_subscription,
        get_presets,
        plan_agent,
        compose_agent_blueprint,
        compose_agent_operating_manual,
        compose_agent_instructions,
        compose_agent_soul,
        verify_agent,
        list_available_wa_devices,
        validate_agent_config,
        create_agent,
        create_wa_dev_trial_link,
        set_agent_memory,
        update_agent,
        delete_agent,
        get_agent_detail,
        list_my_agents,
        generate_google_auth_link,
    ]

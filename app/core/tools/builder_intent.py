"""Intent and workflow classifiers for Arthur builder tools."""
from __future__ import annotations

import json
import re
from typing import Any


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


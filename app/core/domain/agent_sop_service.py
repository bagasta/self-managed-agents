from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_operating_manual import AgentOperatingManual

OPERATING_MANUAL_KEY = "operating_manual"

_MIN_CONTEXT_CHARS = 240

_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "food_beverage": (
        "f&b",
        "restoran",
        "restaurant",
        "cafe",
        "kafe",
        "makanan",
        "minuman",
        "menu",
        "order makanan",
        "pesan makanan",
    ),
    "travel": (
        "travel",
        "trip",
        "wisata",
        "tour",
        "itinerary",
        "tiket",
        "hotel",
        "umroh",
        "haji",
    ),
    "ecommerce": (
        "ecommerce",
        "e-commerce",
        "toko",
        "produk",
        "katalog",
        "checkout",
        "refund",
        "marketplace",
    ),
    "local_service": (
        "jasa",
        "booking",
        "survey",
        "quotation",
        "penawaran",
        "invoice",
        "revisi",
    ),
    "event_service": (
        "acara",
        "event",
        "pesta",
        "ulang tahun",
        "wedding",
        "pernikahan",
        "jumlah tamu",
        "tamu",
        "antar pasang",
        "dekorasi",
        "tenda",
        "kursi",
        "sound system",
        "perlengkapan acara",
        "rental alat pesta",
    ),
    "clinic_wellness": (
        "klinik",
        "clinic",
        "dokter",
        "appointment",
        "wellness",
        "terapi",
        "treatment",
    ),
    "education": (
        "kursus",
        "kelas",
        "sekolah",
        "belajar",
        "pendidikan",
        "tutor",
        "training",
    ),
    "property": (
        "properti",
        "property",
        "rumah",
        "apartemen",
        "unit",
        "viewing",
        "sewa",
        "jual beli",
    ),
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "workflow"


def _combined_text(*parts: Any) -> str:
    return "\n".join(str(part or "") for part in parts if str(part or "").strip())


def _as_text_list(value: Any, *, fallback: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        result = [str(item).strip() for item in value if str(item or "").strip()]
        return result or list(fallback or [])
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return list(fallback or [])


def _parse_json_like(value: Any) -> Any:
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def detect_sop_domain(*parts: Any, requested_domain: str = "") -> tuple[str, str]:
    requested = (requested_domain or "").strip().lower()
    if requested:
        normalized = _slug(requested)
        if normalized in _DOMAIN_KEYWORDS:
            return normalized, "high"
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            if requested in keywords:
                return domain, "high"
        return requested, "medium"

    text = _combined_text(*parts).lower()
    event_markers = (
        "tanggal acara",
        "jumlah tamu",
        "antar pasang",
        "perlengkapan acara",
        "rental alat pesta",
    )
    if "acara" in text and any(marker in text for marker in event_markers):
        return "event_service", "high"

    scores: dict[str, int] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in text)
        if score:
            scores[domain] = score
    if not scores:
        return "generic", "low"

    domain, score = max(scores.items(), key=lambda item: item[1])
    return domain, "high" if score >= 2 else "medium"


def _workflow(
    workflow_id: str,
    name: str,
    trigger: str,
    goal: str,
    required_inputs: list[str],
    steps: list[str],
    decision_points: list[str],
    escalation_rules: list[str],
    prohibited_actions: list[str],
    final_output: str,
    allowed_tools: list[str] | None = None,
    examples: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "name": name,
        "trigger": trigger,
        "goal": goal,
        "required_inputs": required_inputs,
        "steps": steps,
        "decision_points": decision_points,
        "allowed_tools": allowed_tools or [],
        "escalation_rules": escalation_rules,
        "prohibited_actions": prohibited_actions,
        "final_output": final_output,
        "examples": examples or [],
    }


def _generic_workflows(domain: str) -> list[dict[str, Any]]:
    return [
        _workflow(
            "customer_intake",
            "Terima dan pahami kebutuhan customer",
            "Customer menyapa, bertanya, atau menjelaskan kebutuhan.",
            "Mengumpulkan kebutuhan customer dengan aman sebelum membuat janji bisnis.",
            ["nama customer", "kebutuhan utama", "kontak yang bisa dihubungi", "deadline atau waktu yang diinginkan"],
            [
                "Sapa customer dengan singkat dan ramah.",
                "Tanyakan kebutuhan utama customer.",
                "Kumpulkan data wajib yang belum ada.",
                "Ringkas kebutuhan customer sebelum melanjutkan.",
                "Jika ada keputusan harga, stok, legal, medis, finansial, atau approval, eskalasi ke Owner/operator.",
            ],
            [
                "Jika data wajib belum lengkap, tanya data yang kurang.",
                "Jika customer meminta keputusan yang belum ada di SOP, eskalasi.",
            ],
            ["Eskalasi jika agent tidak punya kebijakan, harga, stok, jadwal, approval, atau data bisnis yang pasti."],
            [
                "Jangan mengarang harga, stok, jadwal, kebijakan refund, keputusan legal/medis/finansial, atau janji final.",
                "Jangan mengklaim sudah memproses tindakan eksternal jika tool/action belum berhasil.",
            ],
            "Ringkasan kebutuhan customer dan next step yang aman.",
        )
    ]


def _template_workflows(domain: str) -> list[dict[str, Any]]:
    if domain == "food_beverage":
        return [
            _workflow(
                "order_intake",
                "Terima order makanan/minuman",
                "Customer ingin pesan menu, tanya menu, atau tanya delivery/pickup.",
                "Mengumpulkan detail order sampai siap dikonfirmasi Owner/kasir.",
                ["nama customer", "menu dan jumlah", "alamat atau metode pickup", "waktu pesanan", "metode pembayaran"],
                [
                    "Tanyakan menu dan jumlah pesanan.",
                    "Tanyakan delivery atau pickup.",
                    "Kumpulkan alamat/waktu/metode pembayaran jika relevan.",
                    "Ringkas order dan minta customer mengecek ulang.",
                    "Eskalasi untuk konfirmasi stok, harga final, promo, atau pembayaran jika belum tersedia di tool/data.",
                ],
                ["Jika stok/harga tidak tersedia, jangan konfirmasi final.", "Jika customer komplain, pindah ke workflow komplain."],
                ["Eskalasi untuk pembayaran bermasalah, komplain berat, perubahan order setelah diproses, atau stok tidak pasti."],
                ["Jangan menjanjikan stok/harga/promo yang tidak ada di data.", "Jangan menyatakan pembayaran berhasil tanpa bukti/tool."],
                "Ringkasan order yang siap ditindaklanjuti.",
            )
        ]
    if domain == "travel":
        return [
            _workflow(
                "trip_inquiry",
                "Tangani inquiry perjalanan",
                "Customer bertanya paket, itinerary, harga, jadwal, atau ketersediaan trip.",
                "Mengumpulkan preferensi perjalanan dan menyiapkan ringkasan untuk follow-up.",
                ["tujuan", "tanggal", "jumlah peserta", "budget", "preferensi akomodasi/transport", "kontak customer"],
                [
                    "Tanyakan tujuan dan tanggal perjalanan.",
                    "Kumpulkan jumlah peserta, budget, dan preferensi penting.",
                    "Berikan informasi umum yang sudah pasti.",
                    "Eskalasi untuk harga final, ketersediaan, perubahan jadwal, dan pembayaran.",
                ],
                ["Jika tanggal/budget belum ada, tanyakan dulu.", "Jika customer minta booking final, eskalasi."],
                ["Eskalasi untuk pembayaran, booking final, perubahan jadwal, dan klaim asuransi/refund."],
                ["Jangan menjamin ketersediaan atau harga final tanpa data/tool.", "Jangan membuat janji refund tanpa kebijakan Owner."],
                "Ringkasan kebutuhan trip dan next step booking.",
            )
        ]
    if domain == "ecommerce":
        return [
            _workflow(
                "product_inquiry_checkout",
                "Bantu inquiry produk sampai checkout awal",
                "Customer bertanya produk, stok, harga, checkout, atau refund.",
                "Membantu customer memilih produk dan mengumpulkan data checkout.",
                ["produk yang dicari", "varian/jumlah", "alamat pengiriman", "kontak", "metode pembayaran"],
                [
                    "Pahami produk yang dicari customer.",
                    "Tanyakan varian, jumlah, dan alamat jika customer ingin checkout.",
                    "Gunakan data/tool katalog jika tersedia.",
                    "Ringkas pesanan dan eskalasi untuk stok/harga/refund yang belum pasti.",
                ],
                ["Jika stok/harga tidak ada di data, eskalasi.", "Jika customer meminta refund, ikuti kebijakan atau eskalasi."],
                ["Eskalasi untuk refund, komplain berat, stok tidak pasti, atau pembayaran manual."],
                ["Jangan mengarang stok, harga, ongkir, atau kebijakan refund.", "Jangan mengklaim order dibuat jika tool checkout belum berhasil."],
                "Ringkasan order atau pertanyaan produk yang siap diproses.",
            )
        ]
    if domain == "local_service":
        return [
            _workflow(
                "service_booking",
                "Booking dan kualifikasi jasa",
                "Customer ingin pesan jasa, minta survey, atau minta penawaran.",
                "Mengumpulkan detail pekerjaan agar tim bisa memberi jadwal/quotation.",
                ["jenis layanan", "lokasi", "waktu yang diinginkan", "detail masalah/kebutuhan", "foto/dokumen jika ada", "kontak"],
                [
                    "Tanyakan jenis layanan dan lokasi.",
                    "Kumpulkan detail kebutuhan serta waktu yang diinginkan.",
                    "Minta foto/dokumen jika berguna dan channel mendukung.",
                    "Ringkas kebutuhan untuk Owner/operator.",
                    "Eskalasi untuk harga final, jadwal final, atau pekerjaan berisiko.",
                ],
                ["Jika detail kurang, tanya data tambahan.", "Jika customer minta harga final, eskalasi."],
                ["Eskalasi untuk quotation final, jadwal teknisi, komplain, atau risiko keselamatan."],
                ["Jangan memberi harga final tanpa kebijakan.", "Jangan menjanjikan jadwal teknisi tanpa konfirmasi."],
                "Ringkasan kebutuhan jasa dan data untuk quotation/booking.",
            )
        ]
    if domain == "event_service":
        return [
            _workflow(
                "event_need_intake",
                "Kumpulkan kebutuhan acara",
                "Customer bertanya persiapan acara, perlengkapan, tanggal, jumlah tamu, atau antar-pasang.",
                "Mengumpulkan detail acara sampai siap dicek Owner untuk biaya dan ketersediaan.",
                ["nama customer", "tanggal acara", "lokasi acara", "jumlah tamu", "barang/perlengkapan yang dibutuhkan", "kebutuhan antar-pasang"],
                [
                    "Sapa customer dan tanyakan nama/kontak jika belum ada.",
                    "Kumpulkan tanggal acara, lokasi, jumlah tamu, dan jenis acara.",
                    "Tanyakan barang/perlengkapan yang dibutuhkan dan apakah perlu antar-pasang.",
                    "Ringkas kebutuhan acara sebelum meminta cek Owner.",
                    "Jika customer minta harga final atau ketersediaan barang, eskalasi ke Owner/operator dengan ringkasan lengkap.",
                ],
                [
                    "Jika data acara belum lengkap, tanya data yang kurang sebelum eskalasi.",
                    "Jika customer minta kepastian biaya, stok, jadwal tim, atau booking final, eskalasi.",
                ],
                [
                    "Eskalasi harga final, ketersediaan barang, jadwal antar-pasang, perubahan pesanan, dan booking final ke Owner/operator.",
                ],
                [
                    "Jangan mengarang harga final, stok barang, jadwal tim, atau booking final.",
                    "Jangan menjanjikan barang tersedia sebelum Owner/operator mengecek.",
                ],
                "Ringkasan kebutuhan acara yang siap dicek Owner/operator.",
            ),
            _workflow(
                "owner_review_follow_up",
                "Follow-up hasil cek Owner",
                "Owner/operator sudah memberi keputusan biaya, ketersediaan, atau next step.",
                "Menyampaikan hasil cek Owner ke customer dengan jelas dan melanjutkan proses sesuai arahan.",
                ["keputusan Owner/operator", "ringkasan kebutuhan customer", "next step pembayaran/booking jika ada"],
                [
                    "Baca keputusan Owner/operator sebelum membalas customer.",
                    "Sampaikan hanya informasi yang sudah dipastikan Owner/operator.",
                    "Jika ada aturan uang muka, pelunasan, atau jadwal, jelaskan sesuai arahan Owner.",
                    "Simpan update penting ke memory agar follow-up berikutnya konsisten.",
                ],
                [
                    "Jika arahan Owner belum lengkap, minta klarifikasi ke Owner/operator.",
                    "Jika customer mengubah kebutuhan, kembali ke intake dan eskalasi ulang jika perlu.",
                ],
                [
                    "Eskalasi ulang jika customer meminta perubahan harga, barang, tanggal, lokasi, atau jumlah tamu.",
                ],
                [
                    "Jangan menambah syarat pembayaran, diskon, atau garansi yang tidak disebut Owner/operator.",
                    "Jangan mengklaim pembayaran/booking selesai tanpa bukti atau arahan Owner/operator.",
                ],
                "Customer menerima informasi hasil cek dan next step yang aman.",
            ),
        ]
    if domain == "clinic_wellness":
        return [
            _workflow(
                "appointment_intake",
                "Booking appointment klinik/wellness",
                "Customer ingin konsultasi, treatment, atau jadwal appointment.",
                "Mengumpulkan data booking tanpa memberi diagnosis atau keputusan medis.",
                ["nama", "keluhan/tujuan umum", "jadwal pilihan", "kontak", "cabang/provider jika relevan"],
                [
                    "Tanyakan kebutuhan umum dan jadwal pilihan.",
                    "Kumpulkan kontak dan preferensi cabang/provider.",
                    "Berikan informasi administratif yang sudah pasti.",
                    "Eskalasi pertanyaan medis, diagnosis, resep, kondisi darurat, atau keputusan klinis.",
                ],
                ["Jika customer menyebut kondisi darurat, arahkan ke layanan darurat/manusia.", "Jika pertanyaan medis spesifik, eskalasi."],
                ["Eskalasi semua keputusan medis, diagnosis, resep, dan kondisi darurat."],
                ["Jangan memberi diagnosis, resep, atau klaim kesembuhan.", "Jangan menggantikan tenaga medis."],
                "Ringkasan appointment request yang aman untuk staff.",
            )
        ]
    if domain == "education":
        return [
            _workflow(
                "class_inquiry",
                "Tangani inquiry kelas/kursus",
                "Customer bertanya program, jadwal, biaya, atau pendaftaran.",
                "Mengumpulkan kebutuhan belajar dan menyiapkan next step pendaftaran.",
                ["program yang diminati", "level/usia", "jadwal pilihan", "target belajar", "kontak"],
                [
                    "Tanyakan program dan target belajar.",
                    "Kumpulkan level/usia dan jadwal pilihan.",
                    "Berikan informasi umum yang sudah pasti.",
                    "Eskalasi untuk biaya final, placement, promo, atau perubahan jadwal.",
                ],
                ["Jika level belum jelas, tanyakan atau arahkan placement.", "Jika customer siap daftar, kumpulkan data dan eskalasi."],
                ["Eskalasi untuk biaya final, promo, placement, komplain, dan perubahan jadwal."],
                ["Jangan menjamin hasil belajar.", "Jangan mengarang biaya/promo/jadwal."],
                "Ringkasan kebutuhan kelas dan next step pendaftaran.",
            )
        ]
    if domain == "property":
        return [
            _workflow(
                "property_lead_qualification",
                "Kualifikasi lead properti",
                "Customer bertanya unit, harga, sewa/beli, atau jadwal viewing.",
                "Mengumpulkan preferensi properti dan menyiapkan jadwal follow-up.",
                ["tipe transaksi", "lokasi", "budget", "tipe unit", "jadwal viewing", "kontak"],
                [
                    "Tanyakan kebutuhan beli/sewa dan lokasi.",
                    "Kumpulkan budget, tipe unit, dan jadwal viewing.",
                    "Berikan info umum yang sudah pasti.",
                    "Eskalasi untuk harga final, ketersediaan unit, legalitas, dan booking.",
                ],
                ["Jika budget/lokasi belum ada, tanyakan.", "Jika customer ingin booking/viewing final, eskalasi."],
                ["Eskalasi untuk booking, negosiasi, legalitas, pembayaran, dan ketersediaan unit."],
                ["Jangan menjamin harga/unit tersedia tanpa data.", "Jangan memberi nasihat legal/finansial."],
                "Ringkasan lead properti dan jadwal follow-up.",
            )
        ]
    return _generic_workflows(domain)


def build_agent_operating_manual(
    *,
    name: str,
    description: str = "",
    instructions: str = "",
    tools_config: dict[str, Any] | None = None,
    business_context: str = "",
    domain: str = "",
) -> dict[str, Any]:
    text = _combined_text(name, description, instructions, business_context)
    resolved_domain, confidence = detect_sop_domain(
        name,
        description,
        instructions,
        business_context,
        requested_domain=domain,
    )
    has_template = resolved_domain in _DOMAIN_KEYWORDS
    context_is_sparse = len(text.strip()) < _MIN_CONTEXT_CHARS
    source = "arthur_template" if has_template else "arthur_generic"

    if context_is_sparse:
        maturity = "draft"
        owner_review_required = True
    elif has_template:
        maturity = "usable"
        owner_review_required = False
    else:
        maturity = "needs_review"
        owner_review_required = True

    missing_context: list[str] = []
    if context_is_sparse:
        missing_context.extend(
            [
                "detail bisnis belum lengkap",
                "aturan harga/stok/jadwal/pembayaran belum terkonfirmasi",
                "batas eskalasi Owner/operator belum lengkap",
            ]
        )

    if tools_config and not tools_config.get("escalation"):
        missing_context.append("human handoff/escalation belum aktif di tools_config")

    assumptions = [
        "Agent boleh melakukan intake, klarifikasi, ringkasan, dan eskalasi.",
        "Keputusan final bisnis hanya boleh dilakukan jika SOP/data/tool sudah jelas.",
    ]
    if not has_template:
        assumptions.append("Domain belum punya template khusus; SOP dibuat dari generic SOP builder.")

    return {
        "manual_id": "agent_operating_manual",
        "version": 1,
        "source": source,
        "domain": resolved_domain,
        "domain_confidence": confidence,
        "maturity": maturity,
        "owner_review_required": owner_review_required,
        "missing_context": missing_context,
        "assumptions": assumptions,
        "workflows": _template_workflows(resolved_domain),
    }


def _blueprint_payload(value: Any) -> dict[str, Any] | None:
    parsed = _parse_json_like(value)
    if not isinstance(parsed, dict):
        return None
    nested = parsed.get("blueprint")
    if isinstance(nested, dict):
        return nested
    return parsed


def _workflow_from_blueprint_state(
    *,
    state: dict[str, Any],
    workflow_steps: list[dict[str, Any]],
    escalation_rules: list[dict[str, Any]],
    human_approval_points: list[dict[str, Any]],
    validation_checklist: list[str],
) -> dict[str, Any]:
    state_name = str(state.get("state") or state.get("name") or "workflow").strip()
    state_text = state_name.lower()
    related_steps: list[dict[str, Any]] = []
    for step in workflow_steps:
        haystack = _combined_text(step.get("name"), step.get("agent_action"), step.get("success_criteria")).lower()
        if state_text and state_text in haystack:
            related_steps.append(step)
    if not related_steps:
        related_steps = workflow_steps[:3]

    required_inputs: list[str] = []
    concrete_steps: list[str] = []
    for step in related_steps:
        required_inputs.extend(_as_text_list(step.get("required_user_data")))
        action = str(step.get("agent_action") or step.get("name") or "").strip()
        if action and action not in concrete_steps:
            concrete_steps.append(action)
    concrete_steps.extend(_as_text_list(state.get("allowed_actions")))

    approval_points = [
        f"{point.get('when')}: {point.get('operator_action')} -> {point.get('agent_next_action')}"
        for point in human_approval_points
        if isinstance(point, dict) and (point.get("when") or point.get("operator_action") or point.get("agent_next_action"))
    ]
    escalation_text = [
        f"{rule.get('condition')}: {rule.get('action')}"
        for rule in escalation_rules
        if isinstance(rule, dict) and (rule.get("condition") or rule.get("action"))
    ]

    prohibited = [
        "Jangan mengarang harga, stok, jadwal, refund, approval, atau keputusan final yang tidak ada di SOP/data/tool.",
        "Jangan mengklaim aksi eksternal, file, pembayaran, atau pengiriman berhasil sebelum tool/proses yang relevan benar-benar sukses.",
    ]
    for item in validation_checklist:
        lowered = item.lower()
        if any(keyword in lowered for keyword in ("tidak", "jangan", "belum", "sebelum")):
            prohibited.append(item)

    return _workflow(
        _slug(state_name),
        state_name.replace("_", " ").title(),
        str(state.get("entry_condition") or "Saat state ini dimulai dari konteks percakapan.").strip(),
        str(state.get("exit_condition") or "Menyelesaikan state ini dengan aman dan sesuai SOP.").strip(),
        list(dict.fromkeys(required_inputs)),
        list(dict.fromkeys(concrete_steps)) or ["Ikuti allowed_actions state dan konteks workflow blueprint."],
        [str(state.get("exit_condition") or "").strip()] + approval_points,
        escalation_text,
        list(dict.fromkeys(prohibited)),
        str(state.get("exit_condition") or "State selesai atau blocker disampaikan jujur.").strip(),
    )


def build_agent_operating_manual_from_blueprint(
    blueprint: Any,
    *,
    name: str,
    description: str = "",
    business_context: str = "",
    domain: str = "",
    tools_config: dict[str, Any] | None = None,
    source: str = "arthur_blueprint",
) -> dict[str, Any] | None:
    payload = _blueprint_payload(blueprint)
    if not payload:
        return None

    workflow_steps = [
        step for step in payload.get("workflow_steps", [])
        if isinstance(step, dict)
    ]
    state_plan = [
        state for state in payload.get("state_plan", [])
        if isinstance(state, dict)
    ]
    if not workflow_steps and not state_plan:
        return None

    context = _combined_text(
        name,
        description,
        business_context,
        payload.get("agent_summary"),
        json.dumps(payload, ensure_ascii=False),
    )
    resolved_domain, confidence = detect_sop_domain(context, requested_domain=domain)
    validation_checklist = _as_text_list(payload.get("validation_checklist"))
    escalation_rules = [
        rule for rule in payload.get("escalation_rules", [])
        if isinstance(rule, dict)
    ]
    human_approval_points = [
        point for point in payload.get("human_approval_points", [])
        if isinstance(point, dict)
    ]

    if state_plan:
        workflows = [
            _workflow_from_blueprint_state(
                state=state,
                workflow_steps=workflow_steps,
                escalation_rules=escalation_rules,
                human_approval_points=human_approval_points,
                validation_checklist=validation_checklist,
            )
            for state in state_plan
        ]
    else:
        workflows = [
            _workflow(
                "primary_workflow",
                "Workflow utama agent",
                "User memulai percakapan atau meminta bantuan sesuai tujuan agent.",
                str(payload.get("agent_summary") or description or "Menjalankan kebutuhan user sesuai blueprint.").strip(),
                list(dict.fromkeys([
                    item
                    for step in workflow_steps
                    for item in _as_text_list(step.get("required_user_data"))
                ])),
                [
                    str(step.get("agent_action") or step.get("name") or "").strip()
                    for step in workflow_steps
                    if str(step.get("agent_action") or step.get("name") or "").strip()
                ],
                [
                    str(step.get("success_criteria") or "").strip()
                    for step in workflow_steps
                    if str(step.get("success_criteria") or "").strip()
                ],
                [
                    f"{rule.get('condition')}: {rule.get('action')}"
                    for rule in escalation_rules
                    if rule.get("condition") or rule.get("action")
                ],
                [
                    "Jangan mengarang keputusan, data, atau hasil yang tidak ada di SOP/data/tool.",
                    "Jangan mengklaim task selesai jika output nyata belum tersedia.",
                ],
                "Kebutuhan user selesai, blocker disampaikan jujur, atau kasus dieskalasi.",
            )
        ]

    context_is_sparse = len(context.strip()) < _MIN_CONTEXT_CHARS
    maturity = "draft" if context_is_sparse else "usable"
    owner_review_required = context_is_sparse
    missing_context = []
    if context_is_sparse:
        missing_context.extend([
            "detail bisnis belum lengkap",
            "SOP dibuat dari blueprint terbatas dan perlu review Owner",
        ])
    if tools_config and not tools_config.get("escalation") and (escalation_rules or human_approval_points):
        missing_context.append("blueprint membutuhkan handoff manusia, tapi escalation belum aktif di tools_config")
        maturity = "needs_review"
        owner_review_required = True

    assumptions = _as_text_list(payload.get("assumptions"), fallback=[
        "SOP dibuat dari blueprint Arthur dan harus diikuti sebagai kontrak kerja agent.",
        "Jika data bisnis belum pasti, agent harus bertanya, memakai tool yang tersedia, atau eskalasi.",
    ])

    return {
        "manual_id": "agent_operating_manual",
        "version": 1,
        "source": source,
        "domain": resolved_domain,
        "domain_confidence": confidence,
        "maturity": maturity,
        "owner_review_required": owner_review_required,
        "missing_context": missing_context,
        "assumptions": assumptions,
        "workflows": workflows,
        "knowledge_plan": payload.get("knowledge_plan") if isinstance(payload.get("knowledge_plan"), dict) else {},
        "memory_plan": payload.get("memory_plan") if isinstance(payload.get("memory_plan"), list) else [],
        "state_plan": state_plan,
        "human_approval_points": human_approval_points,
        "escalation_rules": escalation_rules,
        "validation_checklist": validation_checklist,
    }


def normalize_agent_operating_manual(
    value: Any,
    *,
    name: str = "",
    description: str = "",
    instructions: str = "",
    tools_config: dict[str, Any] | None = None,
    business_context: str = "",
    domain: str = "",
) -> dict[str, Any]:
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except Exception:
            value = {"notes": value.strip()}
    if not isinstance(value, dict) or not value.get("workflows"):
        value = build_agent_operating_manual(
            name=name,
            description=description,
            instructions=instructions,
            tools_config=tools_config,
            business_context=business_context,
            domain=domain,
        )

    manual = copy.deepcopy(value)
    manual.setdefault("manual_id", "agent_operating_manual")
    manual.setdefault("version", 1)
    manual.setdefault("source", "owner_provided")
    manual.setdefault("domain", domain or "generic")
    manual.setdefault("domain_confidence", "medium")
    manual.setdefault("maturity", "needs_review")
    manual.setdefault("owner_review_required", manual.get("maturity") in {"draft", "needs_review"})
    manual.setdefault("missing_context", [])
    manual.setdefault("assumptions", [])
    manual.setdefault("workflows", _generic_workflows(str(manual.get("domain") or "generic")))
    return manual


def format_operating_manual_for_prompt(
    manual: dict[str, Any] | None,
    *,
    max_workflows: int = 5,
    max_items: int = 6,
) -> str:
    if not isinstance(manual, dict):
        return ""
    workflows = manual.get("workflows") if isinstance(manual.get("workflows"), list) else []
    if not workflows:
        return ""

    lines = ["\n## SOP Workflow Detail"]
    for workflow in workflows[:max_workflows]:
        if not isinstance(workflow, dict):
            continue
        lines.append(f"- Workflow: {workflow.get('name') or workflow.get('workflow_id') or 'unknown'}")
        if workflow.get("trigger"):
            lines.append(f"  Trigger: {workflow['trigger']}")
        if workflow.get("goal"):
            lines.append(f"  Goal: {workflow['goal']}")
        required = _as_text_list(workflow.get("required_inputs"))[:max_items]
        if required:
            lines.append(f"  Data wajib: {', '.join(required)}")
        steps = _as_text_list(workflow.get("steps"))[:max_items]
        if steps:
            lines.append("  Langkah:")
            lines.extend(f"  - {step}" for step in steps)
        decisions = _as_text_list(workflow.get("decision_points"))[:max_items]
        if decisions:
            lines.append("  Keputusan:")
            lines.extend(f"  - {item}" for item in decisions)
        escalations = _as_text_list(workflow.get("escalation_rules"))[:max_items]
        if escalations:
            lines.append("  Eskalasi:")
            lines.extend(f"  - {item}" for item in escalations)
        prohibited = _as_text_list(workflow.get("prohibited_actions"))[:max_items]
        if prohibited:
            lines.append("  Larangan:")
            lines.extend(f"  - {item}" for item in prohibited)
        if workflow.get("final_output"):
            lines.append(f"  Output akhir: {workflow['final_output']}")
    return "\n".join(lines)


def get_agent_operating_manual(tools_config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(tools_config, dict):
        return None
    manual = tools_config.get(OPERATING_MANUAL_KEY)
    return manual if isinstance(manual, dict) else None


def ensure_operating_manual_in_tools_config(
    tools_config: dict[str, Any] | None,
    *,
    name: str,
    description: str = "",
    instructions: str = "",
    business_context: str = "",
    domain: str = "",
    operating_manual: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    merged = dict(tools_config or {})
    manual = normalize_agent_operating_manual(
        operating_manual if operating_manual not in (None, "", {}) else merged.get(OPERATING_MANUAL_KEY),
        name=name,
        description=description,
        instructions=instructions,
        tools_config=merged,
        business_context=business_context,
        domain=domain,
    )
    merged[OPERATING_MANUAL_KEY] = manual
    return merged, manual


def summarize_operating_manual(manual: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manual, dict):
        return {
            "present": False,
            "maturity": "missing",
            "owner_review_required": True,
            "workflow_count": 0,
            "domain": None,
            "source": None,
            "domain_confidence": None,
            "missing_context": [],
            "workflow_ids": [],
        }

    workflows = manual.get("workflows") if isinstance(manual.get("workflows"), list) else []
    return {
        "present": True,
        "maturity": str(manual.get("maturity") or "needs_review"),
        "owner_review_required": bool(manual.get("owner_review_required")),
        "workflow_count": len(workflows),
        "domain": manual.get("domain"),
        "source": manual.get("source"),
        "domain_confidence": manual.get("domain_confidence"),
        "missing_context": manual.get("missing_context") if isinstance(manual.get("missing_context"), list) else [],
        "workflow_ids": [
            str(workflow.get("workflow_id"))
            for workflow in workflows
            if isinstance(workflow, dict) and workflow.get("workflow_id")
        ],
    }


def operating_manual_readiness_issues(manual: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    summary = summarize_operating_manual(manual)
    blockers: list[str] = []
    warnings: list[str] = []
    if not summary["present"]:
        blockers.append("operating_manual_missing: agent belum punya SOP/Agent Operating Manual terpisah dari instructions.")
        return blockers, warnings
    if summary["workflow_count"] <= 0:
        blockers.append("operating_manual_empty: SOP agent belum punya workflow operasional.")
    maturity = summary["maturity"]
    if maturity in {"draft", "needs_review"}:
        blockers.append(
            f"operating_manual_{maturity}: SOP agent masih {maturity}; agent hanya boleh intake, klarifikasi, ringkasan, dan eskalasi sampai Owner review."
        )
    elif maturity not in {"usable", "verified"}:
        warnings.append(f"operating_manual_{maturity}: status SOP belum dikenal.")
    if summary["owner_review_required"]:
        warnings.append("operating_manual_owner_review_required: Owner perlu review SOP agent.")
    return blockers, warnings


def operating_manual_row_to_artifact(row: AgentOperatingManual) -> dict[str, Any]:
    artifact = getattr(row, "artifact", None)
    if isinstance(artifact, dict) and artifact:
        return dict(artifact)
    # Fallback: narrow projection for pre-migration rows (artifact empty/missing)
    return {
        "manual_id": str(row.id) if row.id else None,
        "version": row.version,
        "source": row.source,
        "domain": row.domain,
        "domain_confidence": row.domain_confidence,
        "maturity": row.maturity,
        "owner_review_required": row.owner_review_required,
        "missing_context": row.missing_context or [],
        "assumptions": row.assumptions or [],
        "workflows": row.workflows or [],
        "created_by_agent_id": row.created_by_agent_id,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


async def get_latest_agent_operating_manual(
    agent_id: uuid.UUID,
    db: AsyncSession,
    *,
    fallback_tools_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        result = await db.execute(
            select(AgentOperatingManual)
            .where(AgentOperatingManual.agent_id == agent_id)
            .order_by(AgentOperatingManual.version.desc(), AgentOperatingManual.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if isinstance(row, AgentOperatingManual):
            return operating_manual_row_to_artifact(row)
    except Exception:
        pass
    return get_agent_operating_manual(fallback_tools_config)


async def upsert_agent_operating_manual(
    agent_id: uuid.UUID,
    manual: dict[str, Any],
    db: AsyncSession,
    *,
    created_by_agent_id: str | None = None,
    version: int | None = None,
) -> AgentOperatingManual:
    normalized = normalize_agent_operating_manual(manual)
    target_version = int(version or normalized.get("version") or 1)
    result = await db.execute(
        select(AgentOperatingManual).where(
            AgentOperatingManual.agent_id == agent_id,
            AgentOperatingManual.version == target_version,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = AgentOperatingManual(
            agent_id=agent_id,
            version=target_version,
        )
        db.add(row)

    row.source = str(normalized.get("source") or "arthur_generic")
    row.domain = str(normalized.get("domain") or "generic")
    row.domain_confidence = str(normalized.get("domain_confidence") or "low")
    row.maturity = str(normalized.get("maturity") or "draft")
    row.owner_review_required = bool(normalized.get("owner_review_required"))
    row.missing_context = normalized.get("missing_context") if isinstance(normalized.get("missing_context"), list) else []
    row.assumptions = normalized.get("assumptions") if isinstance(normalized.get("assumptions"), list) else []
    row.workflows = normalized.get("workflows") if isinstance(normalized.get("workflows"), list) else []
    row.created_by_agent_id = created_by_agent_id or normalized.get("created_by_agent_id")
    row.artifact = normalized
    await db.flush()
    return row

"""Deterministic discovery gate for Arthur's agent-creation flow."""
from __future__ import annotations

import json
import re
from typing import Any


_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "context_goal",
        "label": "Grup 1: Konteks & Tujuan",
        "fields": ("problem", "usage_context", "agent_name", "audience"),
    },
    {
        "id": "agent_behavior",
        "label": "Grup 2: Perilaku Agent",
        "fields": (
            "main_tasks",
            "capabilities",
            "prohibited_actions",
            "allowed_actions",
            "tone_style",
            "ideal_conversations",
            "avoided_conversations",
        ),
    },
    {
        "id": "escalation_knowledge_boundary",
        "label": "Grup 3: Eskalasi & Batasan Pengetahuan",
        "fields": ("unknown_handling", "escalation_target"),
    },
    {
        "id": "data_knowledge",
        "label": "Grup 4: Data & Pengetahuan",
        "fields": ("knowledge_sources", "sensitive_data_policy"),
    },
    {
        "id": "scale_integration",
        "label": "Grup 5: Skala & Integrasi",
        "fields": (
            "whatsapp_scale",
            "daily_chat_volume",
            "integrations",
            "expected_outputs",
            "vision_requirement",
        ),
    },
    {
        "id": "go_live",
        "label": "Grup 6: Sebelum Go-Live",
        "fields": ("go_live_approver",),
    },
)

_QUESTIONS: dict[str, str] = {
    "problem": "Problem/pain point apa yang mau diselesaikan? Ceritakan masalahnya, bukan sekadar fitur agent yang diinginkan.",
    "usage_context": "Agent ini untuk kebutuhan personal atau pekerjaan/bisnis?",
    "agent_name": "Nama agent yang kamu inginkan apa? Saya tidak akan memilih nama tanpa persetujuanmu.",
    "audience": "Siapa yang akan chat dengan agent ini: kamu sendiri, internal tim, atau customer eksternal?",
    "main_tasks": "Apa saja tugas utama agent? Tulis sebagai daftar pekerjaan konkret dari awal sampai selesai.",
    "capabilities": "Kemampuan apa yang dibutuhkan, misalnya menjawab pertanyaan, input data, mengolah file, atau mengirim notifikasi?",
    "prohibited_actions": "Apa yang sama sekali TIDAK BOLEH agent lakukan? Contoh: memberi diskon, menyetujui refund, atau mengarang informasi.",
    "allowed_actions": "Apa yang BOLEH agent lakukan dan sampai batas wewenang mana? Contoh: mencatat pesanan boleh, mengonfirmasi pembayaran tidak boleh.",
    "tone_style": "Tone dan gaya bahasanya bagaimana? Contoh: santai, bahasa Indonesia, boleh emoji secukupnya, tetapi tetap sopan.",
    "ideal_conversations": (
        "Berikan 2-3 contoh percakapan ideal. Contoh format: `Customer: Apakah stok masih ada?` lalu "
        "`Agent: Saya cek dari sumber yang tersedia; kalau belum pasti saya eskalasikan ke admin.`"
    ),
    "avoided_conversations": (
        "Berikan contoh percakapan yang harus dihindari/red line. Contoh: customer meminta diskon, lalu agent "
        "langsung menjanjikan diskon tanpa izin Owner."
    ),
    "unknown_handling": "Kalau agent tidak tahu, informasinya di luar instruksi, atau sumbernya tidak cukup, agent harus berhenti dan melakukan apa?",
    "escalation_target": (
        "Karena ini untuk pekerjaan/bisnis, jelaskan eskalasinya secara detail: kondisi pemicu, nama/role penerima, "
        "dan nomor WhatsApp yang menerima ringkasan percakapan serta lampiran terakhir."
    ),
    "knowledge_sources": "Perlu pengetahuan/RAG tambahan? Jawab ya atau tidak; jika ya, sebutkan sumbernya seperti file, link, Google Sheet, atau database.",
    "sensitive_data_policy": "Apakah ada data sensitif seperti nama, kontak, atau transaksi? Jelaskan aturan kerahasiaan dan retensinya, atau nyatakan tidak ada.",
    "whatsapp_scale": (
        "Agent dipakai pada satu atau banyak nomor WhatsApp? Jelaskan juga polanya: satu nomor melayani banyak user "
        "sekaligus seperti CS, atau setiap user memiliki nomor sendiri."
    ),
    "daily_chat_volume": "Berapa estimasi volume chat per hari? Boleh berupa rentang, misalnya 50-100 percakapan/hari.",
    "integrations": "Perlu integrasi ke sistem lain seperti Google Workspace, CRM, payment gateway, atau database? Nyatakan juga jika tidak perlu.",
    "expected_outputs": "Output konkretnya apa? Contoh: tambah satu baris spreadsheet, buat PDF, atau kirim notifikasi ke admin. Berikan minimal satu contoh.",
    "vision_requirement": "Apakah agent perlu menerima atau memahami gambar/foto/vision? Jawab ya atau tidak dan beri contoh jika ya.",
    "go_live_approver": "Siapa nama atau role yang akan review dan approve agent sebelum dipakai sungguhan?",
    "user_confirmed": "Saya akan merangkum seluruh jawaban discovery. Setelah rangkumannya benar, minta user menyatakan setuju sebelum agent dibuat.",
}

_USAGE_CONTEXT_ALIASES = {
    "personal": "personal",
    "pribadi": "personal",
    "sendiri": "personal",
    "pekerjaan": "work",
    "kerja": "work",
    "bisnis": "work",
    "business": "work",
    "work": "work",
    "professional": "work",
}

_IGNORED_OPERATIONAL_HOUR_FIELDS = {
    "active_hours",
    "agent_active_hours",
    "business_hours",
    "jam_aktif",
    "jam_operasional",
    "operational_hours",
    "operating_hours",
}


def _parse_answers(value: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(value, dict):
        return dict(value), None
    if value in (None, ""):
        return {}, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return {}, f"discovery_answers bukan JSON valid: {exc.msg}"
        if isinstance(parsed, dict):
            return parsed, None
    return {}, "discovery_answers harus berupa JSON object."


def _is_answered(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _normalize_usage_context(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    if text in _USAGE_CONTEXT_ALIASES:
        return _USAGE_CONTEXT_ALIASES[text]
    for alias, normalized in _USAGE_CONTEXT_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return normalized
    return ""


def _confirmed(value: Any) -> bool:
    if value is True:
        return True
    return str(value or "").strip().lower() in {
        "true",
        "yes",
        "ya",
        "iya",
        "setuju",
        "confirmed",
        "dikonfirmasi",
    }


def _has_two_to_three_conversation_examples(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        count = len([item for item in value if _is_answered(item)])
        return 2 <= count <= 3
    text = str(value or "").strip()
    if not text:
        return False
    numbered = re.findall(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+", text)
    customer_turns = re.findall(r"\b(?:customer|user|pelanggan)\s*:", text, flags=re.IGNORECASE)
    count = max(len(numbered), len(customer_turns), 2 if "||" in text else 0)
    return 2 <= count <= 3


def _problem_is_pain_point(value: Any) -> bool:
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return False
    feature_only = re.match(
        r"^(?:saya\s+)?(?:mau|ingin|butuh|perlu|buat|bikin|buatkan|bikinkan)?\s*"
        r"(?:agent|agen|bot)\b",
        text,
    )
    pain_markers = (
        "lambat",
        "lama",
        "sulit",
        "kesulitan",
        "sering",
        "lupa",
        "manual",
        "tidak",
        "belum",
        "gagal",
        "masalah",
        "kewalahan",
        "terlambat",
        "menunggu",
        "kehilangan",
        "bingung",
    )
    return not feature_only or any(marker in text for marker in pain_markers)


def _whatsapp_scale_is_detailed(value: Any) -> bool:
    text = " ".join(str(value or "").strip().lower().split())
    number_scope = bool(re.search(r"\b(1|satu|banyak|beberapa|multi)\b", text))
    user_pattern = bool(
        re.search(
            r"\b(banyak user|banyak pengguna|banyak customer|customer sekaligus|pelanggan sekaligus|"
            r"tiap user|setiap user|per user|tiap pengguna|setiap pengguna|sendiri|satu user|satu pengguna)\b",
            text,
        )
    )
    return number_scope and user_pattern


def _phone_from_escalation(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("whatsapp_number", "phone", "number", "operator_phone"):
            phone = str(value.get(key) or "").strip()
            if phone:
                return phone
    text = str(value or "")
    match = re.search(r"(?:\+?62|0)[\d\s-]{7,16}", text)
    return match.group(0).strip() if match else ""


def _work_escalation_is_detailed(value: Any, operator_phone: str = "") -> bool:
    if isinstance(value, dict):
        recipient = next(
            (
                str(value.get(key) or "").strip()
                for key in ("recipient", "recipient_name_or_role", "name", "role")
                if str(value.get(key) or "").strip()
            ),
            "",
        )
        conditions = next(
            (
                str(value.get(key) or "").strip()
                for key in ("conditions", "trigger_conditions", "triggers", "when")
                if str(value.get(key) or "").strip()
            ),
            "",
        )
        phone = _phone_from_escalation(value) or str(operator_phone or "").strip()
        return bool(recipient and conditions and len(re.sub(r"\D", "", phone)) >= 8)
    text = str(value or "").strip()
    phone = _phone_from_escalation(text) or str(operator_phone or "").strip()
    has_recipient = bool(re.search(r"\b(owner|admin|operator|manager|atasan|tim)\b", text, re.IGNORECASE))
    return bool(has_recipient and len(re.sub(r"\D", "", phone)) >= 8)


def _group_for_field(field: str) -> dict[str, Any]:
    for group in _GROUPS:
        if field in group["fields"]:
            return group
    return {"id": "confirmation", "label": "Konfirmasi", "fields": ("user_confirmed",)}


def validate_agent_discovery(
    discovery_answers: Any,
    *,
    agent_name: str = "",
    operator_phone: str = "",
    require_confirmation: bool = True,
) -> dict[str, Any]:
    """Validate Arthur's six discovery groups without asking for active hours."""
    answers, parse_error = _parse_answers(discovery_answers)
    ignored_fields = sorted(key for key in answers if key in _IGNORED_OPERATIONAL_HOUR_FIELDS)
    for key in ignored_fields:
        answers.pop(key, None)

    supplied_name = str(answers.get("agent_name") or "").strip()
    external_name = str(agent_name or "").strip()
    if not supplied_name and external_name:
        answers["agent_name"] = external_name

    usage_context = _normalize_usage_context(answers.get("usage_context"))
    if usage_context:
        answers["usage_context"] = usage_context

    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    validation_errors: list[str] = [parse_error] if parse_error else []

    required_fields: list[str] = []
    for group in _GROUPS:
        for field in group["fields"]:
            if usage_context == "personal" and field in {"escalation_target", "go_live_approver"}:
                continue
            required_fields.append(field)

    for field in required_fields:
        if not _is_answered(answers.get(field)):
            missing_fields.append(field)

    if _is_answered(answers.get("usage_context")) and not usage_context:
        invalid_fields.append("usage_context")
        validation_errors.append("usage_context harus personal atau pekerjaan/bisnis.")
    if _is_answered(answers.get("problem")) and not _problem_is_pain_point(answers.get("problem")):
        invalid_fields.append("problem")
        validation_errors.append("problem harus menjelaskan pain point/masalah, bukan hanya fitur atau jenis agent.")
    if _is_answered(answers.get("ideal_conversations")) and not _has_two_to_three_conversation_examples(
        answers.get("ideal_conversations")
    ):
        invalid_fields.append("ideal_conversations")
        validation_errors.append("ideal_conversations harus berisi 2-3 contoh percakapan.")
    if _is_answered(answers.get("whatsapp_scale")) and not _whatsapp_scale_is_detailed(
        answers.get("whatsapp_scale")
    ):
        invalid_fields.append("whatsapp_scale")
        validation_errors.append(
            "whatsapp_scale harus menjelaskan jumlah nomor dan pola satu nomor-banyak user atau tiap user-punya nomor."
        )
    if _is_answered(answers.get("daily_chat_volume")) and not re.search(
        r"\d", str(answers.get("daily_chat_volume"))
    ):
        invalid_fields.append("daily_chat_volume")
        validation_errors.append("daily_chat_volume harus memuat estimasi angka atau rentang chat per hari.")
    if usage_context == "work" and _is_answered(answers.get("escalation_target")):
        if not _work_escalation_is_detailed(answers.get("escalation_target"), operator_phone):
            invalid_fields.append("escalation_target")
            validation_errors.append(
                "Untuk pekerjaan/bisnis, escalation_target harus memuat kondisi pemicu, nama/role penerima, dan nomor WhatsApp."
            )
        discovery_phone = _phone_from_escalation(answers.get("escalation_target"))
        if discovery_phone and operator_phone:
            if re.sub(r"\D", "", discovery_phone) != re.sub(r"\D", "", str(operator_phone)):
                invalid_fields.append("escalation_target")
                validation_errors.append(
                    "Nomor eskalasi di discovery berbeda dari operator_phone yang akan dipakai. Minta user memilih nomor yang benar."
                )
    if supplied_name and external_name and supplied_name.casefold() != external_name.casefold():
        invalid_fields.append("agent_name")
        validation_errors.append(
            f"Nama di discovery ('{supplied_name}') berbeda dari nama yang akan dibuat ('{external_name}')."
        )

    unresolved_set = {*missing_fields, *invalid_fields}
    unresolved_fields = [
        field
        for group in _GROUPS
        for field in group["fields"]
        if field in unresolved_set
    ]
    completed_fields = [
        field for field in required_fields if field not in unresolved_fields and _is_answered(answers.get(field))
    ]

    if unresolved_fields:
        next_group = _group_for_field(unresolved_fields[0])
        group_missing = [field for field in next_group["fields"] if field in unresolved_fields]
    elif require_confirmation and not _confirmed(answers.get("user_confirmed")):
        next_group = {"id": "confirmation", "label": "Konfirmasi Akhir", "fields": ("user_confirmed",)}
        group_missing = ["user_confirmed"]
    else:
        next_group = None
        group_missing = []

    progress = []
    for group in _GROUPS:
        applicable = [
            field
            for field in group["fields"]
            if not (usage_context == "personal" and field in {"escalation_target", "go_live_approver"})
        ]
        pending = [field for field in applicable if field in unresolved_fields]
        progress.append(
            {
                "group_id": group["id"],
                "label": group["label"],
                "status": "skipped" if not applicable else "complete" if not pending else "pending",
                "completed": len(applicable) - len(pending),
                "required": len(applicable),
            }
        )

    complete = not unresolved_fields and (not require_confirmation or _confirmed(answers.get("user_confirmed")))
    next_questions = [
        {"topic": field, "question": _QUESTIONS[field]}
        for field in group_missing
    ]
    return {
        "complete": complete,
        "usage_context": usage_context,
        "normalized_answers": answers,
        "required_fields": required_fields,
        "completed_fields": completed_fields,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
        "validation_errors": validation_errors,
        "group_progress": progress,
        "next_group": (
            {"id": next_group["id"], "label": next_group["label"]}
            if next_group else None
        ),
        "next_questions": next_questions,
        "skipped_for_personal": (
            ["escalation_target", "go_live_approver"] if usage_context == "personal" else []
        ),
        "ignored_fields": ignored_fields,
        "operational_hours_requested": False,
    }


def discovery_operator_phone(discovery: dict[str, Any]) -> str:
    """Return the confirmed work escalation phone, when present."""
    answers = discovery.get("normalized_answers") or {}
    return _phone_from_escalation(answers.get("escalation_target"))


def discovery_escalation_policy(discovery: dict[str, Any]) -> str:
    """Map confirmed discovery to the existing owner/operator/none setting."""
    target = (discovery.get("normalized_answers") or {}).get("escalation_target")
    if discovery.get("usage_context") == "personal" and not _is_answered(target):
        return "none"
    if isinstance(target, dict):
        recipient = " ".join(
            str(target.get(key) or "")
            for key in ("recipient", "recipient_name_or_role", "name", "role")
        ).lower()
    else:
        recipient = str(target or "").lower()
    return "owner" if "owner" in recipient or "pemilik" in recipient else "operator"

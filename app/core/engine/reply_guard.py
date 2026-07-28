from __future__ import annotations

import json
import re
from typing import Any

from app.core.engine.tool_capability_registry import disabled_capability_claims


_BUILDER_TOOLS = {
    "plan_agent",
    "compose_agent_blueprint",
    "compose_agent_instructions",
    "compose_agent_soul",
    "validate_agent_config",
    "create_agent",
    "verify_agent",
    "create_wa_dev_trial_link",
    "send_agent_wa_qr",
    "set_agent_memory",
    "update_agent",
    "delete_agent",
    "get_agent_detail",
    "list_my_agents",
    "generate_google_auth_link",
}

_INCOMPLETE_BUILDER_REPLY_MARKERS = (
    "soul sudah siap",
    "soulnya sudah siap",
    "soul agent sudah siap",
    "sudah saya susun soul",
    "tinggal create",
    "tinggal dibuat",
    "siap dibuat",
    "mau saya buat",
    "mau saya lanjut",
    "langsung aku betulin",
    "langsung saya betulin",
    "langsung aku hidupkan",
    "langsung saya aktifkan",
    "saya proses",
    "aku proses",
    "sekarang aku proses",
    "sekarang saya proses",
    "masih saya proses",
    "cek dulu konfigurasi",
    "placeholder",
    "lanjut buat",
    "lanjutkan buat",
    "panggil perencanaan",
    "panggil dulu perencanaan",
    "panggil plan",
    "sedang merencanakan",
    "semua data yang sudah terkumpul",
)

_UPDATE_INTENT_TOOLS = {
    "update_agent",
    "get_agent_detail",
    "list_my_agents",
    "set_agent_memory",
}


def _step_tool_names(steps: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for step in steps or []:
        name = str(step.get("tool", "")).strip()
        if name and name not in names:
            names.append(name)
    return names


def _parse_step_result(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _builder_entitlement_retry_reply(steps: list[dict[str, Any]]) -> str | None:
    for step in reversed(steps or []):
        if step.get("tool") not in {"create_agent", "update_agent"}:
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            continue
        error = str(data.get("error") or "").lower()
        if "entitlement" in error or "melebihi entitlement plan" in error:
            return (
                "Ada batas plan untuk beberapa fitur, jadi saya sesuaikan konfigurasi yang sesuai dulu "
                "dan coba ulang sekarang."
            )
    return None


def _builder_success_reply_is_clear(reply: str) -> bool:
    normalized = reply.lower()
    if any(
        marker in normalized
        for marker in (
            "belum berhasil",
            "tidak berhasil",
            "gagal dibuat",
            "gagal diupdate",
            "belum selesai",
        )
    ):
        return False
    return any(
        marker in normalized
        for marker in (
            "sudah jadi",
            "berhasil dibuat",
            "berhasil diupdate",
            "sudah diupdate",
            "sudah saya edit",
            "sudah saya perbarui",
            "sudah diperbarui",
            "berhasil diperbarui",
            "sudah saya update",
            "paket trial",
            "tidak mengizinkan",
            "perlu upgrade",
            "entitlement",
        )
    )


def _has_whatsapp_onboarding(reply: str) -> bool:
    normalized = reply.lower()
    if "nomor demo arthur" not in normalized:
        return False
    premature_dedicated_number_markers = (
        "nomor whatsapp kamu sendiri",
        "nomor khusus",
        "langsung dipasang",
    )
    return not any(marker in normalized for marker in premature_dedicated_number_markers)


def _looks_like_incomplete_builder_reply(reply: str) -> bool:
    normalized = reply.lower()
    return any(marker in normalized for marker in _INCOMPLETE_BUILDER_REPLY_MARKERS)


def _is_simple_greeting(user_message: str) -> bool:
    text = " ".join(str(user_message or "").casefold().split()).strip(".,!🙏👋🙂😊")
    return bool(
        re.fullmatch(
            r"(?:halo|hai|hi|hello|p|pagi|siang|sore|malam)"
            r"(?:\s+(?:arthur|bro|sis|min|admin))?",
            text,
        )
    )


def _looks_like_technical_builder_reply(reply: str) -> bool:
    normalized = reply.lower()
    return any(
        marker in normalized
        for marker in (
            "field yang diubah",
            "updated_fields",
            "tools_config",
            "escalation_config",
            "allowed_senders",
            "operator_ids",
            "include_instructions",
        )
    )


def _is_builder_context(
    steps: list[dict[str, Any]],
    active_groups: list[str] | tuple[str, ...] | set[str] | None,
) -> bool:
    groups = {str(group) for group in (active_groups or [])}
    return "builder" in groups or any(name in _BUILDER_TOOLS for name in _step_tool_names(steps))


def _sanitize_builder_channel_reply(reply: str) -> str:
    text = (reply or "").strip()
    normalized = text.lower()
    if "dashboard" in normalized or any(
        marker in normalized
        for marker in (
            "settings → hubungkan whatsapp",
            "settings -> hubungkan whatsapp",
        )
    ):
        return (
            "Semua pengaturan agent dilakukan lewat chat WhatsApp ini. "
            "Untuk mencoba agent, pilih nomor demo Arthur agar saya kirim link wa.me dan kode. "
            "Untuk memasang ke nomor khusus milikmu, pilih nomor khusus agar saya kirim scan sekali dari WhatsApp."
        )
    if "webchat" not in normalized and "web chat" not in normalized:
        return text
    if "channel" not in normalized and "whatsapp" not in normalized:
        return text

    kept_lines: list[str] = []
    removed_channel_offer = False
    for line in text.splitlines():
        line_lower = line.lower()
        if "webchat" in line_lower or "web chat" in line_lower:
            removed_channel_offer = True
            continue
        if "channel apa" in line_lower or "mau channel" in line_lower:
            removed_channel_offer = True
            continue
        kept_lines.append(line.rstrip())

    sanitized = "\n".join(kept_lines).strip()
    channel_note = (
        "Channelnya saya set ke WhatsApp. Setelah jadi, kita uji dulu lewat nomor demo Arthur "
        "supaya kualitas jawaban dan alurnya bisa dicek tanpa setup nomor sendiri."
    )
    if not removed_channel_offer:
        return sanitized or channel_note
    if not sanitized:
        return channel_note
    if "nomor demo arthur" in sanitized.lower() and "nomor whatsapp kamu sendiri" not in sanitized.lower():
        return sanitized
    return f"{sanitized}\n\n{channel_note}"


def _latest_verify_result(
    steps: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    for step in reversed(steps or []):
        if step.get("tool") != "verify_agent":
            continue
        return _parse_step_result(step.get("result"))
    return None


def _create_agent_success_reply(
    data: dict[str, Any],
    *,
    steps: list[dict[str, Any]] | None = None,
) -> str:
    name = str(data.get("name") or "agent").strip()
    agent_id = str(data.get("agent_id") or "").strip()
    channel = str(data.get("channel_type") or data.get("channel") or "").strip().lower()
    verify = _latest_verify_result(steps)
    launch_status = str((verify or {}).get("status") or "").strip().lower()
    setup = (verify or {}).get("setup_status_for_owner")
    setup = setup if isinstance(setup, dict) else {}
    setup_summary = str(setup.get("summary_for_owner") or "").strip()
    status_prefix = f"{name} sudah jadi."
    if launch_status == "launch_blocked":
        status_prefix = (
            f"{name} sudah dibuat, tetapi belum siap dipakai penuh."
            + (f" {setup_summary}" if setup_summary else "")
        )

    if channel == "whatsapp":
        return (
            f"{status_prefix} Pilih cara menghubungkannya lewat WhatsApp:\n"
            "1. Nomor demo Arthur — saya kirim link wa.me dan kode untuk langsung mencoba.\n"
            "2. Nomor khusus milikmu — saya kirim scan sekali dari WhatsApp untuk menghubungkannya.\n"
            "Balas `nomor demo` atau `nomor khusus`."
        )
    if agent_id:
        return f"{status_prefix} ID agent: {agent_id}."
    return status_prefix


def _render_builder_questions(questions: Any) -> str | None:
    if not isinstance(questions, list):
        return None
    question_texts = [
        str(item.get("question") or "").strip()
        for item in questions
        if (
            isinstance(item, dict)
            and str(item.get("topic") or "").strip() != "user_confirmed"
            and str(item.get("question") or "").strip()
        )
    ]
    if not question_texts:
        return None
    # The discovery validator returns every missing field in the current group,
    # but WhatsApp should reveal them progressively. Asking only the first
    # highest-priority question keeps the exchange short and lets later turns
    # incorporate information the user volunteers without repeating a checklist.
    return question_texts[0]


_CLARIFICATION_TOPIC_SIGNAL_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "problem": (
        ("masalah", "kendala", "kewalahan", "sulit", "problem", "challenge", "struggle", "pain"),
    ),
    "usage_context": (
        ("pribadi", "personal"),
        ("bisnis", "usaha", "pekerjaan", "kerja", "business", "work"),
    ),
    "agent_name": (("nama", "name", "call the agent", "call it"),),
    "audience": (
        ("siapa", "who"),
        ("chat", "pakai", "menggunakan", "customer", "pelanggan", "tim", "use"),
    ),
    "main_tasks": (
        ("tugas", "kerjakan", "alur", "proses", "workflow", "task", "job", "do"),
    ),
    "capabilities": (
        ("teks", "chat", "file", "gambar", "foto", "laporan", "text", "image", "report"),
    ),
    "prohibited_actions": (
        ("tidak boleh", "dilarang", "batas", "keputusan", "must not", "not allowed", "boundary"),
    ),
    "allowed_actions": (
        ("boleh", "wewenang", "izin", "allowed", "authority", "permission"),
    ),
    "tone_style": (("tone", "gaya", "bahasa", "style", "language"),),
    "ideal_conversations": (("contoh", "percakapan", "example", "conversation"),),
    "avoided_conversations": (
        ("hindari", "jangan", "red line", "avoid", "must not"),
    ),
    "unknown_handling": (
        ("tidak tahu", "tidak tersedia", "tidak pasti", "don't know", "not available", "uncertain"),
    ),
    "escalation_target": (
        ("eskalasi", "manusia", "admin", "owner", "escalat", "human"),
        ("siapa", "mana", "who", "which", "recipient", "penerima"),
    ),
    "knowledge_sources": (
        ("sumber", "knowledge", "dokumen", "website", "database", "source"),
    ),
    "integrations": (
        ("integrasi", "google", "crm", "payment", "database", "integration", "system"),
    ),
    "expected_outputs": (("output", "hasil", "laporan", "spreadsheet", "report"),),
    "vision_requirement": (("gambar", "foto", "image", "photo", "vision"),),
}


def _is_natural_builder_clarification(text: str, *, topic: str = "") -> bool:
    """Keep a concise model-written question instead of forcing form copy.

    ``plan_agent`` remains authoritative about *what* is unresolved, while the
    model is allowed to phrase that question using the conversation's language
    and context. Deterministic copy is only a fallback for empty, internal, or
    misleading progress replies.
    """
    candidate = str(text or "").strip()
    if (
        not candidate
        or "?" not in candidate
        or len(candidate) > 700
    ):
        return False
    normalized = candidate.casefold()
    internal_markers = (
        "plan_agent",
        "discovery_progress",
        "next_questions",
        "capability_clarifications",
        "_evidence",
        "evidence format",
        "format evidence",
        "tool call",
        "panggil tool",
    )
    premature_success_markers = (
        "agent sudah jadi",
        "agent berhasil dibuat",
        "agent telah dibuat",
        "agent is ready",
        "agent has been created",
        "siap digunakan",
        "siap dipakai",
    )
    if any(marker in normalized for marker in (*internal_markers, *premature_success_markers)):
        return False

    signal_groups = _CLARIFICATION_TOPIC_SIGNAL_GROUPS.get(str(topic or "").strip())
    if not signal_groups:
        return False
    question_clauses = [
        clause.casefold().strip()
        for clause in re.findall(r"[^?]+\?", candidate)
        if clause.strip()
    ]
    return any(
        all(
            any(signal in clause for signal in group)
            for group in signal_groups
        )
        for clause in question_clauses
    )


def _builder_clarification_entry(data: dict[str, Any]) -> tuple[str, str] | None:
    questions = data.get("capability_clarifications") or []
    if not questions:
        progress = data.get("discovery_progress")
        if isinstance(progress, dict):
            questions = progress.get("next_questions") or []
    if not isinstance(questions, list):
        return None
    for item in questions:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()
        question = str(item.get("question") or "").strip()
        if topic != "user_confirmed" and question:
            return topic, question
    return None


def _builder_clarification_reply(data: dict[str, Any]) -> str | None:
    """Turn deterministic builder blockers into questions, never failure text."""
    entry = _builder_clarification_entry(data)
    if entry:
        return entry[1]

    code = str(data.get("error_code") or "").strip().upper()
    error = str(data.get("error") or "").strip().lower()
    if code in {"FILE_CAPABILITY_CONTRADICTION", "FILE_CAPABILITY_MISMATCH"}:
        return None
    if "kemampuan file belum diputuskan" in error or "keputusan kemampuan file" in error:
        return (
            "Sebelum saya buat, pilih kebutuhan file agent ini: hanya chat teks, menerima "
            "file/gambar dari user, membuat file/laporan untuk dikirim, atau keduanya?"
        )
    return None


_CREATE_ERROR_PRIORITY = {
    "FILE_CAPABILITY_CONTRADICTION": 100,
    "FILE_CAPABILITY_MISMATCH": 95,
    "LAUNCH_CAPABILITY_UNAVAILABLE": 90,
    "OPERATING_MANUAL_REQUIRED": 80,
    "CONFIG_UNSAFE": 70,
    "DISCOVERY_INCOMPLETE": 40,
}


def _best_create_failure(
    steps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, step in enumerate(steps or []):
        if step.get("tool") != "create_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if not data or data.get("success") is True or not data.get("error"):
            continue
        code = str(data.get("error_code") or "").strip().upper()
        error = str(data.get("error") or "").casefold()
        priority = _CREATE_ERROR_PRIORITY.get(code, 50)
        if not code:
            if "keputusan kemampuan file" in error:
                priority = _CREATE_ERROR_PRIORITY["FILE_CAPABILITY_MISMATCH"]
            elif "operating manual" in error:
                priority = _CREATE_ERROR_PRIORITY["OPERATING_MANUAL_REQUIRED"]
            elif "discovery kebutuhan agent" in error:
                priority = _CREATE_ERROR_PRIORITY["DISCOVERY_INCOMPLETE"]
        candidates.append((priority, index, data))
    if not candidates:
        return None
    # Prefer the most actionable root cause; for equal priorities the latest
    # result is authoritative.
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _plan_agent_clarification_reply(steps: list[dict[str, Any]]) -> str | None:
    """Return a question only when the latest plan still needs clarification."""
    for step in reversed(steps or []):
        if step.get("tool") != "plan_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            return None
        if str(data.get("plan_status") or "").strip().lower() == "needs_clarification":
            return _builder_clarification_reply(data)
        # A newer ready/blocked/temporary result supersedes every older plan.
        return None
    return None


def _plan_agent_clarification_topic(steps: list[dict[str, Any]]) -> str:
    """Return the authoritative unresolved discovery topic from the latest plan."""
    for step in reversed(steps or []):
        if step.get("tool") != "plan_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            return ""
        if str(data.get("plan_status") or "").strip().lower() != "needs_clarification":
            return ""
        entry = _builder_clarification_entry(data)
        return entry[0] if entry else ""
    return ""


def _builder_fallback_reply(
    steps: list[dict[str, Any]],
    *,
    whatsapp_action: str | None = None,
) -> str | None:
    entitlement_retry = _builder_entitlement_retry_reply(steps)
    if entitlement_retry:
        return entitlement_retry

    tool_names = _step_tool_names(steps)
    if not any(name in _BUILDER_TOOLS for name in tool_names):
        return None

    trial_link_error_reply: str | None = None
    for step in reversed(steps or []):
        if step.get("tool") != "create_wa_dev_trial_link":
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            continue
        if data.get("success") is False:
            error = str(data.get("error") or "").strip()
            if error in {
                "agent_target_required",
                "agent_name_ambiguous",
                "agent_name_not_found_or_ambiguous",
                "agent_target_ambiguous_for_current_request",
            }:
                agents = data.get("available_agents") or data.get("candidate_agents") or []
                names = [
                    str(item.get("agent_name") or "").strip()
                    for item in agents
                    if isinstance(item, dict) and str(item.get("agent_name") or "").strip()
                ]
                if names:
                    trial_link_error_reply = trial_link_error_reply or (
                        "Mau nomor demo agent yang mana? Pilih salah satu: " + ", ".join(names) + "."
                    )
                    continue
                trial_link_error_reply = trial_link_error_reply or "Mau nomor demo agent yang mana? Sebut nama agent-nya dulu ya."
                continue
            if error == "agent_target_conflict":
                detected = data.get("detected_agent") if isinstance(data.get("detected_agent"), dict) else {}
                name = str(detected.get("agent_name") or "").strip()
                if name:
                    trial_link_error_reply = trial_link_error_reply or f"Saya tahan dulu supaya tidak salah kirim. Kamu maksud nomor demo untuk {name}, kan?"
                    continue
                trial_link_error_reply = trial_link_error_reply or "Saya tahan dulu supaya tidak salah kirim. Sebut ulang nama agent yang kamu mau."
                continue
        link = data.get("wa_link") or data.get("link") or data.get("trial_link") or data.get("wa_me_url")
        code = data.get("trial_code") or data.get("code")
        if link and code:
            agent_name = str(data.get("agent_name") or "agent").strip()
            contact_name = str(data.get("shared_whatsapp_name") or "").strip()
            if data.get("contact_sent") and contact_name:
                return (
                    f"Link demo {agent_name}: {link}\n"
                    f"Kode: {code}. Setelah link dan kode ini, kontak {contact_name} juga sudah saya kirim."
                )
            return f"Kode trial {agent_name}: {code}. Link: {link}"
        if link:
            return f"Agent-nya sudah siap dicoba. Link: {link}"
    if trial_link_error_reply:
        return trial_link_error_reply

    for step in reversed(steps or []):
        if whatsapp_action == "trial_link":
            break
        if step.get("tool") != "send_agent_wa_qr":
            continue
        result_text = str(step.get("result") or "").strip()
        if "[QR_SENT]" in result_text:
            return (
                "Scan sekali dari WhatsApp sudah saya kirim ke chat kamu. "
                "Buka WhatsApp di nomor khusus yang akan dipasang, pilih Perangkat tertaut, "
                "lalu scan sekarang karena kodenya berlaku singkat."
            )
        if "[INFO]" in result_text:
            return "Nomor WhatsApp khusus itu sudah terhubung ke agent; tidak perlu scan ulang."
        if "[error]" in result_text.lower() or result_text.lower().startswith("error:"):
            detail = re.sub(r"^\[error\]\s*", "", result_text, flags=re.IGNORECASE)
            return f"Scan WhatsApp belum berhasil dikirim: {detail}"

    # A discovery question is a normal builder state, not a technical failure.
    # If the model produced an empty/progress-like reply, reconstruct the exact
    # user-facing questions from plan_agent instead of saying "coba lagi".
    clarification_reply = _plan_agent_clarification_reply(steps)
    if clarification_reply:
        return clarification_reply

    for step in reversed(steps or []):
        if step.get("tool") != "create_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if data and data.get("success") is True:
            return _create_agent_success_reply(data, steps=steps)

    create_failure = _best_create_failure(steps)
    if create_failure:
        clarification_reply = _builder_clarification_reply(create_failure)
        if clarification_reply:
            return clarification_reply
        error = str(create_failure.get("error") or "").strip()
        code = str(create_failure.get("error_code") or "").strip().upper()
        if code == "OPERATING_MANUAL_REQUIRED":
            return (
                "Konfigurasi agent belum dibuat karena SOP kerjanya belum tersusun. "
                "Saya pertahankan seluruh kebutuhan yang sudah dikonfirmasi dan akan menyusun SOP itu sebelum mencoba lagi."
            )
        if code in {"FILE_CAPABILITY_CONTRADICTION", "FILE_CAPABILITY_MISMATCH"}:
            return (
                "Agent belum dibuat karena konfigurasi internal kemampuan file tidak konsisten. "
                "Kebutuhan file yang sudah kamu konfirmasi tetap saya pertahankan; kamu tidak perlu menjawab ulang."
            )
        if code == "LAUNCH_CAPABILITY_UNAVAILABLE":
            return (
                "Agent belum dibuat karena kemampuan membuat file sedang belum tersedia di runtime. "
                "Kebutuhan yang sudah kamu konfirmasi tetap tersimpan dan tidak saya turunkan diam-diam."
            )
        if error:
            return f"Agent belum dibuat karena: {error}"

    for step in reversed(steps or []):
        if step.get("tool") != "update_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            continue
        if data.get("success") is True:
            name = str(data.get("agent_name") or data.get("name") or "Agent").strip()
            return f"{name} sudah saya edit."
        error = str(data.get("error") or "").strip()
        if error:
            return f"Belum berhasil diupdate: {error}"

    # The build/update chain ran but never reached create_agent/update_agent.
    # By the time this fallback is used, the runtime's internal continuation
    # retry has already been attempted. Do NOT surface a confusing
    # "gagal/belum berhasil ... kirim lanjut" loop to the user — frame it as a
    # transient system hiccup they can simply retry.
    if any(name in _UPDATE_INTENT_TOOLS for name in tool_names) and "update_agent" not in tool_names:
        return (
            "Maaf, lagi ada kendala sistem sebentar di sisi saya, jadi update agennya belum kelar. "
            "Coba kirim lagi ya, nanti saya lanjutkan sampai selesai."
        )

    if "create_agent" not in tool_names:
        return (
            "Maaf, lagi ada kendala sistem sebentar di sisi saya, jadi agennya belum selesai saya buat. "
            "Coba kirim lagi ya, nanti saya lanjutkan sampai selesai."
        )

    return (
        "Maaf, lagi ada kendala sistem sebentar di sisi saya. "
        "Coba kirim lagi ya, nanti saya lanjutkan sampai selesai."
    )


def _trial_link_reply_is_complete(reply: str, steps: list[dict[str, Any]]) -> bool:
    text = reply or ""
    for step in reversed(steps or []):
        if step.get("tool") != "create_wa_dev_trial_link":
            continue
        data = _parse_step_result(step.get("result"))
        if not data or data.get("success") is False:
            continue
        link = str(data.get("wa_link") or data.get("link") or data.get("trial_link") or data.get("wa_me_url") or "")
        code = str(data.get("trial_code") or data.get("code") or "")
        if link and link not in text:
            return False
        if code and code not in text:
            return False
        return True
    return True


def _disabled_capability_guard_reply(
    reply: str,
    *,
    tools_config: dict[str, Any] | None = None,
    active_groups: list[str] | tuple[str, ...] | set[str] | None = None,
) -> str | None:
    blocked = disabled_capability_claims(reply, tools_config=tools_config, active_groups=active_groups)
    if not blocked:
        return None
    primary = blocked[0]
    if len(blocked) == 1:
        return primary.fallback_sentence
    labels = ", ".join(cap.label for cap in blocked[:3])
    return (
        f"Saya belum bisa menjalankan beberapa kemampuan yang disebut tadi ({labels}) pada run ini. "
        "Owner perlu mengaktifkan/setup kemampuan itu dulu sebelum saya bisa mengerjakannya."
    )


def _scheduler_success_guard_reply(
    reply: str,
    steps: list[dict[str, Any]],
) -> str | None:
    """Block reminder success claims that have no successful scheduler tool result."""
    text = (reply or "").strip()
    normalized = text.casefold()
    if not text or re.search(
        r"\b(?:belum|tidak|gagal)\b.{0,32}\b(?:reminder|pengingat|jadwal|dibuat|disetel|diatur|dibatalkan)\b",
        normalized,
    ):
        return None

    success_claim = any(
        re.search(pattern, normalized)
        for pattern in (
            r"\b(?:reminder|pengingat|jadwal)\b.{0,24}\b(?:sudah|telah|berhasil)\b.{0,24}\b(?:dibuat|disetel|diatur|aktif|dibatalkan)\b",
            r"\b(?:sudah|telah|berhasil)\b.{0,24}\b(?:set|buat|atur|batalkan|membuat|mengatur|membatalkan)\b.{0,24}\b(?:reminder|pengingat|jadwal)\b",
        )
    )
    if not success_claim:
        return None

    scheduler_steps = [
        step for step in (steps or [])
        if step.get("tool") in {
            "set_reminder",
            "set_multiple_reminders",
            "cancel_reminder",
        }
    ]
    for step in reversed(scheduler_steps):
        result = str(step.get("result") or "").strip()
        result_lower = result.casefold()
        if (
            result
            and not result_lower.startswith("[error]")
            and (
                "berhasil di-set" in result_lower
                or "berhasil dibatalkan" in result_lower
            )
        ):
            return None

    if scheduler_steps:
        return (
            "Reminder belum berhasil diproses karena tool scheduler mengembalikan kegagalan. "
            "Silakan periksa kembali waktu atau jadwalnya, lalu coba lagi."
        )
    return (
        "Reminder belum berhasil dibuat karena tool scheduler belum dijalankan. "
        "Kirim ulang waktu dan pesan pengingatnya agar saya bisa menjadwalkannya."
    )


def ensure_non_empty_reply(
    reply: str,
    steps: list[dict[str, Any]],
    *,
    tools_config: dict[str, Any] | None = None,
    active_groups: list[str] | tuple[str, ...] | set[str] | None = None,
    user_message: str = "",
    builder_whatsapp_action: str | None = None,
) -> str:
    text = (reply or "").strip()
    entitlement_retry = _builder_entitlement_retry_reply(steps)
    if entitlement_retry:
        normalized = text.lower()
        retry_markers = ("coba ulang", "coba lagi", "retry", "sesuaikan konfigurasi")
        if not text or not any(marker in normalized for marker in retry_markers):
            return entitlement_retry

    # plan_agent is the deterministic source of the next unresolved field, but
    # it must not turn Arthur into a form renderer. Keep a natural model-written
    # question so Arthur can acknowledge context and honor the user's language.
    # Fall back to canonical copy only for empty/internal/misleading replies.
    plan_clarification = _plan_agent_clarification_reply(steps)
    if plan_clarification:
        plan_topic = _plan_agent_clarification_topic(steps)
        if plan_clarification.casefold() in text.casefold():
            return text
        if _is_natural_builder_clarification(text, topic=plan_topic):
            return text
        return plan_clarification

    normalized_request = " ".join(str(user_message or "").casefold().split())
    generic_whatsapp_setup = (
        any(
            marker in normalized_request
            for marker in (
                "cara pasang",
                "gimana pasang",
                "gimana cara pasang",
                "cara hubungkan",
                "cara menghubungkan",
                "pasang ke whatsapp",
            )
        )
        and not any(
            marker in normalized_request
            for marker in (
                "nomor demo",
                "nomor khusus",
                "nomor saya sendiri",
                "nomor whatsapp saya",
                "kirim qr",
                "scan qr",
            )
        )
        and not any(
            step.get("tool") in {"create_wa_dev_trial_link", "send_agent_wa_qr"}
            for step in steps or []
        )
    )
    if generic_whatsapp_setup and _is_builder_context(steps, active_groups):
        return (
            "Ada dua pilihan lewat WhatsApp:\n"
            "1. Nomor demo Arthur — saya kirim link wa.me dan kode supaya agent bisa langsung dicoba.\n"
            "2. Nomor khusus milikmu — saya kirim scan sekali dari WhatsApp untuk menghubungkan agent ke nomor itu.\n"
            "Balas `nomor demo` atau `nomor khusus`. Semua proses dilakukan di chat ini."
        )

    if text:
        if _is_builder_context(steps, active_groups):
            text = _sanitize_builder_channel_reply(text)
            if (
                _looks_like_incomplete_builder_reply(text)
                and not any(name in _BUILDER_TOOLS for name in _step_tool_names(steps))
                and _is_simple_greeting(user_message)
            ):
                return (
                    "Halo! Aku Arthur 👋 Aku bisa bantu bikin, mengubah, atau mengecek "
                    "AI agent WhatsApp. Ceritakan kebutuhanmu, ya."
                )
            if _looks_like_incomplete_builder_reply(text) and not any(
                name in _BUILDER_TOOLS for name in _step_tool_names(steps)
            ):
                return (
                    "Kebutuhan terakhir sudah saya catat, tetapi belum ada eksekusi builder "
                    "yang terverifikasi pada turn ini. Progres tersimpan aman; saya akan "
                    "melanjutkan dari state tersebut tanpa meminta jawaban yang sama."
                )
        builder_reply = _builder_fallback_reply(
            steps,
            whatsapp_action=builder_whatsapp_action,
        )
        tool_names = _step_tool_names(steps)
        if (
            builder_reply
            and "create_wa_dev_trial_link" in tool_names
            and not _trial_link_reply_is_complete(text, steps)
        ):
            return builder_reply
        if (
            builder_reply
            and "send_agent_wa_qr" in tool_names
            and builder_whatsapp_action != "trial_link"
        ):
            return builder_reply
        if (
            builder_reply
            and "create_agent" in tool_names
            and not (
                "nomor demo arthur" in text.casefold()
                and "nomor khusus" in text.casefold()
            )
        ):
            return builder_reply
        missing_whatsapp_onboarding = (
            builder_reply
            and "nomor demo Arthur" in builder_reply
            and not _has_whatsapp_onboarding(text)
        )
        if builder_reply and (
            not _builder_success_reply_is_clear(text)
            or _looks_like_technical_builder_reply(text)
            or missing_whatsapp_onboarding
        ):
            if (
                "create_agent" in tool_names
                or "update_agent" in tool_names
                or _looks_like_incomplete_builder_reply(text)
                or _looks_like_technical_builder_reply(text)
            ):
                return builder_reply
        scheduler_guard_reply = _scheduler_success_guard_reply(text, steps)
        if scheduler_guard_reply:
            return scheduler_guard_reply
        disabled_guard_reply = _disabled_capability_guard_reply(
            text,
            tools_config=tools_config,
            active_groups=active_groups,
        )
        return disabled_guard_reply or text

    builder_reply = _builder_fallback_reply(
        steps,
        whatsapp_action=builder_whatsapp_action,
    )
    if builder_reply:
        return builder_reply

    url_pat = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")
    for step in steps or []:
        result = str(step.get("result", ""))
        match = url_pat.search(result)
        if match:
            return f"Proses selesai. Cek hasilnya di sini: {match.group(0).rstrip('.,)')}"

    if steps:
        tool_names = _step_tool_names(steps)
        if tool_names:
            return "Prosesnya sudah saya jalankan. Kalau hasilnya belum muncul, kirim lanjut ya."

    return "Maaf, proses lagi gangguan. Coba kirim ulang pesanmu ya."

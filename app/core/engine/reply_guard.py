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
    "masih saya proses",
    "cek dulu konfigurasi",
    "placeholder",
    "lanjut buat",
    "lanjutkan buat",
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


def _create_agent_success_reply(data: dict[str, Any]) -> str:
    name = str(data.get("name") or "agent").strip()
    agent_id = str(data.get("agent_id") or "").strip()
    channel = str(data.get("channel_type") or data.get("channel") or "").strip().lower()

    if channel == "whatsapp":
        return (
            f"{name} sudah jadi. Pilih cara menghubungkannya lewat WhatsApp:\n"
            "1. Nomor demo Arthur — saya kirim link wa.me dan kode untuk langsung mencoba.\n"
            "2. Nomor khusus milikmu — saya kirim scan sekali dari WhatsApp untuk menghubungkannya.\n"
            "Balas `nomor demo` atau `nomor khusus`."
        )
    if agent_id:
        return f"{name} sudah jadi. ID agent: {agent_id}."
    return f"{name} sudah jadi."


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


def _builder_clarification_reply(data: dict[str, Any]) -> str | None:
    """Turn deterministic builder blockers into questions, never failure text."""
    questions = data.get("capability_clarifications") or []
    if not questions:
        progress = data.get("discovery_progress")
        if isinstance(progress, dict):
            questions = progress.get("next_questions") or []
    rendered = _render_builder_questions(questions)
    if rendered:
        return rendered

    error = str(data.get("error") or "").strip().lower()
    if "kemampuan file belum diputuskan" in error or "keputusan kemampuan file" in error:
        return (
            "Sebelum saya buat, pilih kebutuhan file agent ini: hanya chat teks, menerima "
            "file/gambar dari user, membuat file/laporan untuk dikirim, atau keduanya?"
        )
    return None


def _plan_agent_clarification_reply(steps: list[dict[str, Any]]) -> str | None:
    for step in reversed(steps or []):
        if step.get("tool") != "plan_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if not data or str(data.get("plan_status") or "").strip().lower() != "needs_clarification":
            continue
        return _builder_clarification_reply(data)
    return None


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
        if not data:
            continue
        if data.get("success") is True:
            return _create_agent_success_reply(data)
        clarification_reply = _builder_clarification_reply(data)
        if clarification_reply:
            return clarification_reply
        error = str(data.get("error") or "").strip()
        if error:
            return f"Belum berhasil dibuat: {error}"

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

    # plan_agent is the deterministic source of the next unresolved field.
    # Do not let a non-empty model progress note or internal "evidence format"
    # explanation replace it and send the user into another discovery loop.
    plan_clarification = _plan_agent_clarification_reply(steps)
    if plan_clarification:
        if plan_clarification.casefold() in text.casefold():
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

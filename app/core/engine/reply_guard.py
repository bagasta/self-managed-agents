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
    "create_agent_from_brief",
    "verify_agent",
    "create_wa_dev_trial_link",
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
    "setuju",
    "konfirmasi",
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
        if step.get("tool") not in {"create_agent", "create_agent_from_brief", "update_agent"}:
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
    return (
        "nomor demo arthur" in normalized
        and "nomor whatsapp kamu sendiri" in normalized
    )


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
        "Channelnya saya set ke WhatsApp. Setelah jadi, bisa dicoba lewat nomor demo Arthur "
        "atau dipasang ke nomor WhatsApp kamu sendiri."
    )
    if not removed_channel_offer:
        return sanitized or channel_note
    if not sanitized:
        return channel_note
    if "nomor demo arthur" in sanitized.lower() and "nomor whatsapp kamu sendiri" in sanitized.lower():
        return sanitized
    return f"{sanitized}\n\n{channel_note}"


def _create_agent_success_reply(data: dict[str, Any]) -> str:
    name = str(data.get("name") or "agent").strip()
    agent_id = str(data.get("agent_id") or "").strip()
    channel = str(data.get("channel_type") or data.get("channel") or "").strip().lower()

    if channel == "whatsapp":
        return (
            f"{name} sudah jadi. "
            "Sekarang mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, "
            "atau dicoba dulu lewat nomor demo Arthur yang sudah siap pakai?"
        )
    if agent_id:
        return f"{name} sudah jadi. ID agent: {agent_id}."
    return f"{name} sudah jadi."


def _builder_fallback_reply(steps: list[dict[str, Any]]) -> str | None:
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
                return f"Kontak {contact_name} sudah saya kirim. Kode trial {agent_name}: {code}. Link: {link}"
            return f"Kode trial {agent_name}: {code}. Link: {link}"
        if link:
            return f"Agent-nya sudah siap dicoba. Link: {link}"
    if trial_link_error_reply:
        return trial_link_error_reply

    for step in reversed(steps or []):
        if step.get("tool") not in {"create_agent", "create_agent_from_brief"}:
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            continue
        if data.get("success") is True:
            return _create_agent_success_reply(data)
        if step.get("tool") == "create_agent_from_brief":
            clarifications = data.get("capability_clarifications")
            questions = [
                str(item.get("question") or "").strip()
                for item in (clarifications or [])
                if isinstance(item, dict) and str(item.get("question") or "").strip()
            ]
            if questions:
                return "Sebelum saya buat, saya perlu memastikan: " + " ".join(questions[:3])
            if data.get("status") in {"blocked_by_policy", "blocked_by_subscription", "has_errors"}:
                next_action = str(data.get("next_action") or "").strip()
                errors = [str(item).strip() for item in (data.get("validation_errors") or []) if str(item).strip()]
                if errors:
                    return "Belum bisa dibuat: " + " ".join(errors[:3])
                if next_action:
                    return next_action
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

    if not {"create_agent", "create_agent_from_brief"}.intersection(tool_names):
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
) -> str:
    text = (reply or "").strip()
    entitlement_retry = _builder_entitlement_retry_reply(steps)
    if entitlement_retry:
        normalized = text.lower()
        retry_markers = ("coba ulang", "coba lagi", "retry", "sesuaikan konfigurasi")
        if not text or not any(marker in normalized for marker in retry_markers):
            return entitlement_retry

    if text:
        if _is_builder_context(steps, active_groups):
            text = _sanitize_builder_channel_reply(text)
        builder_reply = _builder_fallback_reply(steps)
        tool_names = _step_tool_names(steps)
        if (
            builder_reply
            and "create_wa_dev_trial_link" in tool_names
            and not _trial_link_reply_is_complete(text, steps)
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
                or "create_agent_from_brief" in tool_names
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

    builder_reply = _builder_fallback_reply(steps)
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

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

    for step in reversed(steps or []):
        if step.get("tool") != "create_wa_dev_trial_link":
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            continue
        link = data.get("wa_link") or data.get("link") or data.get("trial_link")
        code = data.get("trial_code") or data.get("code")
        if link and code:
            return f"Agent-nya sudah siap dicoba. Kode trialnya {code}. Link: {link}"
        if link:
            return f"Agent-nya sudah siap dicoba. Link: {link}"

    for step in reversed(steps or []):
        if step.get("tool") != "create_agent":
            continue
        data = _parse_step_result(step.get("result"))
        if not data:
            continue
        if data.get("success") is True:
            return _create_agent_success_reply(data)
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
        builder_reply = _builder_fallback_reply(steps)
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
            tool_names = _step_tool_names(steps)
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

    url_pat = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")
    for step in steps or []:
        result = str(step.get("result", ""))
        match = url_pat.search(result)
        if match:
            return f"Proses selesai. Cek hasilnya di sini: {match.group(0).rstrip('.,)')}"

    builder_reply = _builder_fallback_reply(steps)
    if builder_reply:
        return builder_reply

    if steps:
        tool_names = _step_tool_names(steps)
        if tool_names:
            return "Prosesnya sudah saya jalankan. Kalau hasilnya belum muncul, kirim lanjut ya."

    return "Maaf, proses lagi gangguan. Coba kirim ulang pesanmu ya."

from __future__ import annotations

import json
import re
from typing import Any


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


def _builder_success_reply_is_clear(reply: str) -> bool:
    normalized = reply.lower()
    return any(marker in normalized for marker in ("sudah jadi", "berhasil dibuat", "berhasil diupdate", "sudah diupdate"))


def _looks_like_incomplete_builder_reply(reply: str) -> bool:
    normalized = reply.lower()
    return any(marker in normalized for marker in _INCOMPLETE_BUILDER_REPLY_MARKERS)


def _builder_fallback_reply(steps: list[dict[str, Any]]) -> str | None:
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
            name = str(data.get("name") or "agent").strip()
            agent_id = str(data.get("agent_id") or "").strip()
            if agent_id:
                return f"{name} sudah jadi. ID agent: {agent_id}."
            return f"{name} sudah jadi."
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
            fields = data.get("updated_fields") if isinstance(data.get("updated_fields"), list) else []
            field_text = f" Field yang diubah: {', '.join(map(str, fields))}." if fields else ""
            return f"{name} berhasil diupdate.{field_text}"
        error = str(data.get("error") or "").strip()
        if error:
            return f"Belum berhasil diupdate: {error}"

    if "create_agent" not in tool_names:
        return (
            "Agent belum berhasil dibuat di giliran ini karena proses berhenti sebelum tahap pembuatan. "
            "Kirim lanjut, saya akan teruskan langsung tanpa tanya ulang."
        )

    return "Proses pembuatan agent belum selesai dengan jelas. Kirim lanjut, saya akan cek dan teruskan langsung."


def ensure_non_empty_reply(reply: str, steps: list[dict[str, Any]]) -> str:
    text = (reply or "").strip()
    if text:
        builder_reply = _builder_fallback_reply(steps)
        if builder_reply and not _builder_success_reply_is_clear(text):
            tool_names = _step_tool_names(steps)
            if "create_agent" in tool_names or "update_agent" in tool_names or _looks_like_incomplete_builder_reply(text):
                return builder_reply
        return text

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

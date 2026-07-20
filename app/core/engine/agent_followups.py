"""Followup/deploy/file-delivery/builder-create detector helpers.

Extracted from agent_runner.py — pure functions, no async, no DB access.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.engine.agent_step_utils import (
    _URL_RE,
    _has_whatsapp_media_send_step,
    _parse_step_result_json,
)
from app.core.engine.tool_builder import _is_enabled

_SHARED_WORKSPACE_FILE_RE = re.compile(r"(/workspace/shared/[^\s`'\"\\),\]}]+)")
_FAILED_STEP_MARKERS = (
    "[error]",
    "error:",
    "not a valid tool",
    "traceback",
    "gagal",
    "failed",
)


def _has_external_service_fallback_blocked_step(steps: list[dict[str, Any]]) -> bool:
    marker = "This is a Google Workspace external-service action"
    return any(marker in str((step or {}).get("result", "") or "") for step in steps or [])


def _step_text(step: dict[str, Any]) -> str:
    return "\n".join(
        str(step.get(key) or "")
        for key in ("tool", "args", "result", "content")
        if step.get(key) is not None
    )


def _has_public_url_in_text(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


def _has_public_url_in_steps(steps: list[dict[str, Any]]) -> bool:
    return any(_has_public_url_in_text(_step_text(step)) for step in steps or [])


def _extract_shared_workspace_file_path(*values: Any) -> str | None:
    for value in values:
        text = str(value or "")
        for match in _SHARED_WORKSPACE_FILE_RE.findall(text):
            path = match.rstrip("\\.,;:)")
            name = path.rsplit("/", 1)[-1]
            if name and "." in name:
                return path
    return None


def _step_failed(step: dict[str, Any]) -> bool:
    result = str((step or {}).get("result") or "").strip().lower()
    if not result:
        return True
    return any(marker in result for marker in _FAILED_STEP_MARKERS)


def _extract_ready_shared_workspace_file_from_task_steps(
    steps: list[dict[str, Any]],
) -> str | None:
    """Return only a subagent artifact that explicitly completed its handoff."""
    for step in reversed(steps or []):
        if str((step or {}).get("tool") or "") != "task":
            continue
        result = str((step or {}).get("result") or "")
        if _step_failed(step) or "siap_dikirim_parent" not in result.lower():
            continue
        path = _extract_shared_workspace_file_path(result)
        if path:
            return path
    return None


def _extract_verified_shared_workspace_file_from_steps(
    steps: list[dict[str, Any]],
) -> str | None:
    """Extract an artifact only from a successful file-producing/sending step."""
    direct_file_tools = {
        "write_file",
        "edit_file",
        "sandbox_write_binary_file",
    }
    for step in reversed(steps or []):
        tool_name = str((step or {}).get("tool") or "")
        if tool_name == "task":
            result = str((step or {}).get("result") or "")
            if _step_failed(step) or "siap_dikirim_parent" not in result.lower():
                continue
            path = _extract_shared_workspace_file_path(result)
        elif tool_name in {"send_whatsapp_document", "send_whatsapp_image"}:
            if not _has_whatsapp_media_send_step([step]):
                continue
            path = _extract_shared_workspace_file_path((step or {}).get("args"))
        elif tool_name == "execute":
            if _step_failed(step):
                continue
            # Arbitrary code may mention many input/history paths. The command
            # must print the final artifact path before it becomes durable state.
            path = _extract_shared_workspace_file_path((step or {}).get("result"))
        elif tool_name in direct_file_tools:
            if _step_failed(step):
                continue
            path = _extract_shared_workspace_file_path(
                (step or {}).get("result"),
                (step or {}).get("args"),
            )
        else:
            continue
        if path:
            return path
    return None


def _user_requested_inline_text_output(user_message: str) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    explicit_file_markers = (
        "kirim file",
        "send file",
        "attachment",
        "lampiran",
        "file txt",
        "txt file",
        ".txt",
    )
    if any(marker in text for marker in explicit_file_markers):
        return False
    inline_markers = (
        "ascii",
        "ascii art",
        "teks saja",
        "text only",
        "plain text",
        "in text form",
        "text form",
        "send it all in text",
        "kirim sebagai teks",
        "di chat aja",
        "langsung di chat",
        "jangan file",
        "tanpa file",
        "not a file",
    )
    return any(marker in text for marker in inline_markers)


def _is_whatsapp_file_delivery_request(user_message: str, steps: list[dict[str, Any]], final_reply: str) -> bool:
    if _user_requested_inline_text_output(user_message):
        return False
    step_results = [str((step or {}).get("result") or "") for step in steps or []]
    text = "\n".join([user_message or "", final_reply or ""] + step_results).lower()
    markers = (
        "siap_dikirim_parent",
        "kirim file",
        "kirim filenya",
        "file-nya",
        "filenya",
        "kirim dokumen",
        "kirim gambar",
        "kirim foto",
        "dokumen",
        "attachment",
        "lampiran",
    )
    return any(marker in text for marker in markers) or bool(
        re.search(r"\.(?:pdf|docx|xlsx|csv|zip)(?:\b|$)", text)
    )


def _needs_whatsapp_file_delivery_followup(
    user_message: str,
    tools_config: dict[str, Any],
    steps: list[dict[str, Any]],
    final_reply: str,
) -> tuple[bool, str | None]:
    """Detect subagent-created shared files that still need parent WA delivery."""
    if _user_requested_inline_text_output(user_message):
        return False, None
    if not _is_enabled(tools_config, "whatsapp_media", default=True):
        return False, None
    if _has_whatsapp_media_send_step(steps):
        return False, None
    path = _extract_ready_shared_workspace_file_from_task_steps(steps)
    if not path:
        return False, None
    if not _is_whatsapp_file_delivery_request(user_message, steps, final_reply):
        return False, None
    return True, path


def _whatsapp_file_delivery_followup_message(
    final_reply: str,
    steps: list[dict[str, Any]],
    shared_path: str,
) -> str:
    filename = shared_path.rsplit("/", 1)[-1] or "file"
    tool_names = ", ".join(
        str(step.get("tool") or "?")
        for step in (steps or [])[-8:]
        if step.get("tool")
    )
    return (
        "LANJUTKAN TASK SEBELUMNYA: subagent sudah membuat file final di shared workspace, "
        "tetapi parent belum mengirim file ke WhatsApp.\n\n"
        f"Path file final: {shared_path}\n"
        f"Filename: {filename}\n"
        f"Ringkasan jawaban sebelumnya: {(final_reply or '').strip()[:1200]}\n"
        f"Tool terakhir: {tool_names or '-'}\n\n"
        "Wajib sekarang panggil tool WhatsApp parent, bukan task/subagent. "
        "Untuk PDF/DOCX/XLSX/CSV/ZIP gunakan send_whatsapp_document(file_path_or_base64=path, filename=filename, caption=...). "
        "Untuk PNG/JPG/JPEG/WEBP gunakan send_whatsapp_image(image_path_or_base64=path, caption=...). "
        "Setelah tool mengembalikan sukses, jawab final singkat bahwa file sudah dikirim. "
        "Jika tool error, sampaikan error nyatanya tanpa mengklaim terkirim."
    )


def _is_website_or_app_request(user_message: str) -> bool:
    text = (user_message or "").lower()
    markers = (
        "website",
        "web site",
        "webapp",
        "web app",
        "landing page",
        "portfolio",
        "company profile",
        "profile page",
        "homepage",
        "frontend",
        "react",
        "next.js",
        "nextjs",
        "vue",
        "svelte",
        "astro",
        "html",
        "css",
        "dashboard",
        "situs",
        "halaman web",
        "aplikasi web",
        "buatkan web",
        "bikin web",
    )
    if any(marker in text for marker in markers):
        return True
    return bool(re.search(r"\bweb\b", text))


def _has_code_creation_evidence(steps: list[dict[str, Any]]) -> bool:
    direct_code_tools = {
        "write_file",
        "edit_file",
        "execute",
        "sandbox_write_binary_file",
    }
    code_markers = (
        "/workspace/src",
        "index.html",
        ".html",
        ".css",
        ".js",
        ".jsx",
        ".tsx",
        "package.json",
        "vite",
        "next",
        "react",
        "tailwind",
        "npm run build",
        "build berhasil",
        "file dibuat",
        "file berhasil",
        "berhasil dibuat",
        "sudah dibuat",
        "telah dibuat",
        "ditulis",
        "menulis file",
        "created",
        "wrote",
        "generated",
        "source code",
        "kode",
    )
    failure_markers = (
        "error",
        "failed",
        "gagal",
        "exception",
        "traceback",
        "not found",
    )
    for step in steps or []:
        tool_name = str(step.get("tool") or "")
        text = _step_text(step)
        lower = text.lower()
        if tool_name in direct_code_tools and not any(marker in lower for marker in failure_markers):
            return True
        if tool_name == "task" and any(marker in lower for marker in code_markers):
            return True
    return False


_BUILD_PROGRESS_TOOLS = frozenset(
    {
        "create_agent_from_brief",
        "plan_agent",
        "compose_agent_blueprint",
        "compose_agent_instructions",
        "compose_agent_soul",
    }
)


def _needs_builder_create_completion(
    steps: list[dict[str, Any]],
    *,
    is_builder: bool,
) -> bool:
    """Detect a build that planned/composed an agent but never reached create_agent.

    Arthur (on a small model) often stops after plan_agent — e.g. to ask about
    Google — and never chains through to create_agent, leaving the user with a
    confusing "belum berhasil" loop. When that happens with no real plan/
    entitlement block, the runtime continues the build once internally instead
    of bouncing it back to the user.
    """
    if not is_builder:
        return False
    tool_names = {str(step.get("tool", "")).strip() for step in (steps or [])}
    # Only the create flow (which always starts with plan_agent) is in scope.
    if "plan_agent" not in tool_names:
        return False
    if not (tool_names & _BUILD_PROGRESS_TOOLS):
        return False
    if "create_agent" in tool_names or "create_agent_from_brief" in tool_names or "update_agent" in tool_names:
        return False
    # A real entitlement BLOCK is not something to silently retry. Match the
    # actual block — NOT the word "entitlement", because plan_agent always emits
    # a `creation_entitlement_check` field even on success (allowed=true).
    for step in steps or []:
        result = step.get("result")
        parsed = _parse_step_result_json(result)
        if isinstance(parsed, dict):
            check = parsed.get("creation_entitlement_check")
            if isinstance(check, dict) and check.get("checked") and not check.get("allowed", True):
                return False
        if "melebihi entitlement" in str(result or "").lower():
            return False
    return True


def _builder_create_completion_directive() -> str:
    """Directive that pushes Arthur to finish the build through create_agent."""
    return (
        "LANJUTKAN PEMBUATAN AGENT SEKARANG SAMPAI SELESAI — JANGAN BERHENTI.\n"
        "Kamu sudah merencanakan/menyusun agent tapi belum memanggil create_agent. "
        "JANGAN bertanya konfirmasi lagi, JANGAN menawarkan Google lagi, JANGAN mengulang plan_agent. "
        "Langsung jalankan berurutan: compose_agent_blueprint (jika belum) -> compose_agent_instructions -> "
        "validate_agent_config -> create_agent, memakai konteks bisnis yang sudah ada. "
        "Kalau ada detail yang belum lengkap, pakai asumsi wajar dan tandai untuk direview nanti — "
        "jangan berhenti untuk bertanya. Setelah create_agent sukses, balas singkat dan natural bahwa agennya sudah jadi."
    )


def _needs_deploy_followup(
    user_message: str,
    tools_config: dict[str, Any],
    steps: list[dict[str, Any]],
    final_reply: str,
) -> bool:
    """Detect website/app work that stopped after coding without public deploy URL."""
    if not _is_enabled(tools_config, "deploy", default=False):
        return False
    if not _is_website_or_app_request(user_message):
        return False
    if _has_public_url_in_text(final_reply) or _has_public_url_in_steps(steps):
        return False
    return _has_code_creation_evidence(steps)


def _deploy_followup_message(final_reply: str, steps: list[dict[str, Any]], *, has_subagents: bool) -> str:
    tool_names = ", ".join(
        str(step.get("tool") or "?")
        for step in (steps or [])[-8:]
        if step.get("tool")
    )
    subagent_instruction = (
        "Jika file website dibuat di workspace sys_coder/subagent, panggil task() ke sys_coder dan instruksikan "
        "sys_coder untuk memanggil deploy_app() dari workspace-nya sendiri. Parent tidak boleh mencoba deploy "
        "workspace kosong yang berbeda."
        if has_subagents
        else "Panggil deploy_app() dari workspace sandbox yang berisi file website."
    )
    return (
        "LANJUTKAN TASK SEBELUMNYA: user meminta website/app dan agent ini memiliki deploy=true, "
        "tetapi percobaan sebelumnya belum mengembalikan URL public.\n\n"
        f"Ringkasan jawaban sebelumnya: {(final_reply or '').strip()[:1200]}\n"
        f"Tool terakhir: {tool_names or '-'}\n\n"
        "Wajib sekarang deploy hasil website/app dengan Cloudflare tunnel.\n"
        f"{subagent_instruction}\n"
        "Gunakan get_deployment_status() jika perlu, lalu deploy_app(command, port), lalu verifikasi status. "
        "Jangan berhenti pada menulis file/build. Jawaban akhir harus menyertakan URL https public dari deploy_app."
    )

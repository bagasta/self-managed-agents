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

_SHARED_WORKSPACE_FILE_RE = re.compile(r"(/workspace/shared/[^\s`'\"),]+)")


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
            path = match.rstrip(".,;:)")
            name = path.rsplit("/", 1)[-1]
            if name and "." in name:
                return path
    return None


def _extract_shared_workspace_file_from_steps(
    steps: list[dict[str, Any]],
    final_reply: str = "",
) -> str | None:
    values: list[Any] = [final_reply]
    values.extend(_step_text(step) for step in reversed(steps or []))
    return _extract_shared_workspace_file_path(*values)


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
    text = "\n".join([user_message or "", final_reply or ""] + [_step_text(step) for step in steps or []]).lower()
    markers = (
        "siap_dikirim_parent",
        "kirim file",
        "kirim filenya",
        "file-nya",
        "filenya",
        "kirim dokumen",
        "kirim gambar",
        "kirim foto",
        "pdf",
        "docx",
        "xlsx",
        "csv",
        "zip",
        "dokumen",
        "attachment",
        "lampiran",
    )
    return any(marker in text for marker in markers)


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
    path = _extract_shared_workspace_file_from_steps(steps, final_reply)
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
    entitlement block, the runtime continues the build internally instead of
    bouncing it back to the user.

    A plan that still needs clarification is deliberately excluded. Previously
    any ``plan_agent`` call was treated as build-ready, so the continuation told
    Arthur to invent missing details and create anyway. That turned a valid
    discovery question into the generic "kendala sistem" fallback.
    """
    if not is_builder:
        return False
    tool_names = {str(step.get("tool", "")).strip() for step in (steps or [])}
    # Only the create flow (which always starts with plan_agent) is in scope.
    if "plan_agent" not in tool_names:
        return False
    if not (tool_names & _BUILD_PROGRESS_TOOLS):
        return False
    if "create_agent" in tool_names or "update_agent" in tool_names:
        return False
    ready_plan_found = False
    # Only a structured, explicitly ready plan may enter auto-completion. A real
    # entitlement block or clarification response must be returned to the user.
    for step in steps or []:
        if str(step.get("tool", "")).strip() != "plan_agent":
            continue
        result = step.get("result")
        parsed = _parse_step_result_json(result)
        if isinstance(parsed, dict):
            plan_status = str(parsed.get("plan_status") or "").strip().lower()
            if plan_status != "ready":
                return False
            check = parsed.get("creation_entitlement_check")
            if isinstance(check, dict) and check.get("checked") and not check.get("allowed", True):
                return False
            ready_plan_found = True
        else:
            # Unstructured/legacy output is not enough evidence that discovery
            # and confirmation were completed.
            return False
        if "melebihi entitlement" in str(result or "").lower():
            return False
    return ready_plan_found


def _needs_builder_retryable_plan(
    steps: list[dict[str, Any]],
    *,
    is_builder: bool,
) -> bool:
    """Return True when the latest plan failed only on transient evidence I/O."""
    if not is_builder:
        return False
    for step in reversed(steps or []):
        if str(step.get("tool", "")).strip() != "plan_agent":
            continue
        parsed = _parse_step_result_json(step.get("result"))
        return bool(
            isinstance(parsed, dict)
            and parsed.get("retryable") is True
            and str(parsed.get("plan_status") or "").strip().lower()
            == "temporarily_unavailable"
        )
    return False


def _builder_retryable_plan_directive() -> str:
    return (
        "ULANGI plan_agent SEKARANG satu kali dengan argumen dan discovery_answers yang sama. "
        "Kegagalan sebelumnya hanya saat memverifikasi riwayat pesan. Jangan meminta user mengulang "
        "jawaban, jangan mengubah `_evidence`, jangan mengarang detail, dan jangan create_agent kecuali "
        "hasil plan_agent yang baru benar-benar berstatus ready."
    )


def _builder_create_completion_directive() -> str:
    """Directive that pushes Arthur to finish the build through create_agent."""
    return (
        "LANJUTKAN PEMBUATAN AGENT SEKARANG SAMPAI SELESAI — JANGAN BERHENTI.\n"
        "Kamu sudah merencanakan/menyusun agent tapi belum memanggil create_agent. "
        "plan_agent sudah berstatus ready dan kebutuhan user sudah dikonfirmasi. "
        "JANGAN bertanya konfirmasi lagi, JANGAN menawarkan Google lagi, JANGAN mengulang plan_agent. "
        "Langsung jalankan berurutan: compose_agent_blueprint (jika belum) -> compose_agent_instructions -> "
        "validate_agent_config -> create_agent, memakai konteks bisnis yang sudah ada. "
        "DILARANG menambah asumsi atau detail yang tidak pernah diberikan user; gunakan hanya discovery_answers "
        "yang sudah dikonfirmasi. Setelah create_agent sukses, balas singkat dan natural bahwa agennya sudah jadi."
    )


def _message_text(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _latest_agent_text(input_messages: list[Any] | None) -> str:
    for message in reversed(input_messages or []):
        role = str(
            getattr(message, "type", "")
            or getattr(message, "role", "")
            or (message.get("role") if isinstance(message, dict) else "")
        ).lower()
        if role in {"ai", "agent", "assistant"}:
            return _message_text(message)
    return ""


def _requested_builder_whatsapp_action(
    user_message: str,
    input_messages: list[Any] | None = None,
) -> str | None:
    """Return the concrete WhatsApp action explicitly selected by the user."""
    current = " ".join(str(user_message or "").casefold().split())
    dedicated_markers = (
        "nomor saya sendiri",
        "nomer saya sendiri",
        "nomor whatsapp saya",
        "nomer whatsapp saya",
        "nomor khusus",
        "pasang ke nomor",
        "kirim qr",
        "qr baru",
        "scan qr",
    )
    if any(marker in current for marker in dedicated_markers):
        return "dedicated_qr"

    trial_markers = (
        "nomor demo",
        "kode demo",
        "kode trial",
        "link trial",
        "trial link",
        "link coba",
        "mau coba agent",
        "coba agentnya",
        "cobain agent",
    )
    if any(marker in current for marker in trial_markers):
        return "trial_link"

    if current in {"iya", "iya mau", "mau", "ok", "oke", "lanjut"}:
        previous = _latest_agent_text(input_messages).casefold()
        if any(
            marker in previous
            for marker in (
                "mau link trial",
                "mau link coba",
                "mau nomor demo",
                "mau aku buatin link trial",
            )
        ):
            return "trial_link"
    return None


def _needs_builder_whatsapp_action_completion(
    action: str | None,
    steps: list[dict[str, Any]],
    *,
    is_builder: bool,
) -> bool:
    if not is_builder or action not in {"trial_link", "dedicated_qr"}:
        return False
    expected_tool = (
        "create_wa_dev_trial_link"
        if action == "trial_link"
        else "send_agent_wa_qr"
    )
    return not any(
        str(step.get("tool") or "").strip() == expected_tool
        for step in steps or []
    )


def _builder_whatsapp_action_directive(action: str) -> str:
    if action == "dedicated_qr":
        return (
            "USER SUDAH MEMILIH NOMOR WHATSAPP KHUSUS. Selesaikan sekarang di turn ini: "
            "temukan agent target yang benar dari konteks, lalu panggil send_agent_wa_qr. "
            "QR harus dikirim ke identitas owner sesi yang terverifikasi. Jangan arahkan user "
            "ke dashboard, jangan mengklaim QR terkirim sebelum hasil tool menyatakan QR_SENT."
        )
    return (
        "USER SUDAH MEMILIH NOMOR DEMO ARTHUR. Selesaikan sekarang di turn ini: "
        "temukan agent target yang benar dari konteks, lalu panggil create_wa_dev_trial_link "
        "dengan send_contact=true. Jawaban final wajib memuat link wa.me dan kode persis dari "
        "hasil tool. Jangan hanya menjelaskan cara mencoba dan jangan arahkan user ke dashboard."
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

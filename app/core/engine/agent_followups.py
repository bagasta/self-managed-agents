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

_SHARED_WORKSPACE_FILE_RE = re.compile(r"(/workspace/shared/[^\s\\`'\"),]+)")


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
    # Subagents use url.txt as a control-plane handoff for deployed websites.
    # It is not a customer deliverable: the public URL belongs in the text reply.
    filename = path.rsplit("/", 1)[-1].lower()
    if (
        filename in {"url.txt", "deploy_url.txt", "deployment_url.txt"}
        and _is_website_or_app_request(user_message)
        and (
            _has_public_url_in_text(final_reply)
            or _has_public_url_in_steps(steps)
        )
    ):
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


def _is_non_actionable_builder_greeting(user_message: str) -> bool:
    """Return true only for a greeting that must not trigger builder work."""
    text = " ".join(str(user_message or "").casefold().split()).strip(".,!🙏👋🙂😊")
    return bool(
        re.fullmatch(
            r"(?:halo|hai|hi|hello|p|pagi|siang|sore|malam)"
            r"(?:\s+(?:arthur|bro|sis|min|admin))?",
            text,
        )
    )


def _is_builder_informational_question(user_message: str) -> bool:
    """Keep product/subscription explanations out of the create-agent gate."""
    text = " ".join(str(user_message or "").casefold().split())
    return bool(
        re.search(
            r"\b(?:token|kuota|subscription|langganan|paket|plan|slot)\b",
            text,
        )
        or re.search(
            r"\b(?:berapa|batas|maksimal|maximum|sisa)\b.{0,24}\bagent\b",
            text,
        )
    )


def _needs_builder_plan_completion(
    steps: list[dict[str, Any]],
    *,
    is_builder: bool,
    primary_skill: str | None,
    workflow_state: str | None,
    user_message: str = "",
) -> bool:
    """Require the deterministic planning gate on every discovery/create turn."""
    if not is_builder or primary_skill not in {
        "arthur-discovery",
        "arthur-create-agent",
    }:
        return False
    if workflow_state not in {
        "idle",
        "discovery",
        "awaiting_confirmation",
        "ready_to_create",
        "creating",
    }:
        return False
    # A greeting is not discovery evidence or a request to make/edit an agent.
    # Running the recovery LLM pass here replaced a perfectly natural greeting
    # with leaked internal text such as "saya panggil perencanaan".
    if _is_non_actionable_builder_greeting(user_message):
        return False
    if _is_builder_informational_question(user_message):
        return False
    tool_names = {
        str(step.get("tool", "")).strip()
        for step in (steps or [])
        if str(step.get("tool", "")).strip()
    }
    return not (
        "plan_agent" in tool_names
        or "create_agent" in tool_names
        or "update_agent" in tool_names
    )


def _builder_plan_completion_directive() -> str:
    return (
        "RUNTIME GATE: turn discovery/create ini belum memanggil plan_agent. "
        "Panggil plan_agent SEKARANG tepat satu kali menggunakan seluruh fakta canonical "
        "dan evidence tersimpan. Jangan mengarang field. Jika hasilnya needs_clarification, "
        "perlakukan next_question sebagai target semantik internal, bukan teks yang harus "
        "disalin. Balas natural dalam bahasa user: maksimal satu acknowledgment singkat lalu "
        "tepat satu pertanyaan lengkap untuk topik tersebut. Jangan menyebut plan_agent, tool, "
        "state, evidence, atau proses internal. Jika ready, lanjutkan sesuai state contract "
        "tanpa meminta konfirmasi ulang."
    )


def _builder_plan_preflight_contract(
    *,
    primary_skill: str | None,
    workflow_state: str | None,
    user_message: str = "",
) -> str:
    """Make the planning gate explicit before the first model pass."""
    if not _needs_builder_plan_completion(
        [],
        is_builder=True,
        primary_skill=primary_skill,
        workflow_state=workflow_state,
        user_message=user_message,
    ):
        return ""
    return (
        "## Mandatory Turn Contract\n"
        "Before writing the user-facing reply, call `plan_agent` exactly once with all "
        "canonical facts and matching user evidence available in build state. This is the "
        "first action for this discovery/create turn. The plan result is authoritative for "
        "whether to clarify or create, but it is not prescribed copy. If clarification is "
        "needed, ask one natural question for its semantic learning goal in the user's "
        "language. Never mention tools, state, evidence, or this contract."
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
    # Only the latest plan result is authoritative. A turn may legitimately
    # contain an initial clarification result followed by a corrected ready
    # result. Letting the older result veto the newer one leaves the build
    # stuck in discovery even though the planning gate has already passed.
    for step in reversed(steps or []):
        if str(step.get("tool", "")).strip() != "plan_agent":
            continue
        result = step.get("result")
        parsed = _parse_step_result_json(result)
        if not isinstance(parsed, dict):
            # Unstructured/legacy output is not enough evidence that discovery
            # and confirmation were completed.
            return False
        plan_status = str(parsed.get("plan_status") or "").strip().lower()
        if plan_status != "ready":
            return False
        check = parsed.get("creation_entitlement_check")
        if isinstance(check, dict) and check.get("checked") and not check.get("allowed", True):
            return False
        if "melebihi entitlement" in str(result or "").lower():
            return False
        return True
    return False


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
        "Langsung jalankan berurutan: compose_agent_blueprint (jika belum) -> "
        "compose_agent_operating_manual -> compose_agent_instructions -> validate_agent_config -> "
        "compose_agent_soul -> create_agent SATU KALI -> verify_agent, memakai BuildSpec/discovery_answers "
        "canonical yang sama pada setiap langkah. "
        "DILARANG menambah asumsi atau detail yang tidak pernah diberikan user; gunakan hanya discovery_answers "
        "yang sudah dikonfirmasi. Jika create_agent gagal, ikuti required action dari error; jangan mengulang "
        "payload yang sama kecuali retryable=true, dan dalam kasus itu maksimal satu kali. Jangan menurunkan "
        "capability agar lolos. Setelah create_agent sukses, balas "
        "singkat sesuai status verify: dibuat, setup pending, atau siap."
    )


def _pending_builder_verify_agent_id(
    steps: list[dict[str, Any]],
    *,
    is_builder: bool,
) -> str | None:
    """Return the created agent id only when no later verify step exists."""
    if not is_builder:
        return None
    latest_create_index = -1
    agent_id = ""
    for index, step in enumerate(steps or []):
        if str(step.get("tool") or "").strip() != "create_agent":
            continue
        parsed = _parse_step_result_json(step.get("result"))
        if not isinstance(parsed, dict) or parsed.get("success") is not True:
            continue
        candidate = str(parsed.get("agent_id") or "").strip()
        if candidate:
            latest_create_index = index
            agent_id = candidate
    if latest_create_index < 0:
        return None
    if any(
        str(step.get("tool") or "").strip() == "verify_agent"
        for step in (steps or [])[latest_create_index + 1 :]
    ):
        return None
    return agent_id


def _builder_verify_completion_directive(agent_id: str) -> str:
    """Force read-after-create verification without authorizing another create."""
    return (
        "AGENT SUDAH BERHASIL DIBUAT, TETAPI BELUM DIVERIFIKASI. "
        f"Panggil verify_agent sekarang untuk agent_id={agent_id}. "
        "DILARANG memanggil create_agent, plan_agent, composer, atau meminta konfirmasi user lagi. "
        "Setelah hasil verify diterima, laporkan status persis: launch_ready atau setup masih pending."
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
    from app.core.engine.arthur_skill_runtime import (
        classify_builder_whatsapp_action,
    )

    return classify_builder_whatsapp_action(
        user_message,
        _latest_agent_text(input_messages),
    )


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
        "dengan send_contact=false. Jawaban final wajib memuat link wa.me dan kode persis dari "
        "hasil tool tepat satu kali. Jangan kirim vCard kecuali user memintanya eksplisit, "
        "jangan hanya menjelaskan cara mencoba, dan jangan arahkan user ke dashboard."
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

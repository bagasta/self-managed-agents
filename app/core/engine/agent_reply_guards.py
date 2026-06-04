"""Reply guards for task/escalation turns.

Extracted from agent_runner.py — PURE refactor, zero behaviour change.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.engine.agent_step_utils import (
    _URL_RE,
    _has_whatsapp_media_send_step,
    _is_operator_envelope,
    _operator_message_payload,
)
from app.core.engine.agent_whatsapp_guards import _has_reply_to_user_step, _has_send_to_number_step


def _task_result_guard_reply(final_reply: str, steps: list[dict[str, Any]], user_message: str) -> str:
    """Prevent parent agents from claiming subagent work succeeded when it did not."""
    if not steps:
        return final_reply

    task_results = [
        str(step.get("result") or "")
        for step in steps
        if step.get("tool") == "task" and step.get("result")
    ]
    if not task_results:
        return final_reply

    task_payload_text = "\n".join(
        json.dumps(step.get("args") or {}, ensure_ascii=False)
        for step in steps
        if step.get("tool") == "task"
    ).lower()
    user_lower = (user_message or "").lower()
    artifact_required = any(
        marker in f"{task_payload_text}\n{user_lower}"
        for marker in (
            "deploy",
            "trycloudflare",
            "website",
            "web app",
            "landing page",
            "html",
            "css",
            "javascript",
            "prototype",
            "portfolio",
            "portofolio",
            "aplikasi",
            "app web",
            "url",
            "link website",
            "file final",
            "dokumen final",
            "kirim file",
            "cv ats",
            "buat cv",
        )
    )

    combined = "\n".join(task_results)
    combined_lower = combined.lower()
    final_lower = (final_reply or "").lower()

    has_success_artifact = bool(
        _URL_RE.search(combined)
        or "[document_sent]" in combined_lower
        or "[image_sent]" in combined_lower
        or _has_whatsapp_media_send_step(steps)
        or " terkirim" in combined_lower
        or "deployment berhasil" in combined_lower
    )
    blocker_markers = (
        "belum menemukan",
        "belum menerima",
        "mohon bagikan",
        "tolong kirim",
        "perlu informasi",
        "butuh informasi",
        "tidak menemukan",
        "file cv",
        "isi cv",
    )
    has_blocker = any(marker in combined_lower for marker in blocker_markers)
    promise_markers = (
        "nanti",
        "sedang",
        "saya mulai",
        "saya langsung",
        "langsung buatkan",
        "akan saya",
        "hasilnya saya kirim",
        "lagi saya",
    )
    final_is_promise = any(marker in final_lower for marker in promise_markers)
    user_asks_status = any(k in user_lower for k in ("mana", "belum jadi", "udah jadi", "sudah jadi", "url", "link"))

    if not artifact_required:
        return final_reply
    if has_success_artifact:
        return final_reply
    if has_blocker:
        return (
            "Belum bisa saya lanjutkan karena bahan yang dibutuhkan belum tersedia di workspace agent. "
            "Subagent minta isi/file CV dikirim ulang atau ditempel di chat dulu, baru saya bisa buat web HTML/CSS/JS-nya."
        )
    if final_is_promise or user_asks_status:
        return (
            "Belum selesai. Subagent belum mengembalikan URL, file terkirim, atau hasil final yang bisa saya serahkan. "
            "Saya tidak akan klaim selesai sebelum ada output yang valid."
        )
    return final_reply


def _operator_escalation_reply_guard(
    final_reply: str,
    steps: list[dict[str, Any]],
    user_message: str,
    escalation_user_jid: str | None,
) -> str:
    """Block operator turns from hallucinating completed customer deliverables."""
    if not escalation_user_jid or not _is_operator_envelope(user_message):
        return final_reply
    if _has_reply_to_user_step(steps) or _has_send_to_number_step(steps):
        return final_reply

    text = (final_reply or "").strip()
    lowered = text.lower()
    if "draft" in lowered and ("ketik" in lowered or "sudah ok" in lowered):
        return final_reply

    deliverable_markers = (
        "cv",
        "file",
        "pdf",
        "dokumen",
        "document",
        "website",
        "web",
    )
    unsafe_completion_markers = (
        "sudah selesai",
        "selesai dibuat",
        "siap dikirim",
        "siap saya kirim",
        "berhasil dibuat",
        "akan saya kirim",
        "harus dilakukan secara manual",
    )
    if not (
        any(marker in lowered for marker in deliverable_markers)
        and any(marker in lowered for marker in unsafe_completion_markers)
    ):
        return final_reply

    operator_text = _operator_message_payload(user_message).lower()
    if any(marker in operator_text for marker in ("pembayaran", "transfer", "bayar", "payment", "paid", "valid", "approve")):
        return (
            "Draft pesan untuk customer:\n"
            "----\n"
            "Halo, pembayaran Anda sudah kami terima. Proses pembuatan CV akan kami lanjutkan, "
            "dan hasilnya akan kami kirimkan setelah siap.\n"
            "----\n"
            "Sudah OK? Ketik 'kirim' untuk saya teruskan ke customer."
        )

    return (
        "Saya belum mengirim atau membuat ulang deliverable dari sesi operator ini. "
        "Silakan tulis pesan yang ingin diteruskan ke customer, lalu ketik 'kirim' setelah draft-nya sudah OK."
    )

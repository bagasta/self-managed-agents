"""Input-message preparation for agent graph execution."""
from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from app.core.engine.context_service import db_messages_to_lc
from app.core.engine.result_parser import sanitize_input_messages


MAX_PRIOR_MESSAGES = 30


def build_input_messages(
    *,
    prior_messages: list[BaseMessage],
    history_rows: list[Any],
    human_content: Any,
    log: Any,
    current_attachment_name: str | None = None,
    current_attachment: dict[str, Any] | None = None,
) -> list[BaseMessage]:
    sanitized_prior = sanitize_input_messages(prior_messages)
    if len(sanitized_prior) != len(prior_messages):
        log.warning(
            "agent_run.sanitized_prior_messages",
            original=len(prior_messages),
            sanitized=len(sanitized_prior),
        )

    if len(sanitized_prior) > MAX_PRIOR_MESSAGES:
        log.debug(
            "agent_run.history_trimmed",
            original=len(sanitized_prior),
            trimmed=MAX_PRIOR_MESSAGES,
        )
        sanitized_prior = sanitized_prior[-MAX_PRIOR_MESSAGES:]

    interrupt_note: list[BaseMessage] = []
    tail_dirty = False
    if history_rows:
        idx_last_user = -1
        for idx in range(len(history_rows) - 1, -1, -1):
            if history_rows[idx].role == "user":
                idx_last_user = idx
                break

        has_final_ai_reply_after_user = False
        if idx_last_user >= 0:
            for row in history_rows[idx_last_user + 1:]:
                if row.role == "agent" and row.content and not str(row.content).startswith("[tool_call]"):
                    has_final_ai_reply_after_user = True
                    break

        tail_dirty = idx_last_user >= 0 and not has_final_ai_reply_after_user
        if tail_dirty:
            kept_rows = history_rows[: idx_last_user + 1] if idx_last_user >= 0 else []
            for row in history_rows[idx_last_user + 1:]:
                kept_rows.append(row)
                if row.role == "agent" and row.content and not str(row.content).startswith("[tool_call]"):
                    break
            if len(kept_rows) < len(history_rows):
                sanitized_prior = sanitize_input_messages(db_messages_to_lc(kept_rows))
                log.info(
                    "agent_run.stripped_dirty_tail",
                    stripped=len(history_rows) - len(kept_rows),
                    tail_dirty=True,
                )

    if tail_dirty:
        interrupt_note = [SystemMessage(content=(
            "[SYSTEM — HARD OVERRIDE] Pesan baru dari user di bawah ini adalah PRIORITAS TUNGGAL. "
            "Task sebelumnya (jika ada) sudah dibatalkan. JANGAN melanjutkan, mengulang, atau "
            "menyebut pekerjaan lama kecuali user eksplisit bertanya tentangnya. "
            "Respon HANYA terhadap pesan user terbaru. "
            "Kalau user cuma sapa ('Halo', 'hai', 'bro'), balas sapaan singkat — JANGAN otomatis "
            "lanjut delegasi atau tool call apapun yang berkaitan dengan task lama."
        ))]

    attachment_note: list[BaseMessage] = []
    if current_attachment_name:
        current_attachment = current_attachment if isinstance(current_attachment, dict) else {}
        input_path = str(current_attachment.get("input_path") or "").strip()
        subagent_input_path = str(current_attachment.get("subagent_input_path") or "").strip()
        extracted_text_path = str(current_attachment.get("extracted_text_path") or "").strip()
        extracted_text_subagent_path = str(current_attachment.get("extracted_text_subagent_path") or "").strip()
        path_note = ""
        if input_path or subagent_input_path or extracted_text_path or extracted_text_subagent_path:
            path_note = (
                f" Parent path: {input_path or '-'}."
                f" Subagent path: {subagent_input_path or '-'}."
                f" Extracted text parent path: {extracted_text_path or '-'}."
                f" Extracted text subagent path: {extracted_text_subagent_path or '-'}."
            )
        attachment_note = [SystemMessage(content=(
            f"[SISTEM — LAMPIRAN AKTIF] File yang BARU dikirim user di turn ini: "
            f"'{current_attachment_name}'. Ini SATU-SATUNYA sumber data/berkas untuk "
            f"permintaan saat ini. JANGAN memakai, membaca, atau merujuk file/lampiran "
            f"dari turn sebelumnya kecuali user secara eksplisit menyebut nama file lama itu."
            f"{path_note}"
        ))]

    return sanitized_prior + interrupt_note + attachment_note + [HumanMessage(content=human_content)]

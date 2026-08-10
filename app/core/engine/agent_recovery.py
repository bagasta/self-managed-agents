"""User-facing recovery notification helpers."""
from __future__ import annotations

from typing import Any


async def send_agent_recovery_message(
    *,
    is_wa_session: bool,
    wa_device_id: str,
    wa_target: str,
    llm_raw: Any,
    system_prompt: Any,
    reason: str,
    log: Any,
) -> None:
    if not is_wa_session:
        return
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        recovery_llm = llm_raw.bind(max_tokens=200)
        response = await recovery_llm.ainvoke([
            SystemMessage(content=system_prompt if isinstance(system_prompt, str) else ""),
            HumanMessage(content=(
                f"[INTERNAL] Task kamu barusan {reason}. "
                "Kirim pesan singkat dan natural ke user untuk memberi tahu bahwa prosesnya terganggu. "
                "Tanyakan apakah mereka ingin melanjutkan atau mencoba lagi. "
                "Jangan gunakan format robotik. Tulis langsung pesan untuk user, tidak lebih dari 2 kalimat."
            )),
        ])
        msg = getattr(response, "content", "") or ""
        if msg.strip():
            from app.core.infra.wa_client import send_wa_message

            await send_wa_message(wa_device_id, wa_target, msg.strip())
    except Exception as err:
        log.warning("agent_run.recovery_message_failed", error=str(err))

"""
context_service.py — Layanan pengambilan & konversi pesan dari DB ke format LangChain.

Dipecah dari agent_runner.py (item 2.1 production plan).

Fungsi yang diekspor:
  load_history(session_id, db, max_turns)
  count_user_messages(session_id, db)
  db_messages_to_lc(db_messages)
"""
from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.run import Run


# Agent rows carrying this marker in `tool_name` are transient delivery/status
# notices (e.g. "Gagal mengirim ..."), not dialogue. They must never be replayed
# as conversation history or the model treats its own past failure as a live fact.
DELIVERY_STATUS_TAG = "__delivery_status__"
INVALID_TOOL_CLAIM_TAG = "__invalid_tool_claim__"
NON_DIALOGUE_AGENT_TAGS = {DELIVERY_STATUS_TAG, INVALID_TOOL_CLAIM_TAG}

# Runs in these terminal-failure states contributed no valid turn; both their
# user and agent rows are dropped from re-injected history.
DEAD_RUN_STATUSES = {"abandoned", "cancelled", "timed_out", "failed"}

_TOOL_FAILURE_MARKERS = (
    "[error]",
    "[tool_error]",
    "error calling tool",
    "api error in ",
    "wrong_google_service",
    "media_source_unavailable",
)

# Header that precedes an inlined attachment body in a user message.
_ATTACHMENT_BODY_MARKER = "Isi dokumen:"


def filter_dead_run_messages(
    rows: list[Message],
    run_status: dict,
) -> list[Message]:
    """Drop rows belonging to dead runs, with one exception for cancelled runs.

    - failed / timed_out / abandoned: drop both user and agent rows — the run
      produced nothing useful and should be invisible to the model.
    - cancelled (human interrupt): keep the user row so the model can see what
      the user asked even though no response was delivered.  The agent row is
      still dropped (it may be partial or empty).  This prevents context loss
      when a user sends a follow-up before the previous run finishes.
    """
    result = []
    for row in rows:
        if row.role not in ("user", "agent") or not row.run_id:
            result.append(row)
            continue
        status = run_status.get(row.run_id)
        if status not in DEAD_RUN_STATUSES:
            result.append(row)
            continue
        # Cancelled run: preserve the user's message; discard the agent's partial reply.
        if status == "cancelled" and row.role == "user":
            result.append(row)
    return result


def tool_result_indicates_failure(result: str | None) -> bool:
    lowered = str(result or "").lower()
    return any(marker in lowered for marker in _TOOL_FAILURE_MARKERS)


def filter_failed_tool_run_agent_messages(
    rows: list[Message],
    failed_tool_run_ids: set[uuid.UUID],
) -> list[Message]:
    """Keep the user's request but drop agent claims produced after tool failure."""
    if not failed_tool_run_ids:
        return rows
    return [
        row
        for row in rows
        if not (
            row.role == "agent"
            and row.run_id in failed_tool_run_ids
        )
    ]


def _elide_stale_attachment_body(content: str) -> str:
    """Strip the inlined document body from a historical attachment message.

    The header ("[Dokumen diterima: <name> ...]") is kept so the model still knows
    a file was shared earlier, but the heavy body is removed to stop a previous
    upload's content from bleeding into the current turn as a competing source.
    """
    idx = content.find(_ATTACHMENT_BODY_MARKER)
    if idx == -1:
        return content
    return (
        content[:idx].rstrip()
        + "\n[isi lampiran turn sebelumnya disembunyikan — bukan lampiran aktif]"
    )


async def load_history(
    session_id: uuid.UUID,
    db: AsyncSession,
    max_turns: int | None = None,
) -> list[Message]:
    """
    Load pesan dari DB untuk session_id.

    Args:
        max_turns: Jika di-set, hanya load N turn terakhir (1 turn = 1 user + 1 agent).
                   None = load semua pesan.
    """
    if max_turns is not None:
        sub = (
            select(Message.id)
            .where(
                Message.session_id == session_id,
                Message.role.in_(["user", "agent"]),
            )
            .order_by(Message.step_index.desc(), Message.timestamp.desc())
            .limit(max_turns * 2)
            .subquery()
        )
        stmt = (
            select(Message)
            .where(Message.id.in_(select(sub.c.id)))
            .order_by(Message.step_index, Message.timestamp)
        )
    else:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.step_index, Message.timestamp)
        )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    if not rows:
        return rows

    run_ids = {row.run_id for row in rows if row.run_id}
    if not run_ids:
        return rows
    run_result = await db.execute(select(Run.id, Run.status).where(Run.id.in_(run_ids)))
    run_status = dict(run_result.all())
    rows = filter_dead_run_messages(rows, run_status)

    tool_result = await db.execute(
        select(Message.run_id, Message.tool_result).where(
            Message.run_id.in_(run_ids),
            Message.role == "tool",
        )
    )
    failed_tool_run_ids = {
        run_id
        for run_id, result in tool_result.all()
        if run_id and tool_result_indicates_failure(result)
    }
    return filter_failed_tool_run_agent_messages(rows, failed_tool_run_ids)


async def count_user_messages(session_id: uuid.UUID, db: AsyncSession) -> int:
    """Hitung total pesan dari role 'user' dalam session."""
    result = await db.execute(
        select(func.count()).where(
            Message.session_id == session_id,
            Message.role == "user",
        )
    )
    return result.scalar_one()


def db_messages_to_lc(db_messages: list[Message]) -> list[BaseMessage]:
    """Konversi Message ORM rows ke list LangChain BaseMessage untuk history injection.

    Menjamin urutan human→ai→human→ai yang valid untuk semua LLM provider.
    Jika ada dua HumanMessage berturut-turut (terjadi saat agent tidak menghasilkan
    reply di run sebelumnya), pesan digabung jadi satu HumanMessage.
    """
    result: list[BaseMessage] = []
    for msg in db_messages:
        if msg.role == "user" and msg.content:
            content = _elide_stale_attachment_body(msg.content)
            # Gabung jika pesan terakhir juga HumanMessage (hindari double-human)
            if result and isinstance(result[-1], HumanMessage):
                prev = result[-1]
                result[-1] = HumanMessage(content=f"{prev.content}\n{content}")
            else:
                result.append(HumanMessage(content=content))
        elif msg.role == "agent" and msg.content:
            # Transient delivery/status notices are not dialogue — never replay them
            # as history (prevents stale "Gagal mengirim ..." from re-surfacing).
            if getattr(msg, "tool_name", None) in NON_DIALOGUE_AGENT_TAGS:
                continue
            result.append(AIMessage(content=msg.content))
    return result

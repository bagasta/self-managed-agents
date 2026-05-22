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
    return [
        row
        for row in rows
        if not (
            row.role == "user"
            and row.run_id
            and run_status.get(row.run_id) in {"abandoned", "cancelled", "timed_out", "failed"}
        )
    ]


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
            lc_msg = HumanMessage(content=msg.content)
            # Gabung jika pesan terakhir juga HumanMessage (hindari double-human)
            if result and isinstance(result[-1], HumanMessage):
                prev = result[-1]
                result[-1] = HumanMessage(content=f"{prev.content}\n{msg.content}")
            else:
                result.append(lc_msg)
        elif msg.role == "agent" and msg.content:
            result.append(AIMessage(content=msg.content))
    return result

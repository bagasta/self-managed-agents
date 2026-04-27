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
    return list(result.scalars().all())


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
    """Konversi Message ORM rows ke list LangChain BaseMessage untuk history injection."""
    result: list[BaseMessage] = []
    for msg in db_messages:
        if msg.role == "user" and msg.content:
            result.append(HumanMessage(content=msg.content))
        elif msg.role == "agent" and msg.content:
            result.append(AIMessage(content=msg.content))
    return result

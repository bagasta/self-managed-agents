"""
Memory service: CRUD for agent long-term memory (key-value store).
Used both by API endpoints and by the memory tools injected into the agent.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory


async def upsert_memory(
    agent_id: uuid.UUID,
    key: str,
    value: str,
    db: AsyncSession,
) -> Memory:
    """Insert or update a memory entry (upsert on unique agent_id+key)."""
    stmt = (
        pg_insert(Memory)
        .values(
            id=uuid.uuid4(),
            agent_id=agent_id,
            key=key,
            value_data=value,
        )
        .on_conflict_do_update(
            constraint="uq_agent_memory_key",
            set_={"value_data": value},
        )
        .returning(Memory)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


async def get_memory(
    agent_id: uuid.UUID,
    key: str,
    db: AsyncSession,
) -> Memory | None:
    stmt = select(Memory).where(Memory.agent_id == agent_id, Memory.key == key)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_memories(agent_id: uuid.UUID, db: AsyncSession) -> list[Memory]:
    stmt = select(Memory).where(Memory.agent_id == agent_id).order_by(Memory.key)
    return list((await db.execute(stmt)).scalars().all())


async def delete_memory(
    agent_id: uuid.UUID, key: str, db: AsyncSession
) -> bool:
    stmt = delete(Memory).where(Memory.agent_id == agent_id, Memory.key == key)
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0


async def build_memory_context(agent_id: uuid.UUID, db: AsyncSession) -> str:
    """Return a compact markdown block of all memories to inject into system prompt."""
    memories = await list_memories(agent_id, db)
    if not memories:
        return ""
    lines = ["## Long-Term Memory", ""]
    for m in memories:
        lines.append(f"- **{m.key}**: {m.value_data}")
    return "\n".join(lines)

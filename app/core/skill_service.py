"""
Skill service: CRUD for agent skills (reusable instruction/prompt blocks).
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill


async def create_or_update_skill(
    agent_id: uuid.UUID,
    name: str,
    description: str,
    content_md: str,
    db: AsyncSession,
) -> Skill:
    stmt = (
        pg_insert(Skill)
        .values(
            id=uuid.uuid4(),
            agent_id=agent_id,
            name=name,
            description=description,
            content_md=content_md,
        )
        .on_conflict_do_update(
            constraint="uq_agent_skill_name",
            set_={"description": description, "content_md": content_md},
        )
        .returning(Skill)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


async def get_skill(
    agent_id: uuid.UUID, name: str, db: AsyncSession
) -> Skill | None:
    stmt = select(Skill).where(Skill.agent_id == agent_id, Skill.name == name)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_skills(agent_id: uuid.UUID, db: AsyncSession) -> list[Skill]:
    stmt = select(Skill).where(Skill.agent_id == agent_id).order_by(Skill.name)
    return list((await db.execute(stmt)).scalars().all())


async def delete_skill(
    agent_id: uuid.UUID, name: str, db: AsyncSession
) -> bool:
    stmt = delete(Skill).where(Skill.agent_id == agent_id, Skill.name == name)
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0

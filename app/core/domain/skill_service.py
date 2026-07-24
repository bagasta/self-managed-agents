"""
Skill service: CRUD for agent skills (reusable instruction/prompt blocks).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
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
            version="user",
            trust_level="user",
            enabled=True,
            immutable=False,
            checksum=hashlib.sha256(content_md.encode("utf-8")).hexdigest(),
        )
        .on_conflict_do_update(
            constraint="uq_agent_skill_name_version",
            set_={
                "description": description,
                "content_md": content_md,
                "enabled": True,
                "checksum": hashlib.sha256(content_md.encode("utf-8")).hexdigest(),
            },
        )
        .returning(Skill)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


async def get_skill(
    agent_id: uuid.UUID, name: str, db: AsyncSession
) -> Skill | None:
    stmt = (
        select(Skill)
        .where(Skill.agent_id == agent_id, Skill.name == name, Skill.enabled.is_(True))
        .order_by(Skill.published_at.desc().nullslast(), Skill.updated_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_skills(agent_id: uuid.UUID, db: AsyncSession) -> list[Skill]:
    stmt = (
        select(Skill)
        .where(Skill.agent_id == agent_id, Skill.enabled.is_(True))
        .order_by(Skill.name, Skill.published_at.desc().nullslast())
    )
    return list((await db.execute(stmt)).scalars().all())


async def delete_skill(
    agent_id: uuid.UUID, name: str, db: AsyncSession
) -> bool:
    stmt = delete(Skill).where(
        Skill.agent_id == agent_id,
        Skill.name == name,
        Skill.immutable.is_(False),
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0


async def publish_system_skill(
    *,
    agent_id: uuid.UUID,
    name: str,
    description: str,
    content_md: str,
    version: str,
    triggers: list[str],
    supported_states: list[str],
    allowed_tool_groups: list[str],
    bundle_version: str,
    publisher: str,
    db: AsyncSession,
) -> Skill:
    """Publish one immutable system-skill version and atomically activate it."""
    checksum = hashlib.sha256(content_md.encode("utf-8")).hexdigest()
    existing = (
        await db.execute(
            select(Skill).where(
                Skill.agent_id == agent_id,
                Skill.name == name,
                Skill.version == version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.checksum and existing.checksum != checksum:
            raise ValueError(
                f"Immutable system skill {name}@{version} has a different checksum"
            )
        existing.enabled = True
        await db.execute(
            update(Skill)
            .where(
                Skill.agent_id == agent_id,
                Skill.name == name,
                Skill.id != existing.id,
                Skill.trust_level == "system",
            )
            .values(enabled=False)
        )
        await db.flush()
        return existing

    await db.execute(
        update(Skill)
        .where(
            Skill.agent_id == agent_id,
            Skill.name == name,
            Skill.trust_level == "system",
        )
        .values(enabled=False)
    )
    skill = Skill(
        agent_id=agent_id,
        name=name,
        description=description,
        content_md=content_md,
        version=version,
        triggers=list(triggers),
        supported_states=list(supported_states),
        allowed_tool_groups=list(allowed_tool_groups),
        checksum=checksum,
        enabled=True,
        trust_level="system",
        bundle_version=bundle_version,
        immutable=True,
        publisher=publisher,
        published_at=datetime.now(timezone.utc),
    )
    db.add(skill)
    await db.flush()
    return skill


async def list_active_system_skills(
    agent_id: uuid.UUID,
    db: AsyncSession,
    *,
    names: list[str] | None = None,
) -> list[Skill]:
    stmt = select(Skill).where(
        Skill.agent_id == agent_id,
        Skill.trust_level == "system",
        Skill.enabled.is_(True),
    )
    if names:
        stmt = stmt.where(Skill.name.in_(names))
    stmt = stmt.order_by(Skill.name, Skill.published_at.desc().nullslast())
    return list((await db.execute(stmt)).scalars().all())

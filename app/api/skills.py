"""
API endpoints for agent skill management.

  GET    /v1/agents/{agent_id}/skills          — list all skills
  POST   /v1/agents/{agent_id}/skills          — create/update a skill
  GET    /v1/agents/{agent_id}/skills/{name}   — get full skill content
  DELETE /v1/agents/{agent_id}/skills/{name}   — delete a skill
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.skill_service import (
    create_or_update_skill,
    delete_skill,
    get_skill,
    list_skills,
)
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.m2 import SkillCreate, SkillResponse

router = APIRouter(prefix="/v1/agents", tags=["skills"])


async def _get_agent_or_404(agent_id: uuid.UUID, db: AsyncSession) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id} not found")
    return agent


@router.get("/{agent_id}/skills", response_model=list[SkillResponse])
async def list_agent_skills(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[SkillResponse]:
    await _get_agent_or_404(agent_id, db)
    skills = await list_skills(agent_id, db)
    return [SkillResponse.model_validate(s) for s in skills]


@router.post("/{agent_id}/skills", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_agent_skill(
    agent_id: uuid.UUID,
    payload: SkillCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SkillResponse:
    await _get_agent_or_404(agent_id, db)
    skill = await create_or_update_skill(agent_id, payload.name, payload.description, payload.content_md, db)
    await db.commit()
    return SkillResponse.model_validate(skill)


@router.get("/{agent_id}/skills/{name}", response_model=SkillResponse)
async def get_agent_skill(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SkillResponse:
    await _get_agent_or_404(agent_id, db)
    skill = await get_skill(agent_id, name, db)
    if not skill:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found")
    return SkillResponse.model_validate(skill)


@router.delete("/{agent_id}/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_skill(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> None:
    await _get_agent_or_404(agent_id, db)
    deleted = await delete_skill(agent_id, name, db)
    await db.commit()
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found")

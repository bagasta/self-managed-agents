"""
API endpoints for agent memory management.

  GET    /v1/agents/{agent_id}/memory          — list all memories
  POST   /v1/agents/{agent_id}/memory          — upsert a memory entry
  DELETE /v1/agents/{agent_id}/memory/{key}    — delete a memory entry
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.memory_service import upsert_memory, list_memories, delete_memory
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.m2 import MemoryCreate, MemoryResponse

router = APIRouter(prefix="/v1/agents", tags=["memory"])


async def _get_agent_or_404(agent_id: uuid.UUID, db: AsyncSession) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id} not found")
    return agent


@router.get("/{agent_id}/memory", response_model=list[MemoryResponse])
async def list_agent_memories(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[MemoryResponse]:
    await _get_agent_or_404(agent_id, db)
    memories = await list_memories(agent_id, db)
    return [MemoryResponse.model_validate(m) for m in memories]


@router.post("/{agent_id}/memory", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def upsert_agent_memory(
    agent_id: uuid.UUID,
    payload: MemoryCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> MemoryResponse:
    await _get_agent_or_404(agent_id, db)
    mem = await upsert_memory(agent_id, payload.key, payload.value, db)
    await db.commit()
    return MemoryResponse.model_validate(mem)


@router.delete("/{agent_id}/memory/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_memory(
    agent_id: uuid.UUID,
    key: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> None:
    await _get_agent_or_404(agent_id, db)
    deleted = await delete_memory(agent_id, key, db)
    await db.commit()
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory key '{key}' not found")

"""
API endpoints for agent custom tool management.

  GET    /v1/agents/{agent_id}/custom-tools           — list all custom tools
  POST   /v1/agents/{agent_id}/custom-tools           — create/update a custom tool
  GET    /v1/agents/{agent_id}/custom-tools/{name}    — get tool detail
  DELETE /v1/agents/{agent_id}/custom-tools/{name}    — delete a tool
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_tool_service import (
    create_or_update_custom_tool,
    delete_custom_tool,
    get_custom_tool,
    list_custom_tools,
)
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.internal import CustomToolCreate, CustomToolResponse

router = APIRouter(prefix="/v1/agents", tags=["custom-tools"])


async def _get_agent_or_404(agent_id: uuid.UUID, db: AsyncSession) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent {agent_id} not found")
    return agent


@router.get("/{agent_id}/custom-tools", response_model=list[CustomToolResponse])
async def list_agent_custom_tools(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[CustomToolResponse]:
    await _get_agent_or_404(agent_id, db)
    tools = await list_custom_tools(agent_id, db)
    return [CustomToolResponse.model_validate(t) for t in tools]


@router.post(
    "/{agent_id}/custom-tools",
    response_model=CustomToolResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_custom_tool(
    agent_id: uuid.UUID,
    payload: CustomToolCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> CustomToolResponse:
    await _get_agent_or_404(agent_id, db)
    ct, err = await create_or_update_custom_tool(
        agent_id, payload.name, payload.description, payload.code, db
    )
    if err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Python syntax error: {err}",
        )
    await db.commit()
    return CustomToolResponse.model_validate(ct)


@router.get("/{agent_id}/custom-tools/{name}", response_model=CustomToolResponse)
async def get_agent_custom_tool(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> CustomToolResponse:
    await _get_agent_or_404(agent_id, db)
    ct = await get_custom_tool(agent_id, name, db)
    if not ct:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Custom tool '{name}' not found")
    return CustomToolResponse.model_validate(ct)


@router.delete("/{agent_id}/custom-tools/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_custom_tool(
    agent_id: uuid.UUID,
    name: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> None:
    await _get_agent_or_404(agent_id, db)
    deleted = await delete_custom_tool(agent_id, name, db)
    await db.commit()
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Custom tool '{name}' not found"
        )

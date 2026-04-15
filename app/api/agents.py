import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.agent import AgentCreate, AgentListResponse, AgentResponse, AgentUpdate

router = APIRouter(prefix="/v1/agents", tags=["agents"])


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentResponse:
    agent = Agent(
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        model=payload.model,
        tools_config=payload.tools_config,
        sandbox_config=payload.sandbox_config,
        safety_policy=payload.safety_policy,
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    return AgentResponse.model_validate(agent)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentListResponse:
    total = (
        await db.execute(
            select(func.count()).select_from(Agent).where(Agent.is_deleted.is_(False))
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(Agent)
            .where(Agent.is_deleted.is_(False))
            .order_by(Agent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    return AgentListResponse(
        items=[AgentResponse.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentResponse:
    return AgentResponse.model_validate(await _get_active_agent(agent_id, db))


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentResponse:
    agent = await _get_active_agent(agent_id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(agent, field, value)
    agent.version += 1
    await db.flush()
    await db.refresh(agent)
    return AgentResponse.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> None:
    agent = await _get_active_agent(agent_id, db)
    agent.is_deleted = True
    await db.flush()


async def _get_active_agent(agent_id: uuid.UUID, db: AsyncSession) -> Agent:
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )
    return agent

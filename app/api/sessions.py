import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.models.session import Session
from app.schemas.session import SessionCreate, SessionResponse

router = APIRouter(prefix="/v1/agents", tags=["sessions"])


@router.post(
    "/{agent_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    agent_id: uuid.UUID,
    payload: SessionCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SessionResponse:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id} not found",
        )

    from app.core.channel_service import encrypt_channel_config
    encrypted_channel_config = encrypt_channel_config(payload.channel_config) if payload.channel_config else {}

    session = Session(
        agent_id=agent_id,
        external_user_id=payload.external_user_id,
        metadata_=payload.metadata,
        channel_type=payload.channel_type,
        channel_config=encrypted_channel_config,
    )
    db.add(session)
    await db.flush()

    # Pre-create the persistent workspace directory for this session
    settings = get_settings()
    workspace = Path(settings.sandbox_base_dir) / str(session.id)
    workspace.mkdir(parents=True, exist_ok=True)
    session.workspace_dir = str(workspace)

    await db.flush()
    await db.refresh(session)
    return SessionResponse.model_validate(session)


@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionResponse, tags=["sessions"])
async def get_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SessionResponse:
    session = (
        await db.execute(select(Session).where(Session.id == session_id, Session.agent_id == agent_id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return SessionResponse.model_validate(session)


@router.patch("/{agent_id}/sessions/{session_id}", response_model=SessionResponse, tags=["sessions"])
async def patch_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SessionResponse:
    """Update session properties. Saat ini support: escalation_active (bool)."""
    from fastapi import Body
    session = (
        await db.execute(select(Session).where(Session.id == session_id, Session.agent_id == agent_id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if "escalation_active" in payload:
        session.escalation_active = bool(payload["escalation_active"])
    await db.flush()
    await db.refresh(session)
    return SessionResponse.model_validate(session)

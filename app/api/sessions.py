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
from app.models.subscription import SubscriptionPlan, User, UserSubscription
from app.schemas.session import SessionCreate, SessionResponse

router = APIRouter(prefix="/v1/agents", tags=["sessions"])

_ARTHUR_UI_ENTERPRISE_TEST_ID = "clevio-arthur-ui-enterprise-test"


async def _ensure_arthur_ui_enterprise_test_owner(db: AsyncSession) -> None:
    """Provision only the reserved local UI-test principal with Enterprise capacity."""
    from app.config import get_settings
    from app.core.domain.subscription_service import ensure_default_subscription_plans

    if get_settings().environment.lower() not in {"development", "dev", "test"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="UI test plan is disabled outside development.")

    await ensure_default_subscription_plans(db)
    user = (
        await db.execute(select(User).where(User.external_id == _ARTHUR_UI_ENTERPRISE_TEST_ID))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email="arthur-ui-enterprise-test@local.invalid",
            password_hash="",
            full_name="Arthur UI Enterprise Test",
            external_id=_ARTHUR_UI_ENTERPRISE_TEST_ID,
            has_used_trial=True,
            email_verified=False,
        )
        db.add(user)
        await db.flush()

    plan = (
        await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == SubscriptionPlan.TIER_3_ID)
        )
    ).scalar_one()
    subscription = (
        await db.execute(select(UserSubscription).where(UserSubscription.user_id == user.id))
    ).scalar_one_or_none()
    if subscription is None:
        subscription = UserSubscription(
            user_id=user.id,
            plan_id=plan.id,
            status="active",
            token_quota=plan.token_quota,
            tokens_used=0,
            expires_at=None,
            grace_until=None,
        )
        db.add(subscription)
    else:
        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.token_quota = plan.token_quota
        subscription.expires_at = None
        subscription.grace_until = None
    await db.flush()


def _is_arthur_ui_enterprise_test(payload: SessionCreate) -> bool:
    metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
    return (
        payload.channel_type == "api"
        and payload.external_user_id == _ARTHUR_UI_ENTERPRISE_TEST_ID
        and metadata.get("source") == "arthur-ui"
        and metadata.get("memory_mode") == "isolated"
        and metadata.get("test_plan") == "enterprise"
    )


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
    if _is_arthur_ui_enterprise_test(payload):
        await _ensure_arthur_ui_enterprise_test_owner(db)

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

    from app.core.infra.channel_service import encrypt_channel_config
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


@router.get("/{agent_id}/sessions", tags=["sessions"])
async def list_sessions(
    agent_id: uuid.UUID,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict:
    from sqlalchemy import desc
    rows = (
        await db.execute(
            select(Session)
            .where(Session.agent_id == agent_id)
            .order_by(desc(Session.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return {"items": [SessionResponse.model_validate(s) for s in rows]}


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

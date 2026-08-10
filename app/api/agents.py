import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config_schema import ToolsConfig
from app.core.domain.agent_ownership import owner_filter
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.agent import (
    AgentCreate,
    AgentListResponse,
    AgentRenewResponse,
    AgentResponse,
    AgentUpdate,
    AgentWhatsAppQRResponse,
    AgentWhatsAppStatusResponse,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _validate_tools_config(tools_config: dict[str, Any] | None) -> None:
    if not tools_config:
        return
    try:
        ToolsConfig.model_validate(tools_config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentResponse:
    _validate_tools_config(payload.tools_config)
    active_until = datetime.now(timezone.utc) + timedelta(days=payload.quota_period_days)
    device_id = str(uuid.uuid4()) if payload.channel_type == "whatsapp" else None
    agent = Agent(
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        tools_config=payload.tools_config,
        sandbox_config=payload.sandbox_config,
        safety_policy=payload.safety_policy,
        escalation_config=payload.escalation_config,
        operator_ids=payload.operator_ids,
        allowed_senders=payload.allowed_senders,
        token_quota=payload.token_quota,
        quota_period_days=payload.quota_period_days,
        active_until=active_until,
        channel_type=payload.channel_type,
        wa_connection_type="cloud_api" if payload.channel_type == "whatsapp" else None,
        owner_external_id=payload.owner_external_id,
        created_by_type=payload.created_by_type or "api",
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)

    return AgentResponse.model_validate(agent)


@router.get("", response_model=AgentListResponse)
async def list_agents(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    owner_external_id: str | None = Query(None, max_length=64),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentListResponse:
    filters = [Agent.is_deleted.is_(False)]
    if owner_external_id:
        filters.append(owner_filter(owner_external_id))

    total = (
        await db.execute(
            select(func.count()).select_from(Agent).where(*filters)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(Agent)
            .where(*filters)
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


@router.post("/system/arthur-v2/ensure", response_model=AgentResponse)
async def ensure_arthur_v2(
    _: str = Depends(verify_api_key),
) -> AgentResponse:
    """Create or refresh only the independent Arthur V2 system record.

    This is intentionally separate from the legacy Arthur seed so UI-DEV can
    provision the version it manages before starting the WhatsApp QR flow.
    """
    from arthur_v2.seed import seed

    return AgentResponse.model_validate(await seed())


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
    if payload.tools_config is not None:
        _validate_tools_config(payload.tools_config)
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
    if agent.wa_device_id:
        try:
            from app.core.infra.wa_client import delete_wa_device
            await delete_wa_device(agent.wa_device_id)
        except Exception as exc:
            logger.warning("delete_agent.wa_disconnect_failed", error=str(exc))
    agent.is_deleted = True
    await db.flush()


@router.post("/{agent_id}/renew", response_model=AgentRenewResponse)
async def renew_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentRenewResponse:
    agent = await _get_active_agent(agent_id, db)
    agent.active_until = datetime.now(timezone.utc) + timedelta(days=agent.quota_period_days)
    agent.tokens_used = 0
    await db.flush()
    await db.refresh(agent)
    return AgentRenewResponse(
        id=agent.id,
        api_key=agent.api_key,
        tokens_used=agent.tokens_used,
        token_quota=agent.token_quota,
        active_until=agent.active_until,
        quota_period_days=agent.quota_period_days,
        message=(
            f"Agent renewed for {agent.quota_period_days} days. "
            f"Token quota reset to {agent.token_quota:,}."
        ),
    )


@router.get("/{agent_id}/whatsapp/qr", response_model=AgentWhatsAppQRResponse)
async def get_whatsapp_qr(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentWhatsAppQRResponse:
    """Get a fresh WhatsApp QR code for an agent (legacy QR flow)."""
    agent = await _get_active_agent(agent_id, db)
    if agent.wa_connection_type == "cloud_api":
        return AgentWhatsAppQRResponse(
            device_id=agent.wa_phone_number_id or "",
            qr_image="",
            status="connected",
        )
    if not agent.wa_device_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent does not have a legacy WhatsApp QR channel configured. Use Meta Embedded Signup instead.",
        )
    try:
        from app.core.infra.wa_client import get_wa_qr
        result = await get_wa_qr(agent.wa_device_id)
        return AgentWhatsAppQRResponse(
            device_id=agent.wa_device_id,
            qr_image=result.get("qr_image", ""),
            status=result.get("status", "unknown"),
        )
    except Exception as exc:
        logger.warning("get_whatsapp_qr.wa_service_down", error=str(exc))
        return AgentWhatsAppQRResponse(
            device_id=agent.wa_device_id or "",
            qr_image="",
            status="service_unavailable",
        )


@router.get("/{agent_id}/whatsapp/status", response_model=AgentWhatsAppStatusResponse)
async def get_whatsapp_status(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentWhatsAppStatusResponse:
    """Get WhatsApp connection status for an agent (Cloud API or legacy)."""
    agent = await _get_active_agent(agent_id, db)
    if agent.wa_connection_type == "cloud_api" or agent.wa_phone_number_id:
        return AgentWhatsAppStatusResponse(
            device_id=agent.wa_phone_number_id or "",
            status="connected" if agent.wa_phone_number_id else "disconnected",
            phone_number=agent.wa_display_phone or "",
        )
    if not agent.wa_device_id:
        return AgentWhatsAppStatusResponse(
            device_id="",
            status="disconnected",
            phone_number="",
        )
    try:
        from app.core.infra.wa_client import get_wa_status
        result = await get_wa_status(agent.wa_device_id)
        return AgentWhatsAppStatusResponse(
            device_id=agent.wa_device_id,
            status=result.get("status", "unknown"),
            phone_number=result.get("phone_number", ""),
        )
    except Exception as exc:
        logger.warning("get_whatsapp_status.wa_service_down", error=str(exc))
        return AgentWhatsAppStatusResponse(
            device_id=agent.wa_device_id,
            status="service_unavailable",
            phone_number="",
        )


@router.delete("/{agent_id}/whatsapp", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_whatsapp(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> Response:
    """Logout WhatsApp and clear device/WABA settings from the agent."""
    agent = await _get_active_agent(agent_id, db)
    if agent.wa_device_id:
        try:
            from app.core.infra.wa_client import delete_wa_device
            await delete_wa_device(agent.wa_device_id)
        except Exception as exc:
            logger.warning("disconnect_whatsapp.wa_service_error", error=str(exc))

    agent.wa_device_id = None
    agent.wa_phone_number_id = None
    agent.wa_waba_id = None
    agent.wa_access_token_encrypted = None
    agent.wa_display_phone = None
    agent.wa_business_name = None
    agent.wa_connection_type = None
    agent.channel_type = None
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{agent_id}/whatsapp/connect", response_model=AgentWhatsAppQRResponse)
async def connect_whatsapp(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> AgentWhatsAppQRResponse:
    """Initialize or re-initialize WhatsApp for an existing agent."""
    agent = await _get_active_agent(agent_id, db)
    if agent.wa_connection_type == "cloud_api":
        return AgentWhatsAppQRResponse(
            device_id=agent.wa_phone_number_id or "",
            qr_image="",
            status="connected",
        )

    # Generate device_id if not present
    if not agent.wa_device_id:
        agent.wa_device_id = str(uuid.uuid4())
        agent.channel_type = "whatsapp"
        await db.flush()

    try:
        from app.core.infra.wa_client import create_wa_device
        wa_result = await create_wa_device(agent.wa_device_id)
        return AgentWhatsAppQRResponse(
            device_id=agent.wa_device_id,
            qr_image=wa_result.get("qr_image", ""),
            status=wa_result.get("status", "waiting_qr"),
        )
    except Exception as exc:
        logger.warning("connect_whatsapp.wa_service_unreachable", error=str(exc))
        # Return fallback response explaining that legacy wa-service is down
        return AgentWhatsAppQRResponse(
            device_id=agent.wa_device_id,
            qr_image="",
            status="service_unavailable",
        )


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

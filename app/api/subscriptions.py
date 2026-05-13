"""Subscription management endpoints.

- GET  /v1/subscriptions/plans                    — list semua plan aktif
- POST /v1/subscriptions/{user_id}/activate       — aktivasi trial / subscription baru
- POST /v1/subscriptions/{user_id}/upgrade        — upgrade/downgrade plan
- POST /v1/subscriptions/{user_id}/topup          — tambah token (admin, dari payment gateway)
- GET  /v1/subscriptions/{user_id}                — cek status subscription (admin)
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key
from app.models.subscription import SubscriptionPlan, UserSubscription, TokenTopup

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PlanResponse(BaseModel):
    id: uuid.UUID
    code: str
    label: str
    max_agents: int | None
    token_quota: int
    period_days: int | None
    grace_period_days: int
    subagents_allowed: bool
    wa_connect: bool
    is_trial: bool

    model_config = {"from_attributes": True}


class ActivateRequest(BaseModel):
    plan_code: str = Field(..., description="Kode plan: 'trial', 'tier_1', 'tier_2', 'tier_3'")


class UpgradeRequest(BaseModel):
    plan_code: str = Field(..., description="Kode plan tujuan: 'tier_1', 'tier_2', 'tier_3'")
    reference_id: str = Field(..., min_length=1, description="ID transaksi dari payment gateway (harus unik)")


class TopUpRequest(BaseModel):
    tokens: int = Field(..., gt=0, description="Jumlah token yang ditambahkan")
    reference_id: str = Field(..., min_length=1, description="ID transaksi dari payment gateway (harus unik)")


class SubscriptionResponse(BaseModel):
    user_id: uuid.UUID
    plan_code: str
    plan_label: str
    subscription_status: str
    token_quota: int
    tokens_used: int
    tokens_remaining: int
    max_agents: int | None
    subagents_allowed: bool
    wa_connect: bool
    expires_at: datetime | None
    grace_until: datetime | None


class TopUpResponse(BaseModel):
    user_id: uuid.UUID
    tokens_added: int
    token_quota_before: int
    token_quota_after: int
    tokens_used: int
    tokens_remaining: int
    subscription_status: str
    reference_id: str
    topped_up_at: datetime


class UpgradeResponse(BaseModel):
    user_id: uuid.UUID
    previous_plan: str
    new_plan: str
    subscription_status: str
    token_quota: int
    tokens_used: int
    tokens_remaining: int
    expires_at: datetime | None
    reference_id: str
    upgraded_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_plan_by_code(code: str, db: AsyncSession) -> SubscriptionPlan:
    plan = (
        await db.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.code == code,
                SubscriptionPlan.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plan '{code}' tidak ditemukan")
    return plan


async def _get_active_subscription(user_id: uuid.UUID, db: AsyncSession) -> UserSubscription:
    sub = (
        await db.execute(
            select(UserSubscription).where(UserSubscription.user_id == user_id)
        )
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak memiliki subscription aktif")
    return sub


async def _check_reference_id_unique(reference_id: str, db: AsyncSession) -> None:
    existing = (
        await db.execute(
            select(TokenTopup).where(TokenTopup.reference_id == reference_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"reference_id '{reference_id}' sudah digunakan")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/plans",
    response_model=list[PlanResponse],
    summary="List semua subscription plan aktif",
)
async def list_plans(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> list[PlanResponse]:
    """Return semua plan yang is_active=True, diurutkan dari trial ke tier tertinggi."""
    plans = (
        await db.execute(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.is_active.is_(True))
            .order_by(SubscriptionPlan.token_quota)
        )
    ).scalars().all()
    return [PlanResponse.model_validate(p) for p in plans]


@router.post(
    "/{user_id}/activate",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Aktivasi subscription untuk user baru",
    description=(
        "Buat subscription baru untuk user. "
        "Gunakan plan_code='trial' untuk trial gratis (hanya bisa sekali per user). "
        "Jika user sudah punya subscription aktif, endpoint ini akan ditolak — "
        "gunakan /upgrade untuk ganti plan."
    ),
)
async def activate_subscription(
    user_id: uuid.UUID,
    payload: ActivateRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SubscriptionResponse:
    # Cek sudah punya subscription
    existing = (
        await db.execute(select(UserSubscription).where(UserSubscription.user_id == user_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User sudah memiliki subscription. Gunakan /upgrade untuk ganti plan.",
        )

    plan = await _get_plan_by_code(payload.plan_code, db)

    now = datetime.now(timezone.utc)
    expires_at = None
    grace_until = None
    if plan.period_days:
        expires_at = now + timedelta(days=plan.period_days)
        grace_until = expires_at + timedelta(days=plan.grace_period_days)

    sub = UserSubscription(
        id=uuid.uuid4(),
        user_id=user_id,
        plan_id=plan.id,
        status="trial" if plan.is_trial else "active",
        token_quota=plan.token_quota,
        tokens_used=0,
        started_at=now,
        expires_at=expires_at,
        grace_until=grace_until,
    )
    db.add(sub)
    try:
        await db.flush()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} tidak ditemukan",
        ) from exc
    await db.refresh(sub)

    logger.info("subscription.activated", user_id=str(user_id), plan=plan.code)
    return SubscriptionResponse(
        user_id=user_id,
        plan_code=plan.code,
        plan_label=plan.label,
        subscription_status=sub.status,
        token_quota=sub.token_quota,
        tokens_used=sub.tokens_used,
        tokens_remaining=sub.tokens_remaining,
        max_agents=plan.max_agents,
        subagents_allowed=plan.subagents_allowed,
        wa_connect=plan.wa_connect,
        expires_at=sub.expires_at,
        grace_until=sub.grace_until,
    )


@router.post(
    "/{user_id}/upgrade",
    response_model=UpgradeResponse,
    summary="Upgrade atau downgrade plan subscription",
    description=(
        "Ganti plan subscription user ke plan lain. "
        "Token quota di-reset ke quota plan baru. "
        "reference_id harus unik per transaksi (idempotency guard). "
        "Admin-only (X-API-Key)."
    ),
)
async def upgrade_subscription(
    user_id: uuid.UUID,
    payload: UpgradeRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> UpgradeResponse:
    if payload.plan_code == "trial":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tidak bisa upgrade ke plan trial. Gunakan /activate untuk aktivasi trial.",
        )

    await _check_reference_id_unique(payload.reference_id, db)

    sub = await _get_active_subscription(user_id, db)

    # Load plan lama untuk response
    old_plan = (
        await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id))
    ).scalar_one()

    new_plan = await _get_plan_by_code(payload.plan_code, db)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=new_plan.period_days) if new_plan.period_days else None
    grace_until = expires_at + timedelta(days=new_plan.grace_period_days) if expires_at else None

    # Catat topup idempotency dengan reference_id
    topup = TokenTopup(
        id=uuid.uuid4(),
        user_id=user_id,
        subscription_id=sub.id,
        tokens_added=new_plan.token_quota - sub.token_quota,
        token_quota_before=sub.token_quota,
        token_quota_after=new_plan.token_quota,
        reference_id=payload.reference_id,
        note=f"Plan upgrade: {old_plan.code} → {new_plan.code}",
    )
    db.add(topup)

    sub.plan_id = new_plan.id
    sub.status = "active"
    sub.token_quota = new_plan.token_quota
    sub.tokens_used = 0
    sub.started_at = now
    sub.expires_at = expires_at
    sub.grace_until = grace_until

    await db.flush()

    logger.info("subscription.upgraded", user_id=str(user_id), old_plan=old_plan.code, new_plan=new_plan.code)
    return UpgradeResponse(
        user_id=user_id,
        previous_plan=old_plan.code,
        new_plan=new_plan.code,
        subscription_status=sub.status,
        token_quota=sub.token_quota,
        tokens_used=sub.tokens_used,
        tokens_remaining=sub.tokens_remaining,
        expires_at=sub.expires_at,
        reference_id=payload.reference_id,
        upgraded_at=now,
    )


@router.post(
    "/{user_id}/topup",
    response_model=TopUpResponse,
    summary="Top-up token subscription user",
    description=(
        "Tambah token quota ke subscription aktif milik user. "
        "Dipanggil oleh backend website setelah payment gateway konfirmasi sukses. "
        "reference_id harus unik per transaksi untuk mencegah double top-up. "
        "Admin-only (X-API-Key)."
    ),
)
async def topup_tokens(
    user_id: uuid.UUID,
    payload: TopUpRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> TopUpResponse:
    await _check_reference_id_unique(payload.reference_id, db)

    sub = await _get_active_subscription(user_id, db)

    if sub.status == "expired":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Subscription sudah expired. Gunakan /upgrade untuk aktivasi ulang.",
        )

    quota_before = sub.token_quota
    sub.token_quota += payload.tokens
    if sub.status == "grace_period":
        sub.status = "active"

    topup = TokenTopup(
        id=uuid.uuid4(),
        user_id=user_id,
        subscription_id=sub.id,
        tokens_added=payload.tokens,
        token_quota_before=quota_before,
        token_quota_after=sub.token_quota,
        reference_id=payload.reference_id,
    )
    db.add(topup)
    await db.flush()

    logger.info("subscription.topup", user_id=str(user_id), tokens_added=payload.tokens, reference_id=payload.reference_id)
    return TopUpResponse(
        user_id=user_id,
        tokens_added=payload.tokens,
        token_quota_before=quota_before,
        token_quota_after=sub.token_quota,
        tokens_used=sub.tokens_used,
        tokens_remaining=sub.tokens_remaining,
        subscription_status=sub.status,
        reference_id=payload.reference_id,
        topped_up_at=topup.topped_up_at,
    )


@router.get(
    "/{user_id}",
    response_model=SubscriptionResponse,
    summary="Cek status subscription user",
    description="Lihat detail subscription, sisa token, dan masa aktif. Admin-only (X-API-Key).",
)
async def get_subscription_status(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SubscriptionResponse:
    sub = await _get_active_subscription(user_id, db)

    plan = (
        await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id))
    ).scalar_one()

    return SubscriptionResponse(
        user_id=user_id,
        plan_code=plan.code,
        plan_label=plan.label,
        subscription_status=sub.status,
        token_quota=sub.token_quota,
        tokens_used=sub.tokens_used,
        tokens_remaining=sub.tokens_remaining,
        max_agents=plan.max_agents,
        subagents_allowed=plan.subagents_allowed,
        wa_connect=plan.wa_connect,
        expires_at=sub.expires_at,
        grace_until=sub.grace_until,
    )

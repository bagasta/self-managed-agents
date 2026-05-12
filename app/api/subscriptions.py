"""Subscription management endpoints.

- POST /v1/subscriptions/{user_id}/topup  — add tokens to active subscription (admin only)
- GET  /v1/subscriptions/{user_id}        — get subscription status (admin only)
"""
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])


# ---------------------------------------------------------------------------
# Schemas (inline — belum ada tabel subscription, pakai stub dulu)
# ---------------------------------------------------------------------------

class TopUpRequest(BaseModel):
    tokens: int = Field(..., gt=0, description="Jumlah token yang ditambahkan")
    reference_id: str = Field(..., min_length=1, description="ID transaksi dari payment gateway (harus unik)")


class TopUpResponse(BaseModel):
    user_id: uuid.UUID
    tokens_added: int
    token_quota_before: int
    token_quota_after: int
    tokens_used: int
    tokens_remaining: int
    status: str
    reference_id: str
    topped_up_at: datetime


class SubscriptionStatusResponse(BaseModel):
    user_id: uuid.UUID
    plan: str
    subscription_status: str
    token_quota: int
    tokens_used: int
    tokens_remaining: int
    expires_at: datetime | None
    grace_until: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_subscription_for_user(user_id: uuid.UUID):
    """
    Placeholder — akan diganti dengan query ke tabel user_subscriptions
    setelah tim backend web implement tabel users + subscriptions.

    Untuk sekarang raise NotImplemented agar endpoint sudah terdaftar
    dan bisa di-test strukturnya, tapi belum bisa dipakai production.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Subscription system not yet implemented. "
            "Endpoint structure is ready — waiting for users + user_subscriptions tables."
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/{user_id}/topup",
    response_model=TopUpResponse,
    status_code=status.HTTP_200_OK,
    summary="Top-up token subscription user",
    description=(
        "Tambah token quota ke subscription aktif milik user. "
        "Dipanggil oleh backend website setelah payment gateway konfirmasi sukses. "
        "Tidak menangani pembayaran — hanya mencatat penambahan token. "
        "reference_id harus unik per transaksi untuk mencegah double top-up."
    ),
)
async def topup_tokens(
    user_id: uuid.UUID,
    payload: TopUpRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> TopUpResponse:
    """Add tokens to a user's active subscription. Admin-only (X-API-Key)."""

    # TODO: Implement setelah tabel user_subscriptions tersedia.
    # Flow yang harus diimplementasikan:
    #
    # 1. Ambil subscription aktif user:
    #    sub = await db.execute(
    #        select(UserSubscription)
    #        .where(UserSubscription.user_id == user_id)
    #        .where(UserSubscription.status.in_(["trial", "active", "grace_period"]))
    #    )
    #
    # 2. Validasi subscription ditemukan dan statusnya bukan "expired"
    #
    # 3. Cek idempotency — reference_id belum pernah dipakai:
    #    existing = await db.execute(
    #        select(TokenTopup).where(TokenTopup.reference_id == payload.reference_id)
    #    )
    #    if existing: raise HTTPException(409, "reference_id already used")
    #
    # 4. Tambah token:
    #    quota_before = sub.token_quota
    #    sub.token_quota += payload.tokens
    #    if sub.status == "grace_period":
    #        sub.status = "active"
    #
    # 5. Catat ke token_topups:
    #    topup = TokenTopup(
    #        user_id=user_id,
    #        subscription_id=sub.id,
    #        tokens_added=payload.tokens,
    #        reference_id=payload.reference_id,
    #    )
    #    db.add(topup)
    #
    # 6. Flush + return response

    _get_subscription_for_user(user_id)  # raises 501 sampai diimplementasikan


@router.get(
    "/{user_id}",
    response_model=SubscriptionStatusResponse,
    summary="Cek status subscription user",
    description="Lihat detail subscription, sisa token, dan masa aktif. Admin-only (X-API-Key).",
)
async def get_subscription_status(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> SubscriptionStatusResponse:
    """Get subscription status for a user. Admin-only (X-API-Key)."""

    # TODO: Implement setelah tabel user_subscriptions tersedia.
    # Flow:
    # sub = await db.execute(select(UserSubscription).where(...))
    # plan = await db.execute(select(SubscriptionPlan).where(...))
    # return SubscriptionStatusResponse(...)

    _get_subscription_for_user(user_id)  # raises 501 sampai diimplementasikan

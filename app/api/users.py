"""User management endpoints.

- POST /v1/users          — daftarkan user baru, opsional langsung assign plan
- GET  /v1/users/{user_id} — lihat profil + status subscription user
- PATCH /v1/users/{user_id} — update profil user
"""
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import verify_api_key
from app.models.subscription import SubscriptionPlan, User, UserSubscription

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/users", tags=["users"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    email: str = Field(..., description="Alamat email user")
    password: str = Field(..., min_length=8, description="Plain-text password (akan di-hash)")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Format email tidak valid")
        return v.lower().strip()
    full_name: str | None = None
    external_id: str | None = Field(
        None,
        max_length=64,
        description="ID eksternal (nomor WA, user ID dari sistem lain). Di-generate otomatis jika kosong.",
    )
    plan_code: str | None = Field(
        None,
        description="Kode plan yang langsung diaktifkan: 'trial', 'tier_1', 'tier_2', 'tier_3'. "
                    "Jika tidak diisi, user dibuat tanpa subscription.",
    )


class PhoneLoginRequest(BaseModel):
    phone_number: str = Field(..., description="Nomor HP terdaftar (format: 628xxx)")


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    phone_number: str | None = None
    external_id: str
    email_verified: bool
    has_used_trial: bool
    created_at: datetime


class UserWithSubscriptionResponse(UserResponse):
    subscription: dict | None = None


class UserUpdate(BaseModel):
    full_name: str | None = None
    email_verified: bool | None = None
    phone_number: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_password(plain: str) -> str:
    return sha256(plain.encode("utf-8")).hexdigest()


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan '{code}' tidak ditemukan",
        )
    return plan


def _build_subscription_dict(sub: UserSubscription, plan: SubscriptionPlan) -> dict:
    return {
        "plan_code": plan.code,
        "plan_label": plan.label,
        "status": sub.status,
        "token_quota": sub.token_quota,
        "tokens_used": sub.tokens_used,
        "tokens_remaining": sub.tokens_remaining,
        "max_agents": plan.max_agents,
        "subagents_allowed": plan.subagents_allowed,
        "wa_connect": plan.wa_connect,
        "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
        "grace_until": sub.grace_until.isoformat() if sub.grace_until else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=UserWithSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Daftarkan user baru",
    description=(
        "Buat user baru. Jika `plan_code` diisi, subscription langsung diaktifkan. "
        "Admin-only (X-API-Key)."
    ),
)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> UserWithSubscriptionResponse:
    # Cek email duplicate
    existing = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{payload.email}' sudah terdaftar",
        )

    external_id = payload.external_id or uuid.uuid4().hex[:16]

    user = User(
        id=uuid.uuid4(),
        email=payload.email,
        password_hash=_hash_password(payload.password),
        full_name=payload.full_name,
        external_id=external_id,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)

    subscription_dict = None

    if payload.plan_code:
        plan = await _get_plan_by_code(payload.plan_code, db)

        now = datetime.now(timezone.utc)
        expires_at = None
        grace_until = None
        if plan.period_days:
            expires_at = now + timedelta(days=plan.period_days)
            grace_until = expires_at + timedelta(days=plan.grace_period_days)

        sub = UserSubscription(
            id=uuid.uuid4(),
            user_id=user.id,
            plan_id=plan.id,
            status="trial" if plan.is_trial else "active",
            token_quota=plan.token_quota,
            tokens_used=0,
            started_at=now,
            expires_at=expires_at,
            grace_until=grace_until,
        )
        db.add(sub)
        await db.flush()

        subscription_dict = _build_subscription_dict(sub, plan)

        if plan.is_trial:
            user.has_used_trial = True
            await db.flush()

    logger.info("user.created", user_id=str(user.id), email=user.email, plan=payload.plan_code)

    return UserWithSubscriptionResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        external_id=user.external_id,
        email_verified=user.email_verified,
        has_used_trial=user.has_used_trial,
        created_at=user.created_at,
        subscription=subscription_dict,
    )


@router.get(
    "/{user_id}",
    response_model=UserWithSubscriptionResponse,
    summary="Lihat profil + subscription user",
    description="Admin-only (X-API-Key).",
)
async def get_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> UserWithSubscriptionResponse:
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan")

    subscription_dict = None
    sub = (
        await db.execute(select(UserSubscription).where(UserSubscription.user_id == user_id))
    ).scalar_one_or_none()
    if sub is not None:
        plan = (
            await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id))
        ).scalar_one()
        subscription_dict = _build_subscription_dict(sub, plan)

    return UserWithSubscriptionResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        external_id=user.external_id,
        email_verified=user.email_verified,
        has_used_trial=user.has_used_trial,
        created_at=user.created_at,
        subscription=subscription_dict,
    )


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update profil user",
    description="Admin-only (X-API-Key).",
)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> UserResponse:
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan")

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.email_verified is not None:
        user.email_verified = payload.email_verified
    if payload.phone_number is not None:
        user.phone_number = payload.phone_number

    await db.flush()
    await db.refresh(user)

    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        external_id=user.external_id,
        email_verified=user.email_verified,
        has_used_trial=user.has_used_trial,
        created_at=user.created_at,
    )


@router.post(
    "/login/phone",
    response_model=UserWithSubscriptionResponse,
    summary="Login via nomor HP",
    description="Public endpoint — tidak butuh X-API-Key. Mengembalikan profil user jika nomor terdaftar.",
)
async def phone_login(
    payload: PhoneLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> UserWithSubscriptionResponse:
    phone = payload.phone_number.strip()
    user = (
        await db.execute(
            select(User).where(
                or_(User.phone_number == phone, User.external_id == phone)
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nomor HP tidak terdaftar",
        )

    subscription_dict = None
    sub = (
        await db.execute(select(UserSubscription).where(UserSubscription.user_id == user.id))
    ).scalar_one_or_none()
    if sub is not None:
        plan = (
            await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id))
        ).scalar_one()
        subscription_dict = _build_subscription_dict(sub, plan)

    logger.info("user.phone_login", user_id=str(user.id), phone=payload.phone_number)

    return UserWithSubscriptionResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        external_id=user.external_id,
        email_verified=user.email_verified,
        has_used_trial=user.has_used_trial,
        created_at=user.created_at,
        subscription=subscription_dict,
    )


# ---------------------------------------------------------------------------
# WhatsApp linking — one-time code shown in dashboard, claimed via Arthur
# ---------------------------------------------------------------------------

class WaLinkCodeResponse(BaseModel):
    user_id: uuid.UUID
    code: str
    expires_at: datetime


class WaLinkClaimRequest(BaseModel):
    code: str = Field(..., description="Kode link dari dashboard")
    phone_number: str = Field(..., description="Nomor WA yang mau dihubungkan (format: 628xxx)")


@router.post(
    "/{user_id}/wa-link-code",
    response_model=WaLinkCodeResponse,
    summary="Generate kode link WhatsApp untuk user dashboard",
    description=(
        "Dipanggil backend website; kode ditampilkan di dashboard lalu user "
        "mengirimkannya ke Arthur via WhatsApp untuk menghubungkan nomornya. "
        "Kode sekali pakai, kedaluwarsa 15 menit. Admin-only (X-API-Key)."
    ),
)
async def create_wa_link_code(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> WaLinkCodeResponse:
    from app.core.domain.wa_link_service import generate_wa_link_code

    try:
        link = await generate_wa_link_code(user_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await db.commit()
    return WaLinkCodeResponse(user_id=user_id, code=link.code, expires_at=link.expires_at)


@router.post(
    "/wa-link/claim",
    summary="Klaim kode link WhatsApp secara manual",
    description=(
        "Jalur manual/support untuk menghubungkan nomor WA ke akun dashboard "
        "tanpa lewat Arthur. Admin-only (X-API-Key)."
    ),
)
async def claim_wa_link(
    payload: WaLinkClaimRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_api_key),
) -> dict:
    from app.core.domain.wa_link_service import claim_wa_link_code

    result = await claim_wa_link_code(payload.code, db, sender_ids=[payload.phone_number])
    if result.get("error"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["error"])
    await db.commit()
    return result

"""
subscription_service.py — User & subscription lifecycle untuk platform.

Dipanggil saat Arthur (atau channel manapun) membuat agent baru.
Memastikan setiap external_user_id punya record users + user_subscriptions + user_api_keys.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import SubscriptionPlan, User, UserSubscription
from app.models.user_api_key import UserApiKey, generate_user_key, hash_user_key

logger = structlog.get_logger(__name__)


DEFAULT_SUBSCRIPTION_PLANS: list[dict[str, Any]] = [
    {
        "id": SubscriptionPlan.TRIAL_ID,
        "code": "trial",
        "label": "Trial",
        "max_agents": 1,
        "token_quota": 2_000_000,
        "period_days": None,
        "grace_period_days": 3,
        "allowed_models": ["openai/gpt-4.1-mini"],
        "subagents_allowed": False,
        "wa_connect": True,
        "is_trial": True,
        "is_active": True,
    },
    {
        "id": SubscriptionPlan.TIER_1_ID,
        "code": "tier_1",
        "label": "Starter",
        "max_agents": 1,
        "token_quota": 10_000_000,
        "period_days": 30,
        "grace_period_days": 3,
        "allowed_models": ["openai/gpt-4.1-mini"],
        "subagents_allowed": True,
        "wa_connect": True,
        "is_trial": False,
        "is_active": True,
    },
    {
        "id": SubscriptionPlan.TIER_2_ID,
        "code": "tier_2",
        "label": "Pro",
        "max_agents": 2,
        "token_quota": 20_000_000,
        "period_days": 30,
        "grace_period_days": 3,
        "allowed_models": ["openai/gpt-4.1-mini", "deepseek/deepseek-v4-flash"],
        "subagents_allowed": True,
        "wa_connect": True,
        "is_trial": False,
        "is_active": True,
    },
    {
        "id": SubscriptionPlan.TIER_3_ID,
        "code": "tier_3",
        "label": "Enterprise",
        "max_agents": None,
        "token_quota": 0,
        "period_days": None,
        "grace_period_days": 7,
        "allowed_models": [],
        "subagents_allowed": True,
        "wa_connect": True,
        "is_trial": False,
        "is_active": True,
    },
]


async def ensure_default_subscription_plans(db: AsyncSession) -> None:
    """Idempotently seed core plans required by auto-provisioning."""
    existing_ids = set(
        (
            await db.execute(
                select(SubscriptionPlan.id).where(
                    SubscriptionPlan.id.in_([p["id"] for p in DEFAULT_SUBSCRIPTION_PLANS])
                )
            )
        ).scalars().all()
    )
    for data in DEFAULT_SUBSCRIPTION_PLANS:
        if data["id"] not in existing_ids:
            db.add(SubscriptionPlan(**data))
    await db.flush()


async def get_or_create_wa_user(
    external_id: str,
    db: AsyncSession,
) -> tuple[User, UserSubscription]:
    """
    Cari user berdasarkan external_id (nomor WA / JID).
    Kalau belum ada → buat User + Tier 1 subscription + UserApiKey otomatis.
    Kalau sudah ada tapi belum punya subscription → buat Tier 1.

    Returns (user, subscription).
    """
    await ensure_default_subscription_plans(db)

    user = (
        await db.execute(select(User).where(User.external_id == external_id))
    ).scalar_one_or_none()

    if user is None:
        user = User(
            email=f"{external_id.replace('+', '').replace(' ', '')}@wa.placeholder",
            password_hash="",
            external_id=external_id,
            has_used_trial=False,
            email_verified=False,
        )
        db.add(user)
        await db.flush()
        logger.info("subscription_service.user_created", external_id=external_id, user_id=str(user.id))

    # Subscription
    sub = (
        await db.execute(
            select(UserSubscription).where(UserSubscription.user_id == user.id)
        )
    ).scalar_one_or_none()

    if sub is None:
        sub = await _create_tier1_subscription(user.id, db)
        user.has_used_trial = True
        logger.info("subscription_service.subscription_created", user_id=str(user.id), plan="tier_1")

    # UserApiKey — buat satu kalau belum punya
    existing_key = (
        await db.execute(
            select(UserApiKey).where(UserApiKey.label == f"wa:{external_id}")
        )
    ).scalar_one_or_none()

    if existing_key is None:
        raw_key = generate_user_key()
        api_key = UserApiKey(
            key_hash=hash_user_key(raw_key),
            label=f"wa:{external_id}",
            expires_at=sub.expires_at or (datetime.now(timezone.utc) + timedelta(days=30)),
        )
        db.add(api_key)
        logger.info("subscription_service.api_key_created", external_id=external_id)

    await db.flush()
    return user, sub


async def _create_tier1_subscription(
    user_id: uuid.UUID,
    db: AsyncSession,
) -> UserSubscription:
    plan = (
        await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == SubscriptionPlan.TIER_1_ID)
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=plan.period_days)
    grace_until = expires_at + timedelta(days=plan.grace_period_days)

    sub = UserSubscription(
        user_id=user_id,
        plan_id=plan.id,
        status="active",
        token_quota=plan.token_quota,
        tokens_used=0,
        started_at=now,
        expires_at=expires_at,
        grace_until=grace_until,
    )
    db.add(sub)
    await db.flush()
    return sub


async def get_subscription_by_external_id(
    external_id: str,
    db: AsyncSession,
) -> tuple[User, UserSubscription, SubscriptionPlan] | None:
    user = (
        await db.execute(select(User).where(User.external_id == external_id))
    ).scalar_one_or_none()
    if user is None:
        return None

    sub = (
        await db.execute(
            select(UserSubscription).where(UserSubscription.user_id == user.id)
        )
    ).scalar_one_or_none()
    if sub is None:
        return None

    plan = (
        await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
        )
    ).scalar_one()

    return user, sub, plan


def _tool_enabled(tools_config: dict[str, Any], key: str, default: bool = False) -> bool:
    cfg = tools_config.get(key)
    if cfg is None:
        return default
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", default))
    return default


def validate_agent_entitlements(
    plan: SubscriptionPlan,
    *,
    model: str,
    tools_config: dict[str, Any],
    channel_type: str | None,
) -> list[str]:
    """Return plan violations for a proposed agent config."""
    violations: list[str] = []
    allowed_models = list(plan.allowed_models or [])
    if allowed_models and model not in allowed_models:
        violations.append(
            f"Model '{model}' tidak tersedia di plan {plan.label}. Model yang tersedia: {', '.join(allowed_models)}."
        )

    if _tool_enabled(tools_config, "subagents", default=False) and not plan.subagents_allowed:
        violations.append(f"Plan {plan.label} tidak mengizinkan sub-agent.")

    if channel_type == "whatsapp" and not plan.wa_connect:
        violations.append(f"Plan {plan.label} tidak mengizinkan koneksi WhatsApp.")

    return violations


async def check_can_create_agent(
    external_id: str,
    db: AsyncSession,
) -> dict:
    """
    Cek apakah user boleh buat agent baru.

    Menghitung agent aktif milik user via DUA cara:
    1. owner_external_id == external_id  (agent baru)
    2. operator_ids @> ARRAY[external_id]  (agent lama / legacy)
    Digabung dengan OR agar agent lama tetap terhitung.
    """
    from sqlalchemy.dialects.postgresql import array
    from app.models.agent import Agent

    result = await get_subscription_by_external_id(external_id, db)
    if result is None:
        return {"allowed": False, "reason": "Subscription tidak ditemukan."}

    user, sub, plan = result

    if not sub.is_usable:
        return {
            "allowed": False,
            "reason": "Subscription kamu sudah expired. Silakan renew untuk melanjutkan.",
            "plan": plan.label,
        }

    # Gabungkan: agent baru (owner_external_id) + agent lama (operator_ids)
    active_agents = (
        await db.execute(
            select(Agent).where(
                Agent.is_deleted.is_(False),
                or_(
                    Agent.owner_external_id == external_id,
                    Agent.operator_ids.contains([external_id]),
                ),
            )
        )
    ).scalars().all()
    agents_used = len(active_agents)

    if plan.max_agents is not None and agents_used >= plan.max_agents:
        return {
            "allowed": False,
            "reason": (
                f"Kamu sudah punya {agents_used} agent aktif. "
                f"Plan {plan.label} maksimal {plan.max_agents} agent. "
                f"Upgrade ke Tier 2 untuk bisa membuat lebih banyak agent."
            ),
            "plan": plan.label,
            "agents_used": agents_used,
            "max_agents": plan.max_agents,
        }

    return {
        "allowed": True,
        "plan": plan.label,
        "agents_used": agents_used,
        "max_agents": plan.max_agents,
        "tokens_remaining": sub.tokens_remaining,
        "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
    }

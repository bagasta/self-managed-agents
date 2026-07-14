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

from app.core.utils.phone_utils import normalize_phone
from app.core.model_defaults import CREATED_AGENT_DEFAULT_MODEL
from app.models.subscription import SubscriptionPlan, User, UserSubscription
from app.models.user_api_key import UserApiKey, generate_user_key, hash_user_key

logger = structlog.get_logger(__name__)

TIER_3_TOKEN_QUOTA = 100_000_000


class QuotaExceeded(Exception):
    """Token quota habis untuk subscription ini."""


def assert_token_quota_available(subscription: Any) -> None:
    """Read-only. Raise QuotaExceeded kalau tokens_used >= token_quota.
    token_quota None = unlimited (tier_3).
    Respects grace_until: jika dalam grace period, allow the run."""
    quota = getattr(subscription, "token_quota", None)
    if quota is None:
        return
    used = int(getattr(subscription, "tokens_used", 0) or 0)
    if used >= int(quota):
        # Check grace period
        grace_until = getattr(subscription, "grace_until", None)
        if grace_until is not None:
            if isinstance(grace_until, datetime):
                _grace = grace_until if grace_until.tzinfo else grace_until.replace(tzinfo=timezone.utc)
                if _grace > datetime.now(timezone.utc):
                    return
        raise QuotaExceeded(
            f"Token quota habis ({used}/{quota}). Owner perlu upgrade plan atau tunggu reset."
        )


DEFAULT_SUBSCRIPTION_PLANS: list[dict[str, Any]] = [
    {
        "id": SubscriptionPlan.TRIAL_ID,
        "code": "trial",
        "label": "Trial",
        "max_agents": 1,
        "token_quota": 5_000_000,
        "period_days": 14,
        "grace_period_days": 3,
        "allowed_models": [CREATED_AGENT_DEFAULT_MODEL, "openai/gpt-4.1-mini"],
        "subagents_allowed": True,
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
        "allowed_models": [CREATED_AGENT_DEFAULT_MODEL, "openai/gpt-4.1-mini"],
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
        "token_quota": TIER_3_TOKEN_QUOTA,
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
    existing_plans = {
        plan.id: plan
        for plan in (
            await db.execute(
                select(SubscriptionPlan).where(
                    SubscriptionPlan.id.in_([p["id"] for p in DEFAULT_SUBSCRIPTION_PLANS])
                )
            )
        ).scalars().all()
    }
    for data in DEFAULT_SUBSCRIPTION_PLANS:
        existing = existing_plans.get(data["id"])
        if existing is None:
            db.add(SubscriptionPlan(**data))
            continue
        if existing.id == SubscriptionPlan.TIER_3_ID:
            existing.max_agents = None
            existing.token_quota = TIER_3_TOKEN_QUOTA
        if existing.id == SubscriptionPlan.TRIAL_ID:
            existing.max_agents = 1
            existing.token_quota = 5_000_000
            existing.period_days = 14
            existing.allowed_models = list(
                dict.fromkeys([CREATED_AGENT_DEFAULT_MODEL, *(existing.allowed_models or [])])
            )
            existing.subagents_allowed = True
        if existing.id == SubscriptionPlan.TIER_1_ID:
            existing.allowed_models = list(
                dict.fromkeys([CREATED_AGENT_DEFAULT_MODEL, *(existing.allowed_models or [])])
            )
    await db.flush()


async def get_or_create_wa_user(
    external_id: str,
    db: AsyncSession,
    *,
    wa_lid: str | None = None,
) -> tuple[User, UserSubscription]:
    """
    Cari user berdasarkan external_id (nomor WA / JID).
    Kalau belum ada → buat User + Trial subscription + UserApiKey otomatis.
    Kalau sudah ada tapi belum punya subscription → buat Trial.

    ``wa_lid``: LID pengirim, saat turn ini membawa nomor asli + LID sekaligus.
    Mapping disimpan di users.wa_lid agar turn berikutnya yang hanya membawa
    LID tetap bisa di-resolve ke nomor telepon user ini.

    Returns (user, subscription).
    """
    await ensure_default_subscription_plans(db)

    user = (
        await db.execute(
            select(User).where(
                or_(
                    User.external_id == external_id,
                    User.phone_number == external_id,
                    User.wa_lid == external_id,
                )
            )
        )
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

    if wa_lid and getattr(user, "wa_lid", None) != wa_lid:
        user.wa_lid = wa_lid
        logger.info(
            "subscription_service.wa_lid_learned",
            user_id=str(user.id),
            external_id=external_id,
        )

    # Subscription
    sub = (
        await db.execute(
            select(UserSubscription).where(UserSubscription.user_id == user.id)
        )
    ).scalar_one_or_none()

    if sub is None:
        sub = await _create_trial_subscription(user.id, db)
        user.has_used_trial = True
        logger.info("subscription_service.subscription_created", user_id=str(user.id), plan="trial")

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


async def _create_trial_subscription(
    user_id: uuid.UUID,
    db: AsyncSession,
) -> UserSubscription:
    plan = (
        await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == SubscriptionPlan.TRIAL_ID)
        )
    ).scalar_one()

    now = datetime.now(timezone.utc)
    period = plan.period_days or 14
    expires_at = now + timedelta(days=period)
    grace_until = expires_at + timedelta(days=plan.grace_period_days)
    sub = UserSubscription(
        user_id=user_id,
        plan_id=plan.id,
        status="trial",
        token_quota=plan.token_quota,
        tokens_used=0,
        started_at=now,
        expires_at=expires_at,
        grace_until=grace_until,
    )
    db.add(sub)
    await db.flush()
    return sub


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


async def resolve_phone_for_wa_lid(wa_lid: str, db: AsyncSession) -> str | None:
    """Reverse-resolve a WhatsApp LID to the real phone number learned earlier.

    Dipakai saat wa-service gagal me-resolve LID→PN (mapping whatsmeow belum
    terisi): kalau LID ini pernah terlihat bersama nomor asli, kita sudah
    menyimpannya di users.wa_lid dan bisa memakai nomornya lagi.
    """
    lid = str(wa_lid or "").strip()
    if not lid:
        return None
    user = (
        await db.execute(
            select(User).where(User.wa_lid == lid, User.phone_number.isnot(None))
        )
    ).scalars().first()
    phone = str(getattr(user, "phone_number", "") or "").strip()
    return phone or None


async def get_subscription_by_external_id(
    external_id: str,
    db: AsyncSession,
) -> tuple[User, UserSubscription, SubscriptionPlan] | None:
    user = (
        await db.execute(
            select(User).where(
                or_(
                    User.external_id == external_id,
                    User.phone_number == external_id,
                    User.wa_lid == external_id,
                )
            )
        )
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


def _subscription_identifier_variants(*external_ids: str | None) -> tuple[list[str], list[uuid.UUID]]:
    identifiers: list[str] = []
    user_ids: list[uuid.UUID] = []
    for raw in external_ids:
        value = str(raw or "").strip()
        if not value:
            continue
        for candidate in (value, normalize_phone(value)):
            if candidate and candidate not in identifiers:
                identifiers.append(candidate)
        try:
            parsed = uuid.UUID(value)
        except (TypeError, ValueError):
            continue
        if parsed not in user_ids:
            user_ids.append(parsed)
    return identifiers, user_ids


async def get_best_subscription_by_external_ids(
    external_ids: list[str | None] | tuple[str | None, ...],
    db: AsyncSession,
    *,
    sender_name: str | None = None,
) -> tuple[User, UserSubscription, SubscriptionPlan] | None:
    """Read-only subscription lookup across known WA/user identifiers.

    WhatsApp can surface a sender as a phone number, JID, or LID-like numeric
    alias across different events. When duplicate user rows exist from old
    auto-provisioning, prefer an active paid subscription over a trial row.
    """
    identifiers, user_ids = _subscription_identifier_variants(*external_ids)
    clauses = []
    if identifiers:
        clauses.append(User.external_id.in_(identifiers))
        clauses.append(User.phone_number.in_(identifiers))
        clauses.append(User.wa_lid.in_(identifiers))
    if user_ids:
        clauses.append(User.id.in_(user_ids))
    if not clauses:
        return None

    rows = (
        await db.execute(
            select(User, UserSubscription, SubscriptionPlan)
            .join(UserSubscription, UserSubscription.user_id == User.id)
            .join(SubscriptionPlan, SubscriptionPlan.id == UserSubscription.plan_id)
            .where(or_(*clauses))
        )
    ).all()
    if not rows:
        return None

    order = {identifier: index for index, identifier in enumerate(identifiers)}

    def _match_rank(user: User) -> int:
        matches = [
            order.get(str(getattr(user, "external_id", "") or "").strip(), 999),
            order.get(normalize_phone(str(getattr(user, "external_id", "") or "")), 999),
            order.get(str(getattr(user, "phone_number", "") or "").strip(), 999),
            order.get(normalize_phone(str(getattr(user, "phone_number", "") or "")), 999),
            order.get(str(getattr(user, "wa_lid", "") or "").strip(), 999),
            order.get(normalize_phone(str(getattr(user, "wa_lid", "") or "")), 999),
        ]
        return min(matches)

    def _score(row: tuple[User, UserSubscription, SubscriptionPlan]) -> tuple[int, int, int, int]:
        user, sub, plan = row
        paid_score = 1 if not getattr(plan, "is_trial", False) else 0
        usable_score = 1 if getattr(sub, "is_usable", False) else 0
        active_score = 1 if getattr(sub, "status", "") == "active" else 0
        return (paid_score, usable_score, active_score, -_match_rank(user))

    best = max(rows, key=_score)
    _best_user, _best_sub, best_plan = best
    name = str(sender_name or "").strip()
    if getattr(best_plan, "is_trial", False) and len(name) >= 3:
        name_matches = (
            await db.execute(
                select(User, UserSubscription, SubscriptionPlan)
                .join(UserSubscription, UserSubscription.user_id == User.id)
                .join(SubscriptionPlan, SubscriptionPlan.id == UserSubscription.plan_id)
                .where(
                    SubscriptionPlan.is_trial.is_(False),
                    UserSubscription.status.in_(["active", "grace_period"]),
                    or_(
                        User.full_name.ilike(f"%{name}%"),
                        User.email.ilike(f"%{name.lower()}%"),
                    ),
                )
            )
        ).all()
        if len(name_matches) == 1:
            logger.info(
                "subscription.identity_inferred_by_sender_name",
                sender_name=name,
                trial_user_id=str(getattr(_best_user, "id", "")),
                paid_user_id=str(getattr(name_matches[0][0], "id", "")),
            )
            return tuple(name_matches[0])  # type: ignore[return-value]

    return tuple(best)  # type: ignore[return-value]


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

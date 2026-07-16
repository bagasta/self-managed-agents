"""Quota checks and usage accounting for agent runs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AgentQuotaCheck:
    allowed: bool
    reason: str = ""
    detail: str = ""
    user_message: str = ""


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quota_exhausted(used: Any, quota: Any) -> bool:
    quota_int = int(quota or 0)
    if quota_int <= 0:
        return False
    return int(used or 0) >= quota_int


def _quota_detail(label: str, used: Any, quota: Any) -> str:
    return f"{label} token quota exhausted ({int(used or 0):,} / {int(quota or 0):,})."


def is_quota_exempt_builder_agent(agent: Any) -> bool:
    """Arthur/builder agents are platform infrastructure and must not burn user quota."""
    capabilities = getattr(agent, "capabilities", None) or []
    if isinstance(capabilities, str):
        capabilities = [capabilities]

    tools_config = getattr(agent, "tools_config", None) or {}
    if not isinstance(tools_config, dict):
        tools_config = {}

    return (
        "builder" in capabilities
        or "system" in capabilities
        or bool(tools_config.get("builder"))
    )


async def get_owner_subscription(agent: Any, db: AsyncSession):
    owner_external_id = getattr(agent, "owner_external_id", None)
    if not owner_external_id:
        return None, None

    from app.models.subscription import User, UserSubscription

    user = (
        await db.execute(
            select(User).where(
                or_(
                    User.external_id == owner_external_id,
                    User.phone_number == owner_external_id,
                    User.wa_lid == owner_external_id,
                )
            )
        )
    ).scalar_one_or_none()
    if not user:
        return None, None

    subscription = (
        await db.execute(select(UserSubscription).where(UserSubscription.user_id == user.id))
    ).scalar_one_or_none()
    return user, subscription


async def check_agent_quota(agent: Any, db: AsyncSession, *, now: datetime | None = None) -> AgentQuotaCheck:
    """Return a hard gate for any run that would call the LLM."""
    if is_quota_exempt_builder_agent(agent):
        return AgentQuotaCheck(allowed=True)

    current = now or datetime.now(timezone.utc)
    active_until = _as_aware_utc(getattr(agent, "active_until", None))
    if active_until and active_until < current:
        detail = (
            f"Agent subscription expired on {active_until.isoformat()}. "
            "Renew or upgrade to reactivate."
        )
        return AgentQuotaCheck(
            allowed=False,
            reason="agent_subscription_expired",
            detail=detail,
            user_message="Maaf, masa aktif agent ini sudah habis. Silakan renew atau upgrade untuk mengaktifkannya lagi.",
        )

    if _quota_exhausted(getattr(agent, "tokens_used", 0), getattr(agent, "token_quota", 0)):
        detail = _quota_detail("Agent", getattr(agent, "tokens_used", 0), getattr(agent, "token_quota", 0))
        return AgentQuotaCheck(
            allowed=False,
            reason="agent_token_quota_exhausted",
            detail=detail,
            user_message="Maaf, kuota token agent ini sudah habis. Silakan top up atau renew untuk melanjutkan.",
        )

    _, subscription = await get_owner_subscription(agent, db)
    if subscription is None:
        return AgentQuotaCheck(allowed=True)

    if not getattr(subscription, "is_usable", False):
        return AgentQuotaCheck(
            allowed=False,
            reason="owner_subscription_inactive",
            detail="Owner subscription is not usable.",
            user_message="Maaf, subscription pemilik agent ini sedang tidak aktif. Silakan renew atau upgrade untuk melanjutkan.",
        )

    if _quota_exhausted(getattr(subscription, "tokens_used", 0), getattr(subscription, "token_quota", 0)):
        detail = _quota_detail(
            "Subscription",
            getattr(subscription, "tokens_used", 0),
            getattr(subscription, "token_quota", 0),
        )
        return AgentQuotaCheck(
            allowed=False,
            reason="owner_subscription_token_quota_exhausted",
            detail=detail,
            user_message="Maaf, kuota token subscription pemilik agent ini sudah habis. Silakan top up atau renew untuk melanjutkan.",
        )

    return AgentQuotaCheck(allowed=True)


async def record_agent_token_usage(agent: Any, tokens_used: int, db: AsyncSession) -> None:
    """Increment both per-agent and owner subscription usage when available."""
    if tokens_used <= 0:
        return
    if is_quota_exempt_builder_agent(agent):
        return

    agent.tokens_used = int(getattr(agent, "tokens_used", 0) or 0) + int(tokens_used)

    _, subscription = await get_owner_subscription(agent, db)
    if subscription is not None:
        subscription.tokens_used = int(getattr(subscription, "tokens_used", 0) or 0) + int(tokens_used)

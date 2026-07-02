"""Link a dashboard account to a WhatsApp sender identity via one-time code.

Flow:
1. Website backend calls ``POST /v1/users/{user_id}/wa-link-code`` → dashboard
   shows the code to the logged-in user.
2. User sends the code to Arthur on WhatsApp → Arthur calls the
   ``link_dashboard_account`` tool with the verified sender identity.
3. :func:`claim_wa_link_code` writes the sender phone into the dashboard
   user's ``phone_number`` and archives auto-provisioned placeholder users
   (``*@wa.placeholder`` trial rows) that were shadowing the paid account,
   so subscription lookups resolve to a single, correct account.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tools.builder_identity import best_owner_identifier
from app.core.utils.phone_utils import normalize_phone
from app.models.subscription import SubscriptionPlan, User, UserSubscription
from app.models.wa_link_code import WaLinkCode

logger = structlog.get_logger(__name__)

WA_LINK_CODE_TTL_MINUTES = 15
# No 0/O/1/I — codes are typed manually from the dashboard into WhatsApp.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6


def normalize_wa_link_code(code: str | None) -> str:
    raw = str(code or "").upper().strip()
    for junk in (" ", "-", "_", "."):
        raw = raw.replace(junk, "")
    if raw.startswith("LINK"):
        raw = raw[4:]
    return raw


def _generate_code_value() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


async def generate_wa_link_code(
    user_id: uuid.UUID,
    db: AsyncSession,
    *,
    ttl_minutes: int = WA_LINK_CODE_TTL_MINUTES,
) -> WaLinkCode:
    """Create a fresh one-time link code for a dashboard user.

    Any previous unused codes for the same user are expired so only the
    latest code shown in the dashboard is claimable.
    """
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise ValueError(f"User {user_id} tidak ditemukan")

    now = datetime.now(timezone.utc)
    pending = (
        await db.execute(
            select(WaLinkCode).where(
                WaLinkCode.user_id == user_id,
                WaLinkCode.used_at.is_(None),
                WaLinkCode.expires_at > now,
            )
        )
    ).scalars().all()
    for row in pending:
        row.expires_at = now

    link = WaLinkCode(
        user_id=user_id,
        code=_generate_code_value(),
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    db.add(link)
    await db.flush()
    logger.info("wa_link.code_generated", user_id=str(user_id), expires_at=link.expires_at.isoformat())
    return link


async def claim_wa_link_code(
    code: str,
    db: AsyncSession,
    *,
    sender_ids: list[str | None],
) -> dict[str, Any]:
    """Claim a link code using the verified WhatsApp sender identity.

    Returns a JSON-serializable dict: on success the linked account + plan,
    on failure an ``error`` message safe to show to the user.
    """
    normalized_code = normalize_wa_link_code(code)
    if len(normalized_code) != _CODE_LENGTH:
        return {"error": f"Kode harus {_CODE_LENGTH} karakter."}

    identity = best_owner_identifier(*sender_ids)
    if not identity:
        return {
            "error": (
                "Identitas WhatsApp pengirim tidak terbaca, jadi belum bisa di-link. "
                "Coba kirim ulang dari nomor WhatsApp yang mau dihubungkan."
            )
        }

    now = datetime.now(timezone.utc)
    link = (
        await db.execute(
            select(WaLinkCode)
            .where(WaLinkCode.code == normalized_code, WaLinkCode.used_at.is_(None))
            .order_by(WaLinkCode.created_at.desc())
        )
    ).scalars().first()
    if link is None:
        return {"error": "Kode tidak ditemukan atau sudah dipakai. Generate ulang dari dashboard."}
    expires_at = link.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= now:
        return {"error": "Kode sudah kedaluwarsa. Generate kode baru dari dashboard."}

    user = (
        await db.execute(select(User).where(User.id == link.user_id))
    ).scalar_one_or_none()
    if user is None:
        return {"error": "Akun dashboard untuk kode ini tidak ditemukan."}

    # Collect all identifier variants of this sender for duplicate cleanup.
    variants: list[str] = []
    for raw in [*sender_ids, identity]:
        for candidate in (str(raw or "").strip(), normalize_phone(str(raw or ""))):
            if candidate and candidate not in variants:
                variants.append(candidate)

    # Archive auto-provisioned placeholder users that shadow the dashboard
    # account for this sender (usually a trial row keyed by the WA number).
    duplicates = (
        await db.execute(
            select(User).where(
                or_(User.external_id.in_(variants), User.phone_number.in_(variants)),
                User.id != user.id,
            )
        )
    ).scalars().all()
    archived: list[str] = []
    for dup in duplicates:
        dup_subs = (
            await db.execute(
                select(UserSubscription).where(UserSubscription.user_id == dup.id)
            )
        ).scalars().all()
        for sub in dup_subs:
            if sub.status in ("trial", "active", "grace_period"):
                sub.status = "merged"
        old_external = str(dup.external_id or "")
        dup.external_id = f"merged:{old_external}"[:64]
        dup.phone_number = None
        archived.append(str(dup.id))
        logger.info(
            "wa_link.placeholder_user_archived",
            duplicate_user_id=str(dup.id),
            merged_into=str(user.id),
        )

    user.phone_number = identity
    link.used_at = now
    link.claimed_identity = identity

    sub_row = (
        await db.execute(
            select(UserSubscription, SubscriptionPlan)
            .join(SubscriptionPlan, SubscriptionPlan.id == UserSubscription.plan_id)
            .where(UserSubscription.user_id == user.id)
        )
    ).first()
    plan_code = plan_label = sub_status = None
    if sub_row is not None:
        sub, plan = sub_row
        plan_code = plan.code
        plan_label = plan.label
        sub_status = sub.status

    await db.flush()
    logger.info(
        "wa_link.code_claimed",
        user_id=str(user.id),
        identity=identity,
        archived_duplicates=len(archived),
        plan_code=plan_code,
    )
    return {
        "success": True,
        "user_id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "linked_identity": identity,
        "plan_code": plan_code,
        "plan_label": plan_label,
        "subscription_status": sub_status,
        "archived_duplicate_users": archived,
    }

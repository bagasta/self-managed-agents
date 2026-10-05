"""Resolve verified external channel identities to stable User principals."""
from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.phone_utils import normalize_phone
from app.models.subscription import User


def arthur_ui_session_isolated_memory(session: object) -> bool:
    """Whether this is the local Arthur UI test chat, whose memory must not cross sessions."""
    metadata = getattr(session, "metadata_", None)
    channel_type = getattr(session, "channel_type", None)
    return (
        channel_type in (None, "api")
        and isinstance(metadata, dict)
        and metadata.get("source") == "arthur-ui"
        and metadata.get("memory_mode") == "isolated"
    )


def arthur_memory_scope(
    owner_user_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    *,
    isolate_session: bool = False,
) -> str:
    """Use an isolated session scope for UI testing; otherwise use verified owner memory."""
    if isolate_session:
        return f"session:{session_id}"
    if owner_user_id is not None:
        try:
            return f"user:{uuid.UUID(str(owner_user_id))}"
        except (TypeError, ValueError, AttributeError):
            pass
    return f"session:{session_id}"


def identity_variants(external_id: str | None) -> list[str]:
    raw = str(external_id or "").strip()
    normalized = normalize_phone(raw)
    values: list[str] = []
    for value in (raw, normalized, f"+{normalized}" if normalized else ""):
        if value and value not in values:
            values.append(value)
    return values


async def resolve_unique_user(
    db: AsyncSession, external_id: str | None
) -> User | None:
    """Return the unique matching user; ambiguity and missing identity fail closed."""
    variants = identity_variants(external_id)
    try:
        parsed_id = uuid.UUID(str(external_id)) if external_id else None
    except (TypeError, ValueError, AttributeError):
        parsed_id = None
    if not variants and parsed_id is None:
        return None
    clauses = []
    if variants:
        clauses.extend((User.external_id.in_(variants), User.phone_number.in_(variants), User.wa_lid.in_(variants)))
    if parsed_id is not None:
        clauses.append(User.id == parsed_id)
    result = await db.execute(
        select(User).where(or_(*clauses))
    )
    rows = {row.id: row for row in result.scalars().all()}
    return next(iter(rows.values())) if len(rows) == 1 else None


async def resolve_unique_user_id(
    db: AsyncSession, external_id: str | None
) -> uuid.UUID | None:
    user = await resolve_unique_user(db, external_id)
    return user.id if user is not None else None

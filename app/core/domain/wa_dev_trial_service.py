from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_DIGITS = "23456789"
_CODE_LENGTH = 6
_POLICY_KEY = "wa_dev_trial"


def normalize_wa_dev_trial_code(code: str | None) -> str:
    """Normalize a short WA trial code for lookup."""
    raw = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return raw[:_CODE_LENGTH]


def extract_wa_dev_trial_code(text: str | None) -> str:
    """Extract a standalone 6-char trial code from free-form WhatsApp text."""
    for token in str(text or "").upper().split():
        raw = "".join(ch for ch in token if ch.isalnum())
        if len(raw) == _CODE_LENGTH and any(ch.isdigit() for ch in raw):
            return raw
    return ""


def looks_like_wa_dev_trial_code(text: str | None) -> bool:
    return bool(extract_wa_dev_trial_code(text))


def _new_code() -> str:
    chars = [secrets.choice(_CODE_DIGITS)]
    chars.extend(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH - 1))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _trial_policy(agent: Agent) -> dict[str, Any]:
    policy = agent.safety_policy if isinstance(agent.safety_policy, dict) else {}
    trial = policy.get(_POLICY_KEY)
    return trial if isinstance(trial, dict) else {}


def get_agent_wa_dev_trial_code(agent: Agent) -> str:
    trial = _trial_policy(agent)
    if trial.get("enabled") is False:
        return ""
    return normalize_wa_dev_trial_code(trial.get("code"))


async def find_agent_by_wa_dev_trial_code(db: AsyncSession, code: str) -> Agent | None:
    normalized = normalize_wa_dev_trial_code(code)
    if len(normalized) != _CODE_LENGTH:
        return None

    result = await db.execute(select(Agent).where(Agent.is_deleted.is_(False)))
    for agent in result.scalars().all():
        if get_agent_wa_dev_trial_code(agent) == normalized:
            return agent
    return None


async def ensure_wa_dev_trial_code(
    db: AsyncSession,
    agent: Agent,
    *,
    force_new: bool = False,
) -> str:
    """Create or reuse the agent's reusable WA shared-number trial code."""
    if not force_new:
        existing = get_agent_wa_dev_trial_code(agent)
        if existing:
            return existing

    for _ in range(40):
        candidate = _new_code()
        collision = await find_agent_by_wa_dev_trial_code(db, candidate)
        if collision is None or collision.id == agent.id:
            break
    else:
        raise RuntimeError("Tidak bisa generate kode WA trial unik")

    policy = dict(agent.safety_policy or {})
    policy[_POLICY_KEY] = {
        "code": candidate,
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reusable": True,
    }
    agent.safety_policy = policy
    await db.flush()
    return candidate

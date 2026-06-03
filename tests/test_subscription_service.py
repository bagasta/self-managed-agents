"""TDD tests for subscription_service.py

Tests:
- get_or_create_wa_user: buat user baru, idempotent, subscription Trial otomatis
- check_can_create_agent: deteksi agent via owner_external_id DAN operator_ids (legacy)
- slot enforcement: Tier 1 max 1 agent
- WA user dapat UserApiKey otomatis
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — build minimal in-memory objects tanpa DB
# ---------------------------------------------------------------------------

def _make_plan(
    max_agents=1,
    token_quota=10_000_000,
    period_days=30,
    grace_period_days=3,
    code="tier_1",
    label="Starter",
    allowed_models=None,
    subagents_allowed=True,
    wa_connect=True,
):
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        code=code, label=label,
        max_agents=max_agents,
        token_quota=token_quota,
        period_days=period_days,
        grace_period_days=grace_period_days,
        allowed_models=["openai/gpt-4.1-mini"] if allowed_models is None else allowed_models,
        subagents_allowed=subagents_allowed,
        wa_connect=wa_connect,
    )


def _make_sub(status="active", token_quota=10_000_000, tokens_used=0, expires_at=None):
    now = datetime.now(timezone.utc)
    expires = expires_at or (now + timedelta(days=30))
    sub = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        plan_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        status=status,
        token_quota=token_quota,
        tokens_used=tokens_used,
        expires_at=expires,
        grace_until=expires + timedelta(days=3),
        is_usable=status in ("trial", "active", "grace_period"),
        tokens_remaining=max(0, token_quota - tokens_used),
    )
    return sub


def _make_agent(owner_external_id=None, operator_ids=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        owner_external_id=owner_external_id,
        operator_ids=operator_ids or [],
        is_deleted=False,
    )


# ---------------------------------------------------------------------------
# Tests: get_or_create_wa_user
# ---------------------------------------------------------------------------

def test_default_tier3_plan_has_unlimited_agents_and_100m_tokens():
    from app.core.domain.subscription_service import DEFAULT_SUBSCRIPTION_PLANS

    tier3 = next(plan for plan in DEFAULT_SUBSCRIPTION_PLANS if plan["code"] == "tier_3")

    assert tier3["max_agents"] is None
    assert tier3["token_quota"] == 100_000_000


class TestGetOrCreateWaUser:
    @pytest.mark.asyncio
    async def test_creates_new_user_and_trial_sub(self):
        """Nomor baru → user baru + Trial subscription + UserApiKey."""
        from app.core.domain import subscription_service
        from app.core.domain.subscription_service import get_or_create_wa_user

        plan = _make_plan()
        created_types = []

        # Patch _create_trial_subscription supaya tidak perlu query plan ke DB
        async def _fake_create_sub(user_id, db):
            sub = _make_sub(status="trial")
            sub.user_id = user_id
            db.add(SimpleNamespace(__class__=type("UserSubscription", (), {})))
            return sub

        # execute: cari user → None, cari sub → None, cari api_key → None
        results = [
            MagicMock(**{"scalar_one_or_none.return_value": None}),
            MagicMock(**{"scalar_one_or_none.return_value": None}),
            MagicMock(**{"scalar_one_or_none.return_value": None}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)
        mock_db.flush = AsyncMock()
        mock_db.add = MagicMock(side_effect=lambda obj: created_types.append(type(obj).__name__))

        with (
            patch.object(subscription_service, "ensure_default_subscription_plans", AsyncMock()),
            patch.object(subscription_service, "_create_trial_subscription", _fake_create_sub),
        ):
            user, sub = await get_or_create_wa_user("628111", mock_db)

        assert user.external_id == "628111"
        assert sub.status == "trial"
        assert "User" in created_types
        assert "UserApiKey" in created_types

    @pytest.mark.asyncio
    async def test_idempotent_existing_user(self):
        """Nomor sudah ada + punya sub + punya api_key → tidak buat apapun."""
        from app.core.domain import subscription_service
        from app.core.domain.subscription_service import get_or_create_wa_user

        existing_user = SimpleNamespace(id=uuid.uuid4(), external_id="628111", has_used_trial=True)
        existing_sub = _make_sub()
        existing_key = MagicMock()

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": existing_user}),
            MagicMock(**{"scalar_one_or_none.return_value": existing_sub}),
            MagicMock(**{"scalar_one_or_none.return_value": existing_key}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)
        mock_db.flush = AsyncMock()

        with patch.object(subscription_service, "ensure_default_subscription_plans", AsyncMock()):
            user, sub = await get_or_create_wa_user("628111", mock_db)

        assert user is existing_user
        assert sub is existing_sub
        mock_db.add.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: check_can_create_agent — deteksi agent lama (operator_ids fallback)
# ---------------------------------------------------------------------------

class TestCheckCanCreateAgent:
    @pytest.mark.asyncio
    async def test_blocks_when_slot_full_via_owner_external_id(self):
        """Agent dengan owner_external_id terisi → slot penuh → blocked."""
        from app.core.domain.subscription_service import check_can_create_agent

        user = SimpleNamespace(id=uuid.uuid4(), external_id="628111")
        sub = _make_sub()
        plan = _make_plan(max_agents=1)

        existing_agent = _make_agent(owner_external_id="628111")

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": user}),
            MagicMock(**{"scalar_one_or_none.return_value": sub}),
            MagicMock(**{"scalar_one.return_value": plan}),
            MagicMock(**{"scalars.return_value.all.return_value": [existing_agent]}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)

        result = await check_can_create_agent("628111", mock_db)

        assert result["allowed"] is False
        assert "upgrade" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_blocks_when_slot_full_via_operator_ids_legacy(self):
        """Agent lama (owner_external_id=None, tapi ada di operator_ids) → slot penuh → blocked."""
        from app.core.domain.subscription_service import check_can_create_agent

        user = SimpleNamespace(id=uuid.uuid4(), external_id="628111")
        sub = _make_sub()
        plan = _make_plan(max_agents=1)

        # Agent lama: owner_external_id kosong, tapi nomor ada di operator_ids
        legacy_agent = _make_agent(owner_external_id=None, operator_ids=["628111"])

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": user}),
            MagicMock(**{"scalar_one_or_none.return_value": sub}),
            MagicMock(**{"scalar_one.return_value": plan}),
            MagicMock(**{"scalars.return_value.all.return_value": [legacy_agent]}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)

        result = await check_can_create_agent("628111", mock_db)

        assert result["allowed"] is False
        assert "upgrade" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_allows_when_no_agents(self):
        """Belum punya agent → boleh buat."""
        from app.core.domain.subscription_service import check_can_create_agent

        user = SimpleNamespace(id=uuid.uuid4(), external_id="628111")
        sub = _make_sub()
        plan = _make_plan(max_agents=1)

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": user}),
            MagicMock(**{"scalar_one_or_none.return_value": sub}),
            MagicMock(**{"scalar_one.return_value": plan}),
            MagicMock(**{"scalars.return_value.all.return_value": []}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)

        result = await check_can_create_agent("628111", mock_db)

        assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_unlimited_agents_for_enterprise(self):
        """Tier 3 max_agents=None → selalu boleh buat."""
        from app.core.domain.subscription_service import check_can_create_agent

        user = SimpleNamespace(id=uuid.uuid4(), external_id="628111")
        sub = _make_sub()
        plan = _make_plan(max_agents=None, code="tier_3", label="Enterprise")

        # Sudah punya 5 agent
        agents = [_make_agent(owner_external_id="628111") for _ in range(5)]

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": user}),
            MagicMock(**{"scalar_one_or_none.return_value": sub}),
            MagicMock(**{"scalar_one.return_value": plan}),
            MagicMock(**{"scalars.return_value.all.return_value": agents}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)

        result = await check_can_create_agent("628111", mock_db)

        assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_blocks_expired_subscription(self):
        """Subscription expired → tidak boleh buat agent."""
        from app.core.domain.subscription_service import check_can_create_agent

        user = SimpleNamespace(id=uuid.uuid4(), external_id="628111")
        sub = _make_sub(status="expired")
        sub.is_usable = False
        plan = _make_plan()

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": user}),
            MagicMock(**{"scalar_one_or_none.return_value": sub}),
            MagicMock(**{"scalar_one.return_value": plan}),
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)

        result = await check_can_create_agent("628111", mock_db)

        assert result["allowed"] is False
        assert "expired" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Tests: WA user dapat UserApiKey otomatis
# ---------------------------------------------------------------------------

class TestWaUserApiKey:
    @pytest.mark.asyncio
    async def test_existing_user_no_duplicate_api_key(self):
        """User yang sudah punya api_key → tidak buat key baru."""
        from app.core.domain import subscription_service
        from app.core.domain.subscription_service import get_or_create_wa_user

        existing_user = SimpleNamespace(id=uuid.uuid4(), external_id="628111", has_used_trial=True)
        existing_sub = _make_sub()
        existing_key = MagicMock()  # sudah ada

        results = [
            MagicMock(**{"scalar_one_or_none.return_value": existing_user}),  # user ada
            MagicMock(**{"scalar_one_or_none.return_value": existing_sub}),   # sub ada
            MagicMock(**{"scalar_one_or_none.return_value": existing_key}),   # key sudah ada
        ]
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=results)
        mock_db.flush = AsyncMock()

        created_types = []
        mock_db.add.side_effect = lambda obj: created_types.append(type(obj).__name__)

        with patch.object(subscription_service, "ensure_default_subscription_plans", AsyncMock()):
            await get_or_create_wa_user("628111", mock_db)

        assert "UserApiKey" not in created_types


class TestAgentEntitlements:
    def test_blocks_unavailable_model(self):
        from app.core.domain.subscription_service import validate_agent_entitlements

        plan = _make_plan(allowed_models=["openai/gpt-4.1-mini"])

        violations = validate_agent_entitlements(
            plan,
            model="anthropic/claude-sonnet-4-6",
            tools_config={},
            channel_type=None,
        )

        assert any("Model" in item for item in violations)

    def test_blocks_subagents_when_plan_disallows(self):
        from app.core.domain.subscription_service import validate_agent_entitlements

        plan = _make_plan(subagents_allowed=False)

        violations = validate_agent_entitlements(
            plan,
            model="openai/gpt-4.1-mini",
            tools_config={"subagents": {"enabled": True}},
            channel_type=None,
        )

        assert any("sub-agent" in item for item in violations)

    def test_allows_enterprise_empty_model_allowlist(self):
        from app.core.domain.subscription_service import validate_agent_entitlements

        plan = _make_plan(allowed_models=[], label="Enterprise")

        violations = validate_agent_entitlements(
            plan,
            model="anthropic/claude-sonnet-4-6",
            tools_config={},
            channel_type=None,
        )

        assert violations == []

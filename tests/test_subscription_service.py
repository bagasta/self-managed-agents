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


@pytest.mark.asyncio
async def test_get_best_subscription_prefers_active_paid_over_trial_duplicate():
    from app.core.domain.subscription_service import get_best_subscription_by_external_ids

    db = MagicMock()
    trial_user = SimpleNamespace(id=uuid.uuid4(), external_id="74350933852232", phone_number=None)
    trial_sub = SimpleNamespace(status="trial", is_usable=True)
    trial_plan = SimpleNamespace(code="trial", is_trial=True)
    paid_user = SimpleNamespace(id=uuid.uuid4(), external_id="62895619356936", phone_number="62895619356936")
    paid_sub = SimpleNamespace(status="active", is_usable=True)
    paid_plan = SimpleNamespace(code="tier_3", is_trial=False)

    result = MagicMock()
    result.all.return_value = [
        (trial_user, trial_sub, trial_plan),
        (paid_user, paid_sub, paid_plan),
    ]
    db.execute = AsyncMock(return_value=result)

    user, sub, plan = await get_best_subscription_by_external_ids(
        ["74350933852232", "62895619356936"],
        db,
    )

    assert user is paid_user
    assert sub is paid_sub
    assert plan is paid_plan


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


# ---------------------------------------------------------------------------
# B1: assert_token_quota_available / QuotaExceeded
# ---------------------------------------------------------------------------

class _Sub:
    def __init__(self, used, quota, grace_until=None, status="active"):
        self.tokens_used = used
        self.token_quota = quota
        self.grace_until = grace_until
        self.status = status


def test_quota_allows_when_under_limit():
    from app.core.domain.subscription_service import assert_token_quota_available
    assert_token_quota_available(_Sub(used=10, quota=100))  # no raise


def test_quota_blocks_when_at_or_over_limit():
    from app.core.domain.subscription_service import assert_token_quota_available, QuotaExceeded
    with pytest.raises(QuotaExceeded):
        assert_token_quota_available(_Sub(used=100, quota=100))


def test_quota_none_quota_is_unlimited():
    from app.core.domain.subscription_service import assert_token_quota_available
    assert_token_quota_available(_Sub(used=10**12, quota=None))  # tier_3 unlimited


def test_quota_grace_until_in_future_allows():
    from app.core.domain.subscription_service import assert_token_quota_available
    future = datetime.now(timezone.utc) + timedelta(days=7)
    assert_token_quota_available(_Sub(used=100, quota=100, grace_until=future))  # no raise


def test_quota_grace_until_in_past_blocks():
    from app.core.domain.subscription_service import assert_token_quota_available, QuotaExceeded
    past = datetime.now(timezone.utc) - timedelta(days=1)
    with pytest.raises(QuotaExceeded):
        assert_token_quota_available(_Sub(used=100, quota=100, grace_until=past))


# ---------------------------------------------------------------------------
# B1 integration: over-quota path returns blocked reply WITHOUT LLM call
# ---------------------------------------------------------------------------

class TestTokenQuotaPreRunGate:
    """Integration-style test: over-quota subscription blocks run before LLM."""

    def _make_agent(self):
        import uuid
        from unittest.mock import MagicMock
        a = MagicMock()
        a.id = uuid.uuid4()
        a.name = "TestAgent"
        a.model = "openai/gpt-4.1-mini"
        a.temperature = 0.7
        a.tools_config = {}
        a.sandbox_config = {}
        a.safety_policy = {}
        a.escalation_config = {}
        a.operator_ids = []
        a.capabilities = []
        a.is_deleted = False
        a.api_key = "ak-test"
        a.token_quota = 1000
        a.tokens_used = 0
        a.active_until = None
        a.owner_external_id = "owner-123"
        a.wa_device_id = None
        a.allowed_senders = None
        a.created_by = None
        return a

    def _make_session(self, agent_id):
        import uuid
        from unittest.mock import MagicMock
        s = MagicMock()
        s.id = uuid.uuid4()
        s.agent_id = agent_id
        s.external_user_id = "user-456"
        s.channel_type = None
        s.channel_config = {}
        s.metadata_ = {}
        return s

    def _make_db(self):
        from unittest.mock import AsyncMock, MagicMock
        db = MagicMock()
        db.execute = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_over_quota_subscription_blocks_without_llm(self):
        """Over-quota owner subscription must return blocked reply; LLM must NOT be called."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from types import SimpleNamespace

        agent = self._make_agent()
        session = self._make_session(agent.id)
        db = self._make_db()

        # Subscription that is over quota
        over_quota_sub = _Sub(used=5_000_000, quota=5_000_000)

        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none = MagicMock(return_value=None)
        db.execute.return_value = mock_scalar

        with (
            patch(
                "app.core.engine.agent_runner.get_owner_subscription",
                new=AsyncMock(return_value=(MagicMock(), over_quota_sub)),
            ),
            patch("app.core.engine.agent_runner.is_quota_exempt_builder_agent", return_value=False),
            patch("app.core.engine.agent_runner.build_agent_llms") as mock_llm,
            patch("app.core.engine.agent_runner.handle_pending_interrupt", new=AsyncMock(return_value=None)),
        ):
            from app.core.engine.agent_runner import run_agent
            result = await run_agent(
                agent_model=agent,
                session=session,
                user_message="Halo",
                db=db,
            )

        # LLM must NOT be called
        mock_llm.assert_not_called()

        # Reply must indicate quota exhausted
        assert "kuota" in result["reply"].lower() or "quota" in result["reply"].lower()
        assert result["tokens_used"] == 0

    @pytest.mark.asyncio
    async def test_quota_lookup_error_allows_run(self):
        """DB error during quota lookup must NOT block a legitimate run; build_agent_llms IS reached."""
        from unittest.mock import AsyncMock, MagicMock, patch

        agent = self._make_agent()
        session = self._make_session(agent.id)
        db = self._make_db()

        mock_scalar = MagicMock()
        mock_scalar.scalar_one_or_none = MagicMock(return_value=None)
        db.execute.return_value = mock_scalar

        # Simulate a DB OperationalError during quota lookup
        with (
            patch(
                "app.core.engine.agent_runner.get_owner_subscription",
                new=AsyncMock(side_effect=Exception("DB connection lost")),
            ),
            patch("app.core.engine.agent_runner.is_quota_exempt_builder_agent", return_value=False),
            patch("app.core.engine.agent_runner.build_agent_llms") as mock_llm,
            patch("app.core.engine.agent_runner.handle_pending_interrupt", new=AsyncMock(return_value=None)),
        ):
            mock_llm.side_effect = Exception("stop here")  # stop after quota gate
            from app.core.engine.agent_runner import run_agent
            try:
                await run_agent(
                    agent_model=agent,
                    session=session,
                    user_message="Halo",
                    db=db,
                )
            except Exception:
                pass  # expected — we just want to confirm build_agent_llms was reached

        # build_agent_llms MUST have been called (run was not blocked by quota lookup failure)
        mock_llm.assert_called_once()

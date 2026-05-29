from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, *results):
        self.results = list(results)
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        if self.results:
            return _ScalarResult(self.results.pop(0))
        return _ScalarResult(None)

    async def flush(self):
        pass

    async def commit(self):
        pass


def _agent(**overrides):
    values = {
        "active_until": datetime.now(timezone.utc) + timedelta(days=1),
        "tokens_used": 0,
        "token_quota": 10_000_000,
        "owner_external_id": None,
        "capabilities": [],
        "tools_config": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_agent_quota_blocks_exhausted_agent_without_llm_run():
    from app.core.domain.agent_quota_service import check_agent_quota

    db = _FakeDB()
    result = await check_agent_quota(
        _agent(tokens_used=10_000_000, token_quota=10_000_000),
        db,
    )

    assert result.allowed is False
    assert result.reason == "agent_token_quota_exhausted"
    assert "10,000,000 / 10,000,000" in result.detail
    assert db.executed == 0


@pytest.mark.asyncio
async def test_agent_quota_blocks_expired_agent():
    from app.core.domain.agent_quota_service import check_agent_quota

    result = await check_agent_quota(
        _agent(active_until=datetime.now(timezone.utc) - timedelta(seconds=1)),
        _FakeDB(),
    )

    assert result.allowed is False
    assert result.reason == "agent_subscription_expired"


@pytest.mark.asyncio
async def test_zero_token_quota_still_means_unlimited_for_custom_agents():
    from app.core.domain.agent_quota_service import check_agent_quota

    result = await check_agent_quota(
        _agent(tokens_used=999_999_999, token_quota=0),
        _FakeDB(),
    )

    assert result.allowed is True


@pytest.mark.asyncio
async def test_owner_subscription_quota_is_also_enforced():
    from app.core.domain.agent_quota_service import check_agent_quota

    user = SimpleNamespace(id="user-1")
    subscription = SimpleNamespace(
        is_usable=True,
        tokens_used=10_000_000,
        token_quota=10_000_000,
    )
    result = await check_agent_quota(
        _agent(owner_external_id="628owner"),
        _FakeDB(user, subscription),
    )

    assert result.allowed is False
    assert result.reason == "owner_subscription_token_quota_exhausted"


@pytest.mark.asyncio
async def test_builder_agent_bypasses_agent_and_owner_quota_gates():
    from app.core.domain.agent_quota_service import check_agent_quota

    result = await check_agent_quota(
        _agent(
            active_until=datetime.now(timezone.utc) - timedelta(days=1),
            tokens_used=10_000_000,
            token_quota=10_000_000,
            owner_external_id="628owner",
            capabilities=["system", "builder"],
            tools_config={"builder": True},
        ),
        _FakeDB(),
    )

    assert result.allowed is True


@pytest.mark.asyncio
async def test_record_agent_token_usage_updates_agent_and_owner_subscription():
    from app.core.domain.agent_quota_service import record_agent_token_usage

    agent = _agent(owner_external_id="628owner", tokens_used=100)
    user = SimpleNamespace(id="user-1")
    subscription = SimpleNamespace(tokens_used=200)

    await record_agent_token_usage(agent, 50, _FakeDB(user, subscription))

    assert agent.tokens_used == 150
    assert subscription.tokens_used == 250


@pytest.mark.asyncio
async def test_builder_agent_token_usage_is_not_recorded_or_charged_to_owner():
    from app.core.domain.agent_quota_service import record_agent_token_usage

    agent = _agent(
        owner_external_id="628owner",
        tokens_used=100,
        capabilities=["system", "builder"],
        tools_config={"builder": True},
    )
    db = _FakeDB(SimpleNamespace(id="user-1"), SimpleNamespace(tokens_used=200))

    await record_agent_token_usage(agent, 50, db)

    assert agent.tokens_used == 100
    assert db.executed == 0

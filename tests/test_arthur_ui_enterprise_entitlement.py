from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import sessions as sessions_api
from app.config import get_settings
from app.models.subscription import SubscriptionPlan, User, UserSubscription
from app.schemas.session import SessionCreate


def _payload(**overrides):
    values = {
        "external_user_id": "clevio-arthur-ui-enterprise-test",
        "channel_type": "api",
        "metadata": {
            "source": "arthur-ui",
            "memory_mode": "isolated",
            "test_plan": "enterprise",
        },
    }
    values.update(overrides)
    return SessionCreate(**values)


def test_enterprise_test_marker_requires_reserved_isolated_arthur_ui_session():
    assert sessions_api._is_arthur_ui_enterprise_test(_payload())
    assert not sessions_api._is_arthur_ui_enterprise_test(
        _payload(external_user_id="628123456789")
    )
    assert not sessions_api._is_arthur_ui_enterprise_test(
        _payload(metadata={"source": "arthur-ui", "memory_mode": "isolated"})
    )
    assert not sessions_api._is_arthur_ui_enterprise_test(
        _payload(channel_type="whatsapp")
    )


@pytest.mark.asyncio
async def test_enterprise_test_owner_isolated_and_subscription_is_unlimited(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "development")

    async def skip_plan_seed(_db):
        return None

    monkeypatch.setattr(
        "app.core.domain.subscription_service.ensure_default_subscription_plans",
        skip_plan_seed,
    )

    enterprise = SimpleNamespace(
        id=SubscriptionPlan.TIER_3_ID,
        token_quota=100_000_000,
        max_agents=None,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar_one(self):
            return self.value

    class FakeDB:
        def __init__(self):
            self.added = []

        async def execute(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            if entity is SubscriptionPlan:
                return Result(enterprise)
            return Result(None)

        def add(self, value):
            self.added.append(value)
            if isinstance(value, User) and value.id is None:
                value.id = "test-owner-id"

        async def flush(self):
            return None

    db = FakeDB()
    await sessions_api._ensure_arthur_ui_enterprise_test_owner(db)

    user = next(value for value in db.added if isinstance(value, User))
    subscription = next(value for value in db.added if isinstance(value, UserSubscription))
    assert user.external_id == "clevio-arthur-ui-enterprise-test"
    assert subscription.user_id == "test-owner-id"
    assert subscription.plan_id == SubscriptionPlan.TIER_3_ID
    assert subscription.status == "active"
    assert subscription.expires_at is None


@pytest.mark.asyncio
async def test_enterprise_test_owner_is_denied_outside_development(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")

    with pytest.raises(HTTPException) as exc:
        await sessions_api._ensure_arthur_ui_enterprise_test_owner(object())

    assert exc.value.status_code == 403

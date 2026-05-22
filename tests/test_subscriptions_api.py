"""TDD tests for Subscription API endpoints.

Endpoints tested:
  GET  /v1/subscriptions/plans
  POST /v1/subscriptions/{user_id}/activate
  POST /v1/subscriptions/{user_id}/upgrade
  POST /v1/subscriptions/{user_id}/topup
  GET  /v1/subscriptions/{user_id}

Uses FastAPI TestClient with real PostgreSQL (via asyncpg) — PostgreSQL required
so that JSONB and UUID types work correctly.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as _sync_create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker as _sync_sessionmaker
from sqlalchemy.pool import NullPool

load_dotenv()

_ASYNC_URL = os.environ["DATABASE_URL"]
_SYNC_URL = _ASYNC_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

from app.database import get_db
from app.main import app
from app.models.subscription import SubscriptionPlan, User, UserSubscription, TokenTopup

# NullPool: each request gets a fresh connection — no pool contention between tests
async_engine = create_async_engine(_ASYNC_URL, poolclass=NullPool)
AsyncTestSession = async_sessionmaker(async_engine, expire_on_commit=False)

sync_engine = _sync_create_engine(_SYNC_URL)
SyncTestSession = _sync_sessionmaker(bind=sync_engine)


async def _override_get_db():
    async with AsyncTestSession() as db:
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


ADMIN_KEY = "test-admin-key"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_user(user_id: str) -> None:
    """Insert a minimal user row so FK constraints pass."""
    uid = uuid.UUID(user_id)
    with SyncTestSession() as db:
        if db.query(User).filter_by(id=uid).first():
            return
        db.add(User(
            id=uid,
            email=f"test-{user_id[:8]}@example.com",
            password_hash="x",
            external_id=user_id[:16],
        ))
        db.commit()


def _clear_test_subscriptions(user_ids: list[str]) -> None:
    """Remove topups + subscriptions + users for test users."""
    if not user_ids:
        return
    uuids = [uuid.UUID(u) for u in user_ids]
    with SyncTestSession() as db:
        db.query(TokenTopup).filter(TokenTopup.user_id.in_(uuids)).delete(synchronize_session=False)
        db.query(UserSubscription).filter(UserSubscription.user_id.in_(uuids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(uuids)).delete(synchronize_session=False)
        db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_admin_key(monkeypatch):
    from app import config
    s = config.get_settings()
    monkeypatch.setattr(s, "api_key", ADMIN_KEY)
    import app.deps as deps_mod
    monkeypatch.setattr(deps_mod, "settings", s)
    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def user_id():
    uid = str(uuid.uuid4())
    _insert_user(uid)
    yield uid
    _clear_test_subscriptions([uid])


@pytest.fixture
def two_users():
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
    _insert_user(u1)
    _insert_user(u2)
    yield u1, u2
    _clear_test_subscriptions([u1, u2])


# ---------------------------------------------------------------------------
# GET /v1/subscriptions/plans
# ---------------------------------------------------------------------------

class TestListPlans:
    def test_returns_all_active_plans(self, client):
        r = client.get("/v1/subscriptions/plans", headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 4

    def test_plan_fields_present(self, client):
        r = client.get("/v1/subscriptions/plans", headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200
        plan = next(p for p in r.json() if p["code"] == "trial")
        assert plan["label"] == "Trial"
        assert plan["is_trial"] is True
        assert plan["token_quota"] > 0

    def test_all_plan_codes_present(self, client):
        r = client.get("/v1/subscriptions/plans", headers={"X-API-Key": ADMIN_KEY})
        codes = {p["code"] for p in r.json()}
        assert {"trial", "tier_1", "tier_2", "tier_3"}.issubset(codes)

    def test_requires_admin_key(self, client):
        r = client.get("/v1/subscriptions/plans")
        assert r.status_code == 422

    def test_rejects_wrong_key(self, client):
        r = client.get("/v1/subscriptions/plans", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_plans_ordered_by_token_quota(self, client):
        r = client.get("/v1/subscriptions/plans", headers={"X-API-Key": ADMIN_KEY})
        quotas = [p["token_quota"] for p in r.json()]
        assert quotas == sorted(quotas)


# ---------------------------------------------------------------------------
# POST /v1/subscriptions/{user_id}/activate
# ---------------------------------------------------------------------------

class TestActivateSubscription:
    def test_activate_trial(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "trial"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["plan_code"] == "trial"
        assert data["subscription_status"] == "trial"
        assert data["tokens_used"] == 0
        assert data["tokens_remaining"] == data["token_quota"]

    def test_activate_paid_plan(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "tier_1"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["subscription_status"] == "active"
        assert data["expires_at"] is not None

    def test_trial_no_expiry(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "trial"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 201
        # trial has period_days=None → no expiry
        assert r.json()["expires_at"] is None

    def test_cannot_activate_twice(self, client, user_id):
        client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "trial"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "trial"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 409
        assert "sudah memiliki subscription" in r.json()["detail"]

    def test_invalid_plan_code(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "tier_99"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 404

    def test_requires_admin_key(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "trial"},
        )
        assert r.status_code == 422

    def test_response_contains_plan_meta(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "tier_2"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        data = r.json()
        assert "subagents_allowed" in data
        assert "wa_connect" in data
        assert "max_agents" in data


# ---------------------------------------------------------------------------
# POST /v1/subscriptions/{user_id}/upgrade
# ---------------------------------------------------------------------------

class TestUpgradeSubscription:
    def _activate(self, client, user_id, plan="trial"):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": plan},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 201

    def test_upgrade_from_trial_to_tier1(self, client, user_id):
        self._activate(client, user_id, "trial")
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_1", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["previous_plan"] == "trial"
        assert data["new_plan"] == "tier_1"
        assert data["subscription_status"] == "active"

    def test_upgrade_resets_tokens_used(self, client, user_id):
        self._activate(client, user_id, "trial")
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_2", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 200
        assert r.json()["tokens_used"] == 0
        assert r.json()["tokens_remaining"] == r.json()["token_quota"]

    def test_upgrade_sets_expiry(self, client, user_id):
        self._activate(client, user_id, "trial")
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_1", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 200
        assert r.json()["expires_at"] is not None

    def test_upgrade_to_trial_rejected(self, client, user_id):
        self._activate(client, user_id, "tier_1")
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "trial", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 400

    def test_duplicate_reference_id_rejected(self, client, two_users):
        u1, u2 = two_users
        ref = f"INV-DUP-{uuid.uuid4().hex[:8]}"
        self._activate(client, u1, "trial")
        client.post(
            f"/v1/subscriptions/{u1}/upgrade",
            json={"plan_code": "tier_1", "reference_id": ref},
            headers={"X-API-Key": ADMIN_KEY},
        )
        self._activate(client, u2, "trial")
        r = client.post(
            f"/v1/subscriptions/{u2}/upgrade",
            json={"plan_code": "tier_1", "reference_id": ref},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 409

    def test_upgrade_user_without_subscription(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_1", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 404

    def test_upgrade_invalid_plan_code(self, client, user_id):
        self._activate(client, user_id, "trial")
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_99", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 404

    def test_reference_id_returned_in_response(self, client, user_id):
        self._activate(client, user_id, "trial")
        ref = f"INV-REF-{uuid.uuid4().hex[:8]}"
        r = client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_1", "reference_id": ref},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 200
        assert r.json()["reference_id"] == ref

    def test_upgrade_then_get_status_reflects_new_plan(self, client, user_id):
        self._activate(client, user_id, "trial")
        client.post(
            f"/v1/subscriptions/{user_id}/upgrade",
            json={"plan_code": "tier_2", "reference_id": f"INV-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.get(f"/v1/subscriptions/{user_id}", headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200
        assert r.json()["plan_code"] == "tier_2"


# ---------------------------------------------------------------------------
# POST /v1/subscriptions/{user_id}/topup
# ---------------------------------------------------------------------------

class TestTopupTokens:
    def _activate(self, client, user_id, plan="tier_1"):
        r = client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": plan},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 201

    def test_topup_increases_quota(self, client, user_id):
        self._activate(client, user_id)
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 1_000_000, "reference_id": f"TOP-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["tokens_added"] == 1_000_000
        assert data["token_quota_after"] == data["token_quota_before"] + 1_000_000

    def test_topup_idempotent_reference_id(self, client, user_id):
        self._activate(client, user_id)
        ref = f"TOP-IDEM-{uuid.uuid4().hex[:8]}"
        client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 500_000, "reference_id": ref},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 500_000, "reference_id": ref},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 409

    def test_topup_reactivates_grace_period(self, client, user_id):
        self._activate(client, user_id)
        # Force grace_period status
        with SyncTestSession() as db:
            sub = db.query(UserSubscription).filter_by(user_id=uuid.UUID(user_id)).first()
            sub.status = "grace_period"
            db.commit()
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 1_000_000, "reference_id": f"TOP-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 200
        assert r.json()["subscription_status"] == "active"

    def test_topup_expired_subscription_rejected(self, client, user_id):
        self._activate(client, user_id)
        with SyncTestSession() as db:
            sub = db.query(UserSubscription).filter_by(user_id=uuid.UUID(user_id)).first()
            sub.status = "expired"
            db.commit()
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 1_000_000, "reference_id": f"TOP-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 400

    def test_topup_without_subscription(self, client, user_id):
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 1_000_000, "reference_id": f"TOP-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 404

    def test_tokens_must_be_positive(self, client, user_id):
        self._activate(client, user_id)
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 0, "reference_id": f"TOP-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 422

    def test_reference_id_required(self, client, user_id):
        self._activate(client, user_id)
        r = client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 1_000_000},
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/subscriptions/{user_id}
# ---------------------------------------------------------------------------

class TestGetSubscriptionStatus:
    def test_get_active_subscription(self, client, user_id):
        client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "tier_1"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.get(f"/v1/subscriptions/{user_id}", headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200
        data = r.json()
        assert data["plan_code"] == "tier_1"
        assert data["plan_label"] == "Starter"
        assert data["subscription_status"] == "active"
        assert data["tokens_remaining"] == data["token_quota"]

    def test_get_trial_subscription(self, client, user_id):
        client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "trial"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.get(f"/v1/subscriptions/{user_id}", headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200
        assert r.json()["subscription_status"] == "trial"

    def test_get_nonexistent_user_returns_404(self, client):
        r = client.get(
            f"/v1/subscriptions/{uuid.uuid4()}",
            headers={"X-API-Key": ADMIN_KEY},
        )
        assert r.status_code == 404

    def test_tokens_remaining_reflects_topup(self, client, user_id):
        client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "tier_1"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        before = client.get(
            f"/v1/subscriptions/{user_id}", headers={"X-API-Key": ADMIN_KEY}
        ).json()["token_quota"]
        client.post(
            f"/v1/subscriptions/{user_id}/topup",
            json={"tokens": 500_000, "reference_id": f"TOP-{uuid.uuid4().hex[:8]}"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.get(f"/v1/subscriptions/{user_id}", headers={"X-API-Key": ADMIN_KEY})
        assert r.status_code == 200
        assert r.json()["token_quota"] == before + 500_000

    def test_plan_meta_fields_present(self, client, user_id):
        client.post(
            f"/v1/subscriptions/{user_id}/activate",
            json={"plan_code": "tier_2"},
            headers={"X-API-Key": ADMIN_KEY},
        )
        r = client.get(f"/v1/subscriptions/{user_id}", headers={"X-API-Key": ADMIN_KEY})
        data = r.json()
        assert "max_agents" in data
        assert "subagents_allowed" in data
        assert "wa_connect" in data
        assert "grace_until" in data

    def test_requires_admin_key(self, client, user_id):
        r = client.get(f"/v1/subscriptions/{user_id}")
        assert r.status_code == 422

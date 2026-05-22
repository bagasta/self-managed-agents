"""TDD tests for User management endpoints.

  POST  /v1/users              — buat user baru (+ opsional assign plan)
  GET   /v1/users/{user_id}    — profil + subscription
  PATCH /v1/users/{user_id}    — update profil

Uses real PostgreSQL (asyncpg + NullPool).
"""
from __future__ import annotations

import os
import uuid

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
from app.models.subscription import User, UserSubscription, TokenTopup

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


def _headers():
    return {"X-API-Key": ADMIN_KEY}


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


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=True)


def _unique_email():
    return f"test-{uuid.uuid4().hex[:8]}@example.com"


def _cleanup_user(email: str):
    with SyncTestSession() as db:
        user = db.query(User).filter_by(email=email).first()
        if user:
            db.query(TokenTopup).filter_by(user_id=user.id).delete()
            db.query(UserSubscription).filter_by(user_id=user.id).delete()
            db.query(User).filter_by(id=user.id).delete()
            db.commit()


# ---------------------------------------------------------------------------
# POST /v1/users
# ---------------------------------------------------------------------------

class TestCreateUser:
    def test_create_user_minimal(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
        }, headers=_headers())
        assert r.status_code == 201
        data = r.json()
        assert data["email"] == email
        assert "id" in data
        assert data["subscription"] is None
        _cleanup_user(email)

    def test_create_user_with_trial_plan(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
            "plan_code": "trial",
        }, headers=_headers())
        assert r.status_code == 201
        data = r.json()
        assert data["subscription"] is not None
        assert data["subscription"]["plan_code"] == "trial"
        assert data["subscription"]["status"] == "trial"
        assert data["has_used_trial"] is True
        _cleanup_user(email)

    def test_create_user_with_tier1_plan(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
            "plan_code": "tier_1",
        }, headers=_headers())
        assert r.status_code == 201
        data = r.json()
        assert data["subscription"]["plan_code"] == "tier_1"
        assert data["subscription"]["status"] == "active"
        _cleanup_user(email)

    def test_create_user_with_full_name(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
            "full_name": "Budi Santoso",
        }, headers=_headers())
        assert r.status_code == 201
        assert r.json()["full_name"] == "Budi Santoso"
        _cleanup_user(email)

    def test_create_user_with_custom_external_id(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
            "external_id": "wa_628111222333",
        }, headers=_headers())
        assert r.status_code == 201
        assert r.json()["external_id"] == "wa_628111222333"
        _cleanup_user(email)

    def test_duplicate_email_returns_409(self, client):
        email = _unique_email()
        client.post("/v1/users", json={"email": email, "password": "pass12345"}, headers=_headers())
        r = client.post("/v1/users", json={"email": email, "password": "pass12345"}, headers=_headers())
        assert r.status_code == 409
        _cleanup_user(email)

    def test_invalid_plan_code_returns_404(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
            "plan_code": "plan_xyz_invalid",
        }, headers=_headers())
        assert r.status_code == 404
        _cleanup_user(email)

    def test_invalid_email_returns_422(self, client):
        r = client.post("/v1/users", json={
            "email": "not-an-email",
            "password": "securepass123",
        }, headers=_headers())
        assert r.status_code == 422

    def test_short_password_returns_422(self, client):
        r = client.post("/v1/users", json={
            "email": _unique_email(),
            "password": "short",
        }, headers=_headers())
        assert r.status_code == 422

    def test_requires_admin_key(self, client):
        r = client.post("/v1/users", json={
            "email": _unique_email(),
            "password": "securepass123",
        }, headers={"X-API-Key": "wrong-key"})
        assert r.status_code in (401, 403)

    def test_external_id_auto_generated_if_not_provided(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={"email": email, "password": "securepass123"}, headers=_headers())
        assert r.status_code == 201
        assert r.json()["external_id"] != ""
        _cleanup_user(email)

    def test_subscription_has_token_quota(self, client):
        email = _unique_email()
        r = client.post("/v1/users", json={
            "email": email,
            "password": "securepass123",
            "plan_code": "tier_2",
        }, headers=_headers())
        assert r.status_code == 201
        sub = r.json()["subscription"]
        assert sub["token_quota"] > 0
        assert sub["tokens_remaining"] == sub["token_quota"]
        _cleanup_user(email)


# ---------------------------------------------------------------------------
# GET /v1/users/{user_id}
# ---------------------------------------------------------------------------

class TestGetUser:
    def test_get_user_without_subscription(self, client):
        email = _unique_email()
        created = client.post("/v1/users", json={"email": email, "password": "pass12345678"}, headers=_headers()).json()
        r = client.get(f"/v1/users/{created['id']}", headers=_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == created["id"]
        assert data["subscription"] is None
        _cleanup_user(email)

    def test_get_user_with_subscription(self, client):
        email = _unique_email()
        created = client.post("/v1/users", json={
            "email": email, "password": "pass12345678", "plan_code": "trial"
        }, headers=_headers()).json()
        r = client.get(f"/v1/users/{created['id']}", headers=_headers())
        assert r.status_code == 200
        assert r.json()["subscription"]["plan_code"] == "trial"
        _cleanup_user(email)

    def test_get_nonexistent_user_returns_404(self, client):
        r = client.get(f"/v1/users/{uuid.uuid4()}", headers=_headers())
        assert r.status_code == 404

    def test_requires_admin_key(self, client):
        r = client.get(f"/v1/users/{uuid.uuid4()}", headers={"X-API-Key": "wrong"})
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PATCH /v1/users/{user_id}
# ---------------------------------------------------------------------------

class TestUpdateUser:
    def test_update_full_name(self, client):
        email = _unique_email()
        created = client.post("/v1/users", json={"email": email, "password": "pass12345678"}, headers=_headers()).json()
        r = client.patch(f"/v1/users/{created['id']}", json={"full_name": "Siti Rahayu"}, headers=_headers())
        assert r.status_code == 200
        assert r.json()["full_name"] == "Siti Rahayu"
        _cleanup_user(email)

    def test_verify_email(self, client):
        email = _unique_email()
        created = client.post("/v1/users", json={"email": email, "password": "pass12345678"}, headers=_headers()).json()
        assert created["email_verified"] is False
        r = client.patch(f"/v1/users/{created['id']}", json={"email_verified": True}, headers=_headers())
        assert r.status_code == 200
        assert r.json()["email_verified"] is True
        _cleanup_user(email)

    def test_update_nonexistent_user_returns_404(self, client):
        r = client.patch(f"/v1/users/{uuid.uuid4()}", json={"full_name": "ghost"}, headers=_headers())
        assert r.status_code == 404

    def test_requires_admin_key(self, client):
        r = client.patch(f"/v1/users/{uuid.uuid4()}", json={"full_name": "x"}, headers={"X-API-Key": "wrong"})
        assert r.status_code in (401, 403)

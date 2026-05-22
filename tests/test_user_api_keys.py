"""TDD tests for User API Key endpoints.

Uses FastAPI TestClient (sync) with async SQLite via aiosqlite.
asyncio.run() is used directly — no pytest-asyncio dependency needed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as _sync_create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker as _sync_sessionmaker

from app.database import get_db
from app.main import app
from app.models.user_api_key import UserApiKey

# ---------------------------------------------------------------------------
# Two engines sharing the same in-memory SQLite file:
#   - async engine  → used by FastAPI routes (via dependency override)
#   - sync engine   → used by test helpers (_insert / _clear_rows)
# ---------------------------------------------------------------------------
_DB_FILE = "/tmp/test_uak.db"
ASYNC_URL = f"sqlite+aiosqlite:///{_DB_FILE}"
SYNC_URL = f"sqlite:///{_DB_FILE}"

async_engine = create_async_engine(ASYNC_URL)
AsyncTestSession = async_sessionmaker(async_engine, expire_on_commit=False)

sync_engine = _sync_create_engine(SYNC_URL, connect_args={"check_same_thread": False})
SyncTestSession = _sync_sessionmaker(bind=sync_engine)

# Create table synchronously at module load
UserApiKey.__table__.create(bind=sync_engine, checkfirst=True)


async def _override_get_db():
    async with AsyncTestSession() as db:
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def patch_admin_key(monkeypatch):
    from app import deps, config
    s = config.get_settings()
    monkeypatch.setattr(s, "api_key", ADMIN_KEY)
    monkeypatch.setattr(deps, "settings", s)
    # Set and restore DB override scoped to this test only
    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def clean_rows():
    yield
    with SyncTestSession() as db:
        db.execute(UserApiKey.__table__.delete())
        db.commit()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _admin():
    return {"X-API-Key": ADMIN_KEY}


# ---------------------------------------------------------------------------
# Helper: insert key directly via sync engine
# ---------------------------------------------------------------------------

def _insert(**kwargs):
    from types import SimpleNamespace
    from app.models.user_api_key import generate_user_key, hash_user_key
    raw_key = generate_user_key()
    with SyncTestSession() as db:
        k = UserApiKey(key_hash=hash_user_key(raw_key), **kwargs)
        db.add(k)
        db.commit()
        db.refresh(k)
        return SimpleNamespace(
            key=raw_key, id=k.id, label=k.label,
            expires_at=k.expires_at, revoked=k.revoked, created_at=k.created_at,
        )


# ---------------------------------------------------------------------------
# POST /v1/auth/keys
# ---------------------------------------------------------------------------

class TestGenerateKey:
    def test_creates_key_with_label(self, client):
        r = client.post("/v1/auth/keys", json={"label": "my-app"}, headers=_admin())
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["key"].startswith("uak_")
        assert data["label"] == "my-app"
        assert data["revoked"] is False

    def test_creates_key_without_label(self, client):
        r = client.post("/v1/auth/keys", json={}, headers=_admin())
        assert r.status_code == 201
        assert r.json()["label"] is None

    def test_key_expires_in_30_days(self, client):
        r = client.post("/v1/auth/keys", json={}, headers=_admin())
        raw = r.json()["expires_at"]
        expires_at = datetime.fromisoformat(raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        delta = expires_at - datetime.now(timezone.utc)
        assert 29 <= delta.days <= 30

    def test_requires_admin_key(self, client):
        r = client.post("/v1/auth/keys", json={}, headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_missing_admin_key_returns_422(self, client):
        r = client.post("/v1/auth/keys", json={})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/auth/keys/me
# ---------------------------------------------------------------------------

class TestKeyStatus:
    def test_active_key_returns_200(self, client):
        k = _insert(label="test")
        r = client.get("/v1/auth/keys/me", headers={"X-User-Key": k.key})
        assert r.status_code == 200
        assert r.json()["is_active"] is True

    def test_invalid_key_returns_401(self, client):
        r = client.get("/v1/auth/keys/me", headers={"X-User-Key": "uak_bad"})
        assert r.status_code == 401

    def test_expired_key_returns_403(self, client):
        k = _insert(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        r = client.get("/v1/auth/keys/me", headers={"X-User-Key": k.key})
        assert r.status_code == 403

    def test_revoked_key_returns_403(self, client):
        k = _insert(revoked=True)
        r = client.get("/v1/auth/keys/me", headers={"X-User-Key": k.key})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /v1/auth/keys/renew
# ---------------------------------------------------------------------------

class TestRenewKey:
    def test_renew_extends_expiry_by_30_days(self, client):
        k = _insert(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        r = client.post("/v1/auth/keys/renew", headers={"X-User-Key": k.key})
        assert r.status_code == 200
        raw = r.json()["expires_at"]
        expires_at = datetime.fromisoformat(raw)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        delta = expires_at - datetime.now(timezone.utc)
        assert delta.days >= 29

    def test_renew_response_has_message(self, client):
        k = _insert(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        r = client.post("/v1/auth/keys/renew", headers={"X-User-Key": k.key})
        assert "renewed" in r.json()["message"].lower()

    def test_expired_key_cannot_renew(self, client):
        k = _insert(expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        r = client.post("/v1/auth/keys/renew", headers={"X-User-Key": k.key})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /v1/auth/keys/{id}/revoke
# ---------------------------------------------------------------------------

class TestRevokeKey:
    def test_revoke_returns_204(self, client):
        k = _insert()
        r = client.post(f"/v1/auth/keys/{k.id}/revoke", headers=_admin())
        assert r.status_code == 204

    def test_revoked_key_rejected_on_me(self, client):
        k = _insert()
        client.post(f"/v1/auth/keys/{k.id}/revoke", headers=_admin())
        r = client.get("/v1/auth/keys/me", headers={"X-User-Key": k.key})
        assert r.status_code == 403

    def test_revoke_nonexistent_returns_404(self, client):
        r = client.post(f"/v1/auth/keys/{uuid.uuid4()}/revoke", headers=_admin())
        assert r.status_code == 404

    def test_revoke_requires_admin(self, client):
        k = _insert()
        r = client.post(f"/v1/auth/keys/{k.id}/revoke", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

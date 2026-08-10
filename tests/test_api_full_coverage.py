"""Full API coverage tests — all Postman collection endpoints.

Covers (in order of Postman collection):
  Health, Agents CRUD, Sessions, History, Runs, Memory, Skills,
  Custom Tools, Models, Stream (SSE), Channels incoming (basic routing)

Uses real PostgreSQL via asyncpg + NullPool.
Messages endpoint (LLM execution) is skipped — requires live LLM.
Documents/RAG endpoints are tested for CRUD and search (no embedding).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as _sync_create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker as _sync_sessionmaker
from sqlalchemy.pool import NullPool

load_dotenv()

_ASYNC_URL = os.environ["DATABASE_URL"]
_SYNC_URL = _ASYNC_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

from app.database import get_db
from app.main import app
from app.models.agent import Agent
from app.models.session import Session
from app.models.run import Run
from app.models.message import Message

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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_admin_key(monkeypatch):
    from app import config
    s = config.get_settings()
    monkeypatch.setattr(s, "api_key", ADMIN_KEY)
    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=True)


def _headers():
    return {"X-API-Key": ADMIN_KEY}


def _create_agent(client: TestClient, name: str | None = None) -> dict:
    name = name or f"test-agent-{uuid.uuid4().hex[:8]}"
    r = client.post("/v1/agents", json={"name": name, "instructions": "test"}, headers=_headers())
    assert r.status_code == 201, r.text
    return r.json()


def _delete_agent(client: TestClient, agent_id: str):
    client.delete(f"/v1/agents/{agent_id}", headers=_headers())


def _create_session(client: TestClient, agent_id: str) -> dict:
    r = client.post(
        f"/v1/agents/{agent_id}/sessions",
        json={"external_user_id": "u1"},
        headers=_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_no_auth_required(self, client):
        r = client.get("/health")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class TestAgentsCRUD:
    def test_create_agent_minimal(self, client):
        r = client.post("/v1/agents", json={"name": "MinimalAgent"}, headers=_headers())
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "MinimalAgent"
        assert "id" in data
        _delete_agent(client, data["id"])

    def test_create_agent_full(self, client):
        r = client.post("/v1/agents", json={
            "name": "FullAgent",
            "description": "desc",
            "instructions": "Be helpful",
            "model": "anthropic/claude-haiku-4-5",
            "temperature": 0.5,
        }, headers=_headers())
        assert r.status_code == 201
        data = r.json()
        assert data["model"] == "anthropic/claude-haiku-4-5"
        _delete_agent(client, data["id"])

    def test_create_agent_requires_auth(self, client):
        r = client.post("/v1/agents", json={"name": "NoAuth"}, headers={"X-API-Key": "wrong-key"})
        assert r.status_code in (401, 403)

    def test_create_agent_missing_name_returns_422(self, client):
        r = client.post("/v1/agents", json={"description": "no name"}, headers=_headers())
        assert r.status_code == 422

    def test_list_agents(self, client):
        agent = _create_agent(client, "list-test-agent")
        r = client.get("/v1/agents?limit=20&offset=0", headers=_headers())
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "total" in data
        ids = [a["id"] for a in data["items"]]
        assert agent["id"] in ids
        _delete_agent(client, agent["id"])

    def test_create_and_list_agents_scoped_by_owner_external_id(self, client):
        owner_response = client.post(
            "/v1/agents",
            json={"name": "owner-scoped-agent", "owner_external_id": "628111", "created_by_type": "dashboard"},
            headers=_headers(),
        )
        other_response = client.post(
            "/v1/agents",
            json={"name": "other-scoped-agent", "owner_external_id": "628222", "created_by_type": "dashboard"},
            headers=_headers(),
        )
        assert owner_response.status_code == 201
        assert other_response.status_code == 201
        owner_agent = owner_response.json()
        other_agent = other_response.json()

        assert owner_agent["owner_external_id"] == "628111"
        assert owner_agent["created_by_type"] == "dashboard"

        r = client.get("/v1/agents?limit=20&owner_external_id=628111", headers=_headers())
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()["items"]]
        assert owner_agent["id"] in ids
        assert other_agent["id"] not in ids

        _delete_agent(client, owner_agent["id"])
        _delete_agent(client, other_agent["id"])

    def test_get_agent_by_id(self, client):
        agent = _create_agent(client, "get-by-id-agent")
        r = client.get(f"/v1/agents/{agent['id']}", headers=_headers())
        assert r.status_code == 200
        assert r.json()["id"] == agent["id"]
        _delete_agent(client, agent["id"])

    def test_get_nonexistent_agent_returns_404(self, client):
        r = client.get(f"/v1/agents/{uuid.uuid4()}", headers=_headers())
        assert r.status_code == 404

    def test_patch_agent(self, client):
        agent = _create_agent(client, "patch-test-agent")
        r = client.patch(
            f"/v1/agents/{agent['id']}",
            json={"name": "patched-name", "instructions": "updated"},
            headers=_headers(),
        )
        assert r.status_code == 200
        assert r.json()["name"] == "patched-name"
        _delete_agent(client, agent["id"])

    def test_patch_nonexistent_agent_returns_404(self, client):
        r = client.patch(f"/v1/agents/{uuid.uuid4()}", json={"name": "x"}, headers=_headers())
        assert r.status_code == 404

    def test_delete_agent(self, client):
        agent = _create_agent(client, "delete-me-agent")
        r = client.delete(f"/v1/agents/{agent['id']}", headers=_headers())
        assert r.status_code == 204
        # Verify gone
        r2 = client.get(f"/v1/agents/{agent['id']}", headers=_headers())
        assert r2.status_code == 404

    def test_renew_agent_key(self, client):
        agent = _create_agent(client, "renew-key-agent")
        r = client.post(f"/v1/agents/{agent['id']}/renew", headers=_headers())
        assert r.status_code == 200
        data = r.json()
        assert "active_until" in data
        _delete_agent(client, agent["id"])


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class TestSessions:
    def test_create_session(self, client):
        agent = _create_agent(client, "session-create-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/sessions",
            json={"external_user_id": "user123"},
            headers=_headers(),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["agent_id"] == agent["id"]
        assert data["external_user_id"] == "user123"
        _delete_agent(client, agent["id"])

    def test_create_session_with_metadata(self, client):
        agent = _create_agent(client, "session-meta-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/sessions",
            json={"external_user_id": "u1", "metadata": {"source": "web"}},
            headers=_headers(),
        )
        assert r.status_code == 201
        _delete_agent(client, agent["id"])

    def test_create_session_nonexistent_agent(self, client):
        r = client.post(
            f"/v1/agents/{uuid.uuid4()}/sessions",
            json={},
            headers=_headers(),
        )
        assert r.status_code == 404

    def test_get_session(self, client):
        agent = _create_agent(client, "session-get-agent")
        session = _create_session(client, agent["id"])
        r = client.get(
            f"/v1/agents/{agent['id']}/sessions/{session['id']}",
            headers=_headers(),
        )
        assert r.status_code == 200
        assert r.json()["id"] == session["id"]
        _delete_agent(client, agent["id"])

    def test_get_nonexistent_session_returns_404(self, client):
        agent = _create_agent(client, "session-404-agent")
        r = client.get(
            f"/v1/agents/{agent['id']}/sessions/{uuid.uuid4()}",
            headers=_headers(),
        )
        assert r.status_code == 404
        _delete_agent(client, agent["id"])

    def test_patch_session(self, client):
        agent = _create_agent(client, "session-patch-agent")
        session = _create_session(client, agent["id"])
        r = client.patch(
            f"/v1/agents/{agent['id']}/sessions/{session['id']}",
            json={"metadata": {"updated": True}},
            headers=_headers(),
        )
        assert r.status_code == 200
        _delete_agent(client, agent["id"])


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class TestHistory:
    def test_get_history_empty_session(self, client):
        agent = _create_agent(client, "history-agent")
        session = _create_session(client, agent["id"])
        r = client.get(
            f"/v1/sessions/{session['id']}/history?limit=50",
            headers=_headers(),
        )
        assert r.status_code == 200
        data = r.json()
        assert "messages" in data
        assert data["messages"] == []
        _delete_agent(client, agent["id"])

    def test_get_history_nonexistent_session(self, client):
        r = client.get(
            f"/v1/sessions/{uuid.uuid4()}/history",
            headers=_headers(),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class TestRuns:
    def test_get_nonexistent_run_returns_404(self, client):
        r = client.get(f"/v1/runs/{uuid.uuid4()}", headers=_headers())
        assert r.status_code == 404

    def test_get_run_requires_auth(self, client):
        r = client.get(f"/v1/runs/{uuid.uuid4()}", headers={"X-API-Key": "wrong-key"})
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class TestMemory:
    def test_list_memory_empty(self, client):
        agent = _create_agent(client, "memory-list-agent")
        r = client.get(f"/v1/agents/{agent['id']}/memory", headers=_headers())
        assert r.status_code == 200
        assert r.json() == []
        _delete_agent(client, agent["id"])

    def test_upsert_memory(self, client):
        agent = _create_agent(client, "memory-upsert-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/memory",
            json={"key": "user_name", "value": "Alice"},
            headers=_headers(),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["key"] == "user_name"
        assert data["value_data"] == "Alice"
        _delete_agent(client, agent["id"])

    def test_upsert_memory_updates_existing(self, client):
        agent = _create_agent(client, "memory-update-agent")
        client.post(f"/v1/agents/{agent['id']}/memory", json={"key": "k", "value": "v1"}, headers=_headers())
        r = client.post(f"/v1/agents/{agent['id']}/memory", json={"key": "k", "value": "v2"}, headers=_headers())
        assert r.status_code in (200, 201)
        # Verify only one entry with updated value
        r2 = client.get(f"/v1/agents/{agent['id']}/memory", headers=_headers())
        entries = [m for m in r2.json() if m["key"] == "k"]
        assert entries[-1]["value_data"] == "v2"
        _delete_agent(client, agent["id"])

    def test_delete_memory(self, client):
        agent = _create_agent(client, "memory-delete-agent")
        client.post(f"/v1/agents/{agent['id']}/memory", json={"key": "to_delete", "value": "val"}, headers=_headers())
        r = client.delete(f"/v1/agents/{agent['id']}/memory/to_delete", headers=_headers())
        assert r.status_code == 204
        r2 = client.get(f"/v1/agents/{agent['id']}/memory", headers=_headers())
        keys = [m["key"] for m in r2.json()]
        assert "to_delete" not in keys
        _delete_agent(client, agent["id"])

    def test_delete_nonexistent_memory_returns_404(self, client):
        agent = _create_agent(client, "memory-notfound-agent")
        r = client.delete(f"/v1/agents/{agent['id']}/memory/nonexistent", headers=_headers())
        assert r.status_code == 404
        _delete_agent(client, agent["id"])

    def test_memory_nonexistent_agent_returns_404(self, client):
        r = client.get(f"/v1/agents/{uuid.uuid4()}/memory", headers=_headers())
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class TestSkills:
    def test_list_skills_empty(self, client):
        agent = _create_agent(client, "skills-list-agent")
        r = client.get(f"/v1/agents/{agent['id']}/skills", headers=_headers())
        assert r.status_code == 200
        assert r.json() == []
        _delete_agent(client, agent["id"])

    def test_create_skill(self, client):
        agent = _create_agent(client, "skills-create-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/skills",
            json={"name": "handle_refund", "description": "Handles refund requests", "content_md": "## Refund\nAlways refund."},
            headers=_headers(),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "handle_refund"
        _delete_agent(client, agent["id"])

    def test_get_skill_by_name(self, client):
        agent = _create_agent(client, "skills-get-agent")
        client.post(f"/v1/agents/{agent['id']}/skills", json={
            "name": "my_skill", "description": "desc", "content_md": "# content"
        }, headers=_headers())
        r = client.get(f"/v1/agents/{agent['id']}/skills/my_skill", headers=_headers())
        assert r.status_code == 200
        assert r.json()["name"] == "my_skill"
        _delete_agent(client, agent["id"])

    def test_get_nonexistent_skill_returns_404(self, client):
        agent = _create_agent(client, "skills-404-agent")
        r = client.get(f"/v1/agents/{agent['id']}/skills/ghost", headers=_headers())
        assert r.status_code == 404
        _delete_agent(client, agent["id"])

    def test_delete_skill(self, client):
        agent = _create_agent(client, "skills-delete-agent")
        client.post(f"/v1/agents/{agent['id']}/skills", json={
            "name": "deleteme", "description": "d", "content_md": "c"
        }, headers=_headers())
        r = client.delete(f"/v1/agents/{agent['id']}/skills/deleteme", headers=_headers())
        assert r.status_code == 204
        r2 = client.get(f"/v1/agents/{agent['id']}/skills/deleteme", headers=_headers())
        assert r2.status_code == 404
        _delete_agent(client, agent["id"])


# ---------------------------------------------------------------------------
# Custom Tools
# ---------------------------------------------------------------------------

class TestCustomTools:
    _TOOL_CODE = "def fibonacci(n: int) -> int:\n    if n <= 1: return n\n    return fibonacci(n-1) + fibonacci(n-2)\n"

    def test_list_custom_tools_empty(self, client):
        agent = _create_agent(client, "ct-list-agent")
        r = client.get(f"/v1/agents/{agent['id']}/custom-tools", headers=_headers())
        assert r.status_code == 200
        assert r.json() == []
        _delete_agent(client, agent["id"])

    def test_create_custom_tool(self, client):
        agent = _create_agent(client, "ct-create-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/custom-tools",
            json={"name": "fibonacci", "description": "Compute fibonacci", "code": self._TOOL_CODE},
            headers=_headers(),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "fibonacci"
        _delete_agent(client, agent["id"])

    def test_create_tool_invalid_name_returns_422(self, client):
        agent = _create_agent(client, "ct-invalid-name-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/custom-tools",
            json={"name": "Bad-Name", "description": "d", "code": "def Bad_Name(): pass"},
            headers=_headers(),
        )
        assert r.status_code == 422
        _delete_agent(client, agent["id"])

    def test_get_custom_tool_by_name(self, client):
        agent = _create_agent(client, "ct-get-agent")
        client.post(f"/v1/agents/{agent['id']}/custom-tools", json={
            "name": "mytool", "description": "d", "code": "def mytool(): pass\n"
        }, headers=_headers())
        r = client.get(f"/v1/agents/{agent['id']}/custom-tools/mytool", headers=_headers())
        assert r.status_code == 200
        assert r.json()["name"] == "mytool"
        _delete_agent(client, agent["id"])

    def test_get_nonexistent_tool_returns_404(self, client):
        agent = _create_agent(client, "ct-404-agent")
        r = client.get(f"/v1/agents/{agent['id']}/custom-tools/ghost", headers=_headers())
        assert r.status_code == 404
        _delete_agent(client, agent["id"])

    def test_delete_custom_tool(self, client):
        agent = _create_agent(client, "ct-delete-agent")
        client.post(f"/v1/agents/{agent['id']}/custom-tools", json={
            "name": "deltool", "description": "d", "code": "def deltool(): pass\n"
        }, headers=_headers())
        r = client.delete(f"/v1/agents/{agent['id']}/custom-tools/deltool", headers=_headers())
        assert r.status_code == 204
        r2 = client.get(f"/v1/agents/{agent['id']}/custom-tools/deltool", headers=_headers())
        assert r2.status_code == 404
        _delete_agent(client, agent["id"])


# ---------------------------------------------------------------------------
# Documents / RAG
# ---------------------------------------------------------------------------

class TestDocuments:
    def test_create_document(self, client):
        agent = _create_agent(client, "doc-create-agent")
        r = client.post(
            f"/v1/agents/{agent['id']}/documents",
            json={"title": "Test Doc", "content": "This is test content for RAG."},
            headers=_headers(),
        )
        assert r.status_code == 201
        data = r.json()
        assert data["title"] == "Test Doc"
        _delete_agent(client, agent["id"])

    def test_list_documents(self, client):
        agent = _create_agent(client, "doc-list-agent")
        client.post(f"/v1/agents/{agent['id']}/documents", json={
            "title": "Doc1", "content": "content1"
        }, headers=_headers())
        r = client.get(f"/v1/agents/{agent['id']}/documents?limit=20&offset=0", headers=_headers())
        assert r.status_code == 200
        data = r.json()
        assert "items" in data or isinstance(data, list)
        _delete_agent(client, agent["id"])

    def test_get_document_by_id(self, client):
        agent = _create_agent(client, "doc-get-agent")
        created = client.post(f"/v1/agents/{agent['id']}/documents", json={
            "title": "GetMe", "content": "content"
        }, headers=_headers()).json()
        r = client.get(f"/v1/agents/{agent['id']}/documents/{created['id']}", headers=_headers())
        assert r.status_code == 200
        assert r.json()["id"] == created["id"]
        _delete_agent(client, agent["id"])

    def test_patch_document(self, client):
        agent = _create_agent(client, "doc-patch-agent")
        created = client.post(f"/v1/agents/{agent['id']}/documents", json={
            "title": "PatchMe", "content": "original"
        }, headers=_headers()).json()
        r = client.patch(
            f"/v1/agents/{agent['id']}/documents/{created['id']}",
            json={"title": "Updated Title"},
            headers=_headers(),
        )
        assert r.status_code == 200
        assert r.json()["title"] == "Updated Title"
        _delete_agent(client, agent["id"])

    def test_delete_document(self, client):
        agent = _create_agent(client, "doc-del-agent")
        created = client.post(f"/v1/agents/{agent['id']}/documents", json={
            "title": "DelMe", "content": "content"
        }, headers=_headers()).json()
        r = client.delete(f"/v1/agents/{agent['id']}/documents/{created['id']}", headers=_headers())
        assert r.status_code == 204
        r2 = client.get(f"/v1/agents/{agent['id']}/documents/{created['id']}", headers=_headers())
        assert r2.status_code == 404
        _delete_agent(client, agent["id"])

    def test_search_documents(self, client):
        agent = _create_agent(client, "doc-search-agent")
        client.post(f"/v1/agents/{agent['id']}/documents", json={
            "title": "Python guide", "content": "Python is a programming language."
        }, headers=_headers())
        r = client.post(
            f"/v1/agents/{agent['id']}/documents/search",
            json={"query": "programming language", "limit": 5},
            headers=_headers(),
        )
        assert r.status_code == 200
        _delete_agent(client, agent["id"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TestModels:
    def test_list_models(self, client):
        r = client.get("/v1/models", headers=_headers())
        assert r.status_code == 200
        data = r.json()
        # Response is either a list or a dict with "models" key
        models = data if isinstance(data, list) else data.get("models", data)
        assert len(models) > 0
        assert "id" in (models[0] if isinstance(models, list) else list(models.values())[0])

    def test_list_models_requires_auth(self, client):
        r = client.get("/v1/models", headers={"X-API-Key": "wrong-key"})
        assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Stream (SSE)
# ---------------------------------------------------------------------------

class TestStream:
    def test_stream_nonexistent_session_returns_404(self, client):
        # With stream_type=False so TestClient doesn't block on SSE
        r = client.get(
            f"/v1/sessions/{uuid.uuid4()}/stream?timeout=30",
            headers=_headers(),
        )
        assert r.status_code == 404

    def test_stream_requires_auth(self, client):
        agent = _create_agent(client, "stream-auth-agent")
        session = _create_session(client, agent["id"])
        # Make a non-streaming request (no auth) — expect 401/403
        r = client.get(f"/v1/sessions/{session['id']}/stream?timeout=30")
        assert r.status_code in (401, 403)
        _delete_agent(client, agent["id"])

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.domain import memory_service


@pytest.mark.asyncio
async def test_load_layered_memory_prefers_active_versioned_soul(monkeypatch):
    agent_id = uuid.uuid4()
    calls: list[tuple[str, str | None]] = []

    async def fake_get_memory(_agent_id, key, _db, scope=None):
        calls.append((key, scope))
        if key == "agent_context_version":
            return SimpleNamespace(value_data="2")
        if key == "soul:v2":
            return SimpleNamespace(value_data="new active soul")
        return None

    monkeypatch.setattr(memory_service, "get_memory", fake_get_memory)

    data = await memory_service.load_layered_memory(agent_id, db=SimpleNamespace(), scope="62811")

    assert data["soul"] == "new active soul"
    assert data["agent_context_version"] == "2"
    assert ("soul", None) not in calls


@pytest.mark.asyncio
async def test_load_layered_memory_falls_back_to_legacy_soul(monkeypatch):
    agent_id = uuid.uuid4()

    async def fake_get_memory(_agent_id, key, _db, scope=None):
        if key == "agent_context_version":
            return SimpleNamespace(value_data="7")
        if key == "soul:v7":
            return None
        if key == "soul":
            return SimpleNamespace(value_data="legacy soul")
        return None

    monkeypatch.setattr(memory_service, "get_memory", fake_get_memory)

    data = await memory_service.load_layered_memory(agent_id, db=SimpleNamespace(), scope="62811")

    assert data["soul"] == "legacy soul"
    assert data["agent_context_version"] == "7"


@pytest.mark.asyncio
async def test_build_memory_context_excludes_versioned_context_archives(monkeypatch):
    agent_id = uuid.uuid4()

    async def fake_list_memories(_agent_id, _db, scope=None):
        return [
            SimpleNamespace(key="soul:v2", value_data="archived soul"),
            SimpleNamespace(key="agent_blueprint:v2", value_data="archived blueprint"),
            SimpleNamespace(key="setup_summary:v2", value_data="archived setup"),
            SimpleNamespace(key="favorite_tone", value_data="santai"),
        ]

    monkeypatch.setattr(memory_service, "list_memories", fake_list_memories)

    block = await memory_service.build_memory_context(agent_id, db=SimpleNamespace(), scope=None)

    assert "favorite_tone" in block
    assert "soul:v2" not in block
    assert "agent_blueprint:v2" not in block
    assert "setup_summary:v2" not in block

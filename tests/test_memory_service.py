import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.domain import memory_service
from app.core.engine.prompt_builder import build_system_prompt


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


@pytest.mark.asyncio
async def test_load_layered_memory_loads_longterm_and_runtime_layers(monkeypatch):
    agent_id = uuid.uuid4()
    scoped_values = {
        ("longterm", "62811"): "old durable context",
        ("active_context", "62811"): "latest runtime context",
        ("last_turn", "62811"): "latest user and agent turn",
        ("last_attachment", "62811"): "dummy test.pdf",
        ("last_generated_artifact", "62811"): "/workspace/shared/embarkation_analysis.png",
    }

    async def fake_get_memory(_agent_id, key, _db, scope=None):
        value = scoped_values.get((key, scope))
        return SimpleNamespace(value_data=value) if value else None

    monkeypatch.setattr(memory_service, "get_memory", fake_get_memory)

    data = await memory_service.load_layered_memory(agent_id, db=SimpleNamespace(), scope="62811")

    assert data["longterm"] == "old durable context"
    assert data["active_context"] == "latest runtime context"
    assert data["last_turn"] == "latest user and agent turn"
    assert data["last_attachment"] == "dummy test.pdf"
    assert data["last_generated_artifact"] == "/workspace/shared/embarkation_analysis.png"


@pytest.mark.asyncio
async def test_record_runtime_memory_writes_scoped_daily_longterm_and_active_context(monkeypatch):
    agent_id = uuid.uuid4()
    today_key = f"daily:{memory_service.memory_today()}"
    existing = {
        today_key: "- older daily line",
        "longterm": "- older longterm line",
    }
    writes: list[tuple[str, str, str | None]] = []

    async def fake_get_memory(_agent_id, key, _db, scope=None):
        value = existing.get(key)
        return SimpleNamespace(value_data=value) if value else None

    async def fake_upsert_memory(_agent_id, key, value, _db, scope=None):
        writes.append((key, value, scope))
        return SimpleNamespace(key=key, value_data=value, scope=scope)

    monkeypatch.setattr(memory_service, "get_memory", fake_get_memory)
    monkeypatch.setattr(memory_service, "upsert_memory", fake_upsert_memory)

    await memory_service.record_runtime_memory(
        agent_id=agent_id,
        db=SimpleNamespace(),
        scope="62811",
        user_message=(
            "Tolong buat visualisasi dari dummy test.pdf\n"
            "Isi dokumen:\n```"
            + ("titanic stale body " * 100)
            + "```"
        ),
        final_reply="Berikut file embarkation_analysis.png.",
        current_attachment_name="dummy test.pdf",
        generated_artifact_path="/workspace/shared/embarkation_analysis.png",
    )

    by_key = {key: value for key, value, _scope in writes}

    assert {scope for _key, _value, scope in writes} == {"62811"}
    assert today_key in by_key
    assert "active_context" in by_key
    assert "last_turn" in by_key
    assert by_key["last_attachment"] == "dummy test.pdf"
    assert by_key["last_generated_artifact"] == "/workspace/shared/embarkation_analysis.png"
    assert "longterm" in by_key
    assert "dummy test.pdf" in by_key["active_context"]
    assert "embarkation_analysis.png" in by_key["longterm"]
    assert "titanic stale body" not in by_key["active_context"]
    assert "[konten dokumen dipangkas" in by_key["active_context"]


def test_build_system_prompt_injects_runtime_memory_as_latest_context():
    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_config={},
        channel_type="whatsapp",
        external_user_id="62811",
        metadata_={},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Arva",
        model="openai/gpt-4.1-mini",
        instructions="Kamu adalah assistant personal.",
        tools_config={"memory": True},
        escalation_config={},
        safety_policy={},
        capabilities=[],
    )

    prompt = build_system_prompt(
        agent_model=agent,
        session=session,
        active_groups=["memory"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name="Bagas",
        context_summary="",
        memory_block="",
        layered_memory={
            "soul": "Kamu Arva.",
            "user_profile": "User: Bagas.",
            "active_context": "Lampiran terbaru: dummy test.pdf",
            "last_turn": "User minta visualisasi dummy test.pdf.",
            "last_attachment": "dummy test.pdf",
            "last_generated_artifact": "/workspace/shared/embarkation_analysis.png",
            "daily_today": "Turn terbaru memakai dummy test.pdf.",
            "longterm": "Konteks lama: titanic.txt pernah dianalisis.",
            "today_date": "2026-06-10",
            "yesterday_date": "2026-06-09",
        },
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="Tolong buat visualisasi dari file ini",
        current_time=datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc),
    )

    assert "## Konteks Aktif Runtime" in prompt
    assert "Jika bagian ini bertentangan" in prompt
    assert "dummy test.pdf" in prompt
    assert "## Long-Term Curated Context" in prompt
    assert "titanic.txt pernah dianalisis" in prompt
    assert "active_context" in prompt
    assert "longterm" in prompt
    assert "Di-load otomatis" in prompt

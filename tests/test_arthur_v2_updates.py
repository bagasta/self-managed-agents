from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from arthur_v2 import plugin


@pytest.mark.asyncio
async def test_update_saves_name_and_instructions_preserving_capabilities(monkeypatch):
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Before", description="Purpose", instructions="Old SOP",
        tools_config={"scheduler": True, "mcp": {"servers": {"google_workspace": {"allowed_services": ["sheets"]}}}},
        version=3, channel_type="whatsapp", model="deepseek/deepseek-v4-flash", is_deleted=False,
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: agent)
    no_job = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[result, no_job]), get=AsyncMock(return_value=agent),
        commit=AsyncMock(), refresh=AsyncMock(),
    )
    class Context:
        async def __aenter__(self):
            return db
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(plugin, "agent_belongs_to_owner", lambda *args: True)
    tool = next(t for t in plugin.build_arthur_v2_tools(db_factory=Context, owner_phone="6281234567890", self_agent_id=None) if t.name == "update_assistant")
    config = agent.tools_config.copy()
    reply = await tool.ainvoke({"agent_id": str(agent.id), "name": " After ", "instructions": "Revised SOP", "confirmed": True})
    assert reply["ok"] is True
    assert agent.name == "After"
    assert agent.description == "Purpose"
    assert agent.instructions.startswith("Revised SOP\n")
    assert "# GOOGLE WORKSPACE" in agent.instructions
    assert "# OTOMASI BACKGROUND" in agent.instructions
    assert agent.tools_config == config
    assert agent.version == 4
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_does_not_access_database_without_confirmation():
    tool = next(t for t in plugin.build_arthur_v2_tools(db_factory=None, owner_phone="6281234567890", self_agent_id=None) if t.name == "update_assistant")
    reply = await tool.ainvoke({"agent_id": str(uuid.uuid4()), "name": "After"})
    assert reply["needs_confirmation"] is True

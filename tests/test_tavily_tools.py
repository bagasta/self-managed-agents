from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def test_build_tavily_tools_exposes_search_and_extract(monkeypatch):
    from app.core.tools import tavily_tool

    monkeypatch.setattr(
        tavily_tool,
        "get_settings",
        lambda: SimpleNamespace(tavily_api_key="test-key"),
    )

    tools = tavily_tool.build_tavily_tools({"tavily": True})

    assert {tool.name for tool in tools} == {"tavily_search", "tavily_extract"}


@pytest.mark.asyncio
async def test_agent_tool_setup_loads_tavily_by_default(monkeypatch):
    from app.core.engine import agent_tool_setup
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    monkeypatch.setattr(
        agent_tool_setup,
        "get_settings",
        lambda: SimpleNamespace(tavily_api_key="test-key"),
    )
    monkeypatch.setattr(
        agent_tool_setup,
        "build_tavily_tools",
        lambda cfg: [SimpleNamespace(name="tavily_search")],
    )

    agent = SimpleNamespace(id=uuid4(), capabilities=[])
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        channel_type="api",
        channel_config={},
        external_user_id="628111",
    )

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={"memory": False, "skills": False, "escalation": False},
        raw_tools_config={},
        db=AsyncMock(),
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        escalation_user_jid=None,
        sender_name=None,
        user_message="cari info terbaru",
    )

    assert "tavily" in setup.active_groups
    assert {tool.name for tool in setup.tools} == {"tavily_search"}


@pytest.mark.asyncio
async def test_agent_tool_setup_can_disable_tavily(monkeypatch):
    from app.core.engine import agent_tool_setup
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    monkeypatch.setattr(
        agent_tool_setup,
        "get_settings",
        lambda: SimpleNamespace(tavily_api_key="test-key"),
    )
    monkeypatch.setattr(
        agent_tool_setup,
        "build_tavily_tools",
        lambda cfg: [SimpleNamespace(name="tavily_search")],
    )

    agent = SimpleNamespace(id=uuid4(), capabilities=[])
    session = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        channel_type="api",
        channel_config={},
        external_user_id="628111",
    )

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={
            "memory": False,
            "skills": False,
            "escalation": False,
            "tavily": False,
        },
        raw_tools_config={},
        db=AsyncMock(),
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        escalation_user_jid=None,
        sender_name=None,
        user_message="cari info terbaru",
    )

    assert "tavily" not in setup.active_groups
    assert setup.tools == []

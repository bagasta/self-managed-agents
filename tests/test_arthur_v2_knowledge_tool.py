from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.channels import _is_arthur_system_agent, _is_passive_deploy_acknowledgement
from arthur_v2.plugin import (
    CreateAssistantInput,
    _merge_runtime_config,
    build_arthur_v2_system_prompt,
    build_arthur_v2_tools,
)


def test_arthur_v2_exposes_owned_knowledge_tool() -> None:
    tools = build_arthur_v2_tools(
        db_factory=None,
        owner_phone="6281234567890",
        self_agent_id="00000000-0000-0000-0000-000000000000",
        session_id="11111111-1111-1111-1111-111111111111",
    )

    assert "add_assistant_knowledge" in {tool.name for tool in tools}
    assert "add_assistant_knowledge" in build_arthur_v2_system_prompt()


def test_arthur_v2_exposes_deploy_for_website_assistants() -> None:
    schema = CreateAssistantInput.model_json_schema()
    prompt = build_arthur_v2_system_prompt()
    tools = build_arthur_v2_tools(
        db_factory=None,
        owner_phone="6281234567890",
        self_agent_id="00000000-0000-0000-0000-000000000000",
    )
    runtime_tool = next(tool for tool in tools if tool.name == "configure_assistant_runtime")

    assert "enable_deploy" in schema["properties"]
    assert "enable_deploy" in runtime_tool.args_schema.model_json_schema()["properties"]
    assert "deploy_app" in prompt


def test_runtime_config_preserves_omitted_deploy_and_subagents() -> None:
    config, sandbox, deploy, subagent_ids = _merge_runtime_config(
        {
            "sandbox": True,
            "deploy": True,
            "subagents": {"enabled": True, "agent_ids": ["agent-1"]},
        },
        enable_sandbox=None,
        enable_deploy=None,
        subagent_ids=None,
    )

    assert sandbox is True
    assert deploy is True
    assert subagent_ids == ["agent-1"]
    assert config["subagents"] == {"enabled": True, "agent_ids": ["agent-1"]}


def test_runtime_config_explicit_empty_subagents_uses_system_subagents() -> None:
    config, sandbox, deploy, subagent_ids = _merge_runtime_config(
        {"sandbox": False, "deploy": False},
        enable_sandbox=None,
        enable_deploy=True,
        subagent_ids=[],
    )

    assert sandbox is True
    assert deploy is True
    assert subagent_ids == []
    assert config["subagents"] == {"enabled": True, "agent_ids": []}


def test_only_explicit_arthur_system_plugins_bypass_spam_guard() -> None:
    assert _is_arthur_system_agent(SimpleNamespace(tools_config={"system_plugin": "arthur_v2"}))
    assert not _is_arthur_system_agent(SimpleNamespace(tools_config={"builder": True}))


def test_deploy_acknowledgement_does_not_interrupt_active_website_work() -> None:
    deploy_agent = SimpleNamespace(tools_config={"deploy": True})

    assert _is_passive_deploy_acknowledgement(deploy_agent, "Ok")
    assert not _is_passive_deploy_acknowledgement(deploy_agent, "ganti desainnya jadi merah")


@pytest.mark.asyncio
async def test_arthur_v2_knowledge_tool_requires_confirmation() -> None:
    tools = build_arthur_v2_tools(
        db_factory=None,
        owner_phone="6281234567890",
        self_agent_id="00000000-0000-0000-0000-000000000000",
        session_id="11111111-1111-1111-1111-111111111111",
    )
    add_knowledge = next(tool for tool in tools if tool.name == "add_assistant_knowledge")

    result = await add_knowledge.ainvoke({"agent_id": "00000000-0000-0000-0000-000000000001"})

    assert result["ok"] is False
    assert result["needs_confirmation"] is True

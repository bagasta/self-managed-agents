from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.agent import AgentCreate, AgentUpdate

from app.core.engine import agent_llm


class _FakeChatOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def bind(self, **kwargs):
        return (self, kwargs)


def _settings():
    return SimpleNamespace(
        openrouter_api_key="test",
        mistral_api_key="test",
        llm_max_tokens=1024,
        google_agent_max_tokens=4096,
        coding_agent_max_tokens=8192,
        llm_request_timeout_seconds=120,
        llm_max_retries=1,
    )


def test_google_agent_gets_safe_minimum_output_budget(monkeypatch):
    monkeypatch.setattr(agent_llm, "ChatOpenAI", _FakeChatOpenAI)
    model = SimpleNamespace(
        model="anthropic/claude-haiku-5.5",
        max_tokens=800,
        tools_config={
            "mcp": {
                "enabled": True,
                "servers": {"google_workspace": {"allowed_services": ["docs"]}},
            }
        },
    )

    llm, _ = agent_llm.build_agent_llms(model, _settings(), 0.7)

    assert llm.kwargs["max_tokens"] == 4096


def test_non_google_agent_keeps_normal_output_budget(monkeypatch):
    monkeypatch.setattr(agent_llm, "ChatOpenAI", _FakeChatOpenAI)
    model = SimpleNamespace(
        model="deepseek/deepseek-v4-flash",
        max_tokens=None,
        tools_config={"tavily": True},
    )

    llm, _ = agent_llm.build_agent_llms(model, _settings(), 0.7)

    assert llm.kwargs["max_tokens"] == 1024


@pytest.mark.parametrize("configured,expected", [(4096, 16384), (24000, 24000)])
def test_deepseek_google_reasoning_leaves_room_for_writes(monkeypatch, configured, expected):
    monkeypatch.setattr(agent_llm, "ChatOpenAI", _FakeChatOpenAI)
    model = SimpleNamespace(model="deepseek/deepseek-v4-flash", max_tokens=configured,
                            tools_config={"mcp": {"servers": {"google_workspace": {"allowed_services": ["sheets"]}}}})
    llm, _ = agent_llm.build_agent_llms(model, _settings(), 0.2)
    assert llm.kwargs["max_tokens"] == expected


@pytest.mark.parametrize("model_name", [" deepseek/deepseek-v4-flash \n", " mistral/mistral-small-latest "])
def test_saved_model_whitespace_does_not_break_provider_routing(monkeypatch, model_name):
    monkeypatch.setattr(agent_llm, "ChatOpenAI", _FakeChatOpenAI)
    model = SimpleNamespace(model=model_name, max_tokens=None, tools_config={})
    llm, _ = agent_llm.build_agent_llms(model, _settings(), 0.7)
    assert llm.kwargs["model"] == model_name.strip().removeprefix("mistral/")
    assert ("mistral.ai" in llm.kwargs["base_url"]) == model_name.strip().startswith("mistral/")


@pytest.mark.parametrize("schema", [AgentCreate, AgentUpdate])
def test_agent_model_input_is_trimmed_and_cannot_be_blank(schema):
    assert schema(name="Test", model=" deepseek/deepseek-v4-flash ").model == "deepseek/deepseek-v4-flash"
    with pytest.raises(ValidationError):
        schema(name="Test", model=" \n ")


def test_arthur_has_room_to_serialize_complete_workflow(monkeypatch):
    monkeypatch.setattr(agent_llm, "ChatOpenAI", _FakeChatOpenAI)
    model = SimpleNamespace(model="anthropic/claude-haiku-5.5", max_tokens=4096, tools_config={"system_plugin": "arthur_v2"})
    llm, _ = agent_llm.build_agent_llms(model, _settings(), 0.7)
    assert llm.kwargs["max_tokens"] == 16384

from types import SimpleNamespace

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

from __future__ import annotations

from types import SimpleNamespace

from app.core.engine import agent_llm
from app.core.engine.tool_builder import build_sandbox_binary_tool


def test_sandbox_agents_get_a_larger_default_completion_budget(monkeypatch):
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind(self, **_kwargs):
            return self

    monkeypatch.setattr(agent_llm, "ChatOpenAI", FakeChatOpenAI)
    settings = SimpleNamespace(
        openrouter_api_key="test",
        mistral_api_key="test",
        coding_agent_max_tokens=8192,
        llm_max_tokens=1024,
        llm_request_timeout_seconds=120,
        llm_max_retries=1,
    )
    agent = SimpleNamespace(
        model="deepseek/deepseek-v4-flash",
        temperature=0.7,
        max_tokens=None,
        tools_config={"sandbox": True, "deploy": True},
    )

    agent_llm.build_agent_llms(agent, settings, temperature=0.7)

    assert captured["max_tokens"] == 8192


def test_sandbox_text_writer_is_available_for_large_source_files():
    written: dict[str, str] = {}

    class FakeSandbox:
        def write_file(self, path: str, content: str) -> str:
            written[path] = content
            return "ok"

        def write_binary_file(self, _path: str, _content: str) -> str:
            return "ok"

    tools = {tool.name: tool for tool in build_sandbox_binary_tool(FakeSandbox())}

    assert tools["sandbox_write_text_file"].invoke({"path": "index.html", "content": "<main>Dim sum</main>"}) == "ok"
    assert written == {"index.html": "<main>Dim sum</main>"}

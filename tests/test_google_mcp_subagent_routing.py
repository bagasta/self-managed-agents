from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import ToolMessage

from app.core.engine import agent_tool_setup
from app.core.engine.agent_runner import (
    BlockTaskToolMiddleware,
    _google_workspace_server_has_auth,
    _remove_google_workspace_mcp_server,
)
from app.core.engine.agent_tool_setup import build_agent_tool_setup
from app.core.engine.google_mcp_support import (
    build_google_mcp_usage_notice,
    _is_google_mcp_intent,
    is_google_workspace_mcp_configured,
)


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str, **kwargs) -> None:
        self.events.append((event, kwargs))


def _agent():
    return SimpleNamespace(id=uuid4(), capabilities=[])


def _session(agent_id):
    return SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        channel_type="api",
        channel_config={},
        external_user_id="user@example.com",
    )


def _tools_config() -> dict:
    return {
        "memory": False,
        "skills": False,
        "escalation": False,
        "subagents": {"enabled": True, "agent_ids": ["sys_coder"]},
        "mcp": {
            "enabled": True,
            "servers": {
                "google_workspace": {
                    "url": "http://localhost:8002/mcp",
                    "transport": "streamable_http",
                }
            },
        },
    }


def test_google_workspace_mcp_configured_detects_wrapped_config() -> None:
    assert is_google_workspace_mcp_configured(_tools_config())


def test_google_mcp_usage_notice_marks_workspace_parent_only() -> None:
    notice = build_google_mcp_usage_notice("buat google slide")

    assert "PARENT-ONLY EXECUTION" in notice
    assert "main agent WAJIB memanggil tool Google MCP langsung" in notice
    assert "JANGAN delegasikan aksi Google Workspace ke subagent/task()" in notice


def test_google_mcp_intent_detects_indonesian_calendar_terms() -> None:
    assert _is_google_mcp_intent("tolong buat jadwal meeting di kalender besok")
    assert _is_google_mcp_intent("buatkan dokumen google untuk proposal")


def test_parent_only_middleware_blocks_task_tool_only() -> None:
    middleware = BlockTaskToolMiddleware()
    task_request = SimpleNamespace(
        tool=SimpleNamespace(name="task"),
        tool_call={"id": "tc_task"},
    )
    blocked = middleware.wrap_tool_call(
        task_request,
        lambda request: ToolMessage(content="should not run", tool_call_id="tc_task"),
    )

    assert isinstance(blocked, ToolMessage)
    assert blocked.tool_call_id == "tc_task"
    assert blocked.status == "error"
    assert "disabled for this run" in blocked.content

    normal_request = SimpleNamespace(
        tool=SimpleNamespace(name="create_presentation"),
        tool_call={"id": "tc_mcp"},
    )
    allowed = middleware.wrap_tool_call(
        normal_request,
        lambda request: ToolMessage(content="ok", tool_call_id=request.tool_call["id"]),
    )

    assert isinstance(allowed, ToolMessage)
    assert allowed.content == "ok"
    assert allowed.tool_call_id == "tc_mcp"


def test_google_workspace_mcp_removed_until_per_user_bearer_exists() -> None:
    filtered = _remove_google_workspace_mcp_server(_tools_config())

    assert filtered["mcp"]["enabled"] is False
    assert filtered["mcp"]["servers"] == {}


def test_google_workspace_auth_header_detection_requires_runtime_bearer() -> None:
    assert _google_workspace_server_has_auth(SimpleNamespace(workspace_server={})) is False
    assert (
        _google_workspace_server_has_auth(
            SimpleNamespace(
                workspace_server={
                    "headers": {"Authorization": "Bearer per-user-token"}
                }
            )
        )
        is True
    )


@pytest.mark.asyncio
async def test_google_mcp_intent_skips_subagent_build_before_prompt(monkeypatch) -> None:
    called = False

    async def fake_build_subagents(*args, **kwargs):
        nonlocal called
        called = True
        return ([{"name": "sys_coder"}], [])

    monkeypatch.setattr(agent_tool_setup, "build_subagents", fake_build_subagents)
    log = _Log()
    agent = _agent()

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=_session(agent.id),
        tools_config=_tools_config(),
        raw_tools_config={},
        db=None,
        log=log,
        escalation_user_jid=None,
        sender_name="Bagas",
        user_message="buatkan presentasi google slide dengan mcp",
    )

    assert called is False
    assert setup.subagent_list == []
    assert not any(group.startswith("subagents(") for group in setup.active_groups)
    assert ("agent_run.google_mcp_subagents_skipped", {"reason": "google_workspace_mcp_parent_only"}) in log.events


@pytest.mark.asyncio
async def test_google_mcp_intent_skips_parent_sandbox_tools(monkeypatch) -> None:
    sandbox_called = False
    deploy_called = False

    def fake_sandbox(*args, **kwargs):
        nonlocal sandbox_called
        sandbox_called = True
        raise AssertionError("Google Workspace MCP run must not create DockerSandbox")

    def fake_deploy_tools(*args, **kwargs):
        nonlocal deploy_called
        deploy_called = True
        return []

    monkeypatch.setattr(agent_tool_setup, "DockerSandbox", fake_sandbox)
    monkeypatch.setattr(agent_tool_setup, "build_deployment_tools", fake_deploy_tools)
    monkeypatch.setattr(
        agent_tool_setup,
        "build_sandbox_binary_tool",
        lambda sandbox: [SimpleNamespace(name="sandbox_write_binary_file")],
    )
    log = _Log()
    agent = _agent()
    cfg = _tools_config()
    cfg["sandbox"] = True
    cfg["deploy"] = True

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=_session(agent.id),
        tools_config=cfg,
        raw_tools_config={},
        db=None,
        log=log,
        escalation_user_jid=None,
        sender_name="Bagas",
        user_message="buatkan presentasi google slide dengan mcp",
    )

    assert sandbox_called is False
    assert deploy_called is False
    assert setup.sandbox is None
    assert "sandbox" not in setup.active_groups
    assert "deploy" not in setup.active_groups
    assert "google_mcp_parent_only" in setup.active_groups
    assert all("sandbox" not in getattr(tool, "name", "") for tool in setup.tools)
    assert (
        "agent_run.google_mcp_parent_sandbox_skipped",
        {
            "reason": "google_workspace_mcp_must_not_fallback_to_sandbox",
            "deploy_enabled": True,
        },
    ) in log.events


@pytest.mark.asyncio
async def test_non_google_intent_keeps_subagent_build(monkeypatch) -> None:
    called = False

    async def fake_build_subagents(*args, **kwargs):
        nonlocal called
        called = True
        return ([{"name": "sys_coder"}], [])

    monkeypatch.setattr(agent_tool_setup, "build_subagents", fake_build_subagents)
    log = _Log()
    agent = _agent()

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=_session(agent.id),
        tools_config=_tools_config(),
        raw_tools_config={},
        db=None,
        log=log,
        escalation_user_jid=None,
        sender_name="Bagas",
        user_message="buatkan landing page portfolio",
    )

    assert called is True
    assert setup.subagent_list == [{"name": "sys_coder"}]
    assert "subagents(1)" in setup.active_groups

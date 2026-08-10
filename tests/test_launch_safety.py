import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_mock_sandbox(session_id: str) -> MagicMock:
    sandbox = MagicMock()
    sandbox.session_id = session_id
    workspace = Path(tempfile.mkdtemp()) / "workspace"
    workspace.mkdir()
    sandbox.workspace_dir = workspace
    return sandbox


def _session_and_agent():
    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_type="whatsapp",
        channel_config={"user_phone": "62811@s.whatsapp.net", "device_id": "dev-1"},
        external_user_id="62811",
    )
    agent = SimpleNamespace(id=agent_id, capabilities=[], tools_config={})
    return session, agent


_TOOLS_CONFIG = {
    "memory": False,
    "skills": False,
    "tavily": False,
    "escalation": True,
    "whatsapp_media": True,
    "sandbox": True,
    "deploy": True,
    "tool_creator": True,
    "subagents": {"enabled": True, "agent_ids": ["sys_coder"]},
}


@pytest.mark.asyncio
async def test_kill_switch_disables_sandbox_subagents_when_flag_off(monkeypatch):
    """Regression: when the kill switch is OFF, sandbox/deploy/subagents are stripped
    but escalation + WhatsApp media survive."""
    from app.core.engine import agent_tool_setup
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    monkeypatch.setattr(agent_tool_setup.get_settings(), "sandbox_subagents_enabled", False)

    session, agent = _session_and_agent()
    logs: list[tuple[str, dict]] = []
    log = SimpleNamespace(
        info=lambda event, **kwargs: logs.append((event, kwargs)),
        warning=lambda event, **kwargs: logs.append((event, kwargs)),
    )

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config=dict(_TOOLS_CONFIG),
        raw_tools_config={},
        db=SimpleNamespace(),
        log=log,
        escalation_user_jid=None,
        sender_name="Bagas",
        user_message="halo",
        operating_manual={"maturity": "usable", "owner_review_required": False},
    )

    tool_names = {getattr(tool, "name", "") for tool in setup.tools}
    assert setup.sandbox is None
    assert setup.subagent_list == []
    assert setup.sub_sandboxes == []
    assert "sandbox" not in setup.active_groups
    assert "deploy" not in setup.active_groups
    assert not any(group.startswith("subagents") for group in setup.active_groups)
    assert "escalation" in setup.active_groups
    assert "whatsapp_media" in setup.active_groups
    assert "escalate_to_human" in tool_names
    assert "send_whatsapp_document" in tool_names
    assert any(event == "agent_run.sandbox_subagents_disabled_for_launch" for event, _ in logs)


@pytest.mark.asyncio
async def test_sandbox_enabled_when_flag_on(monkeypatch):
    """When the kill switch is ON (default), sandbox + deploy are active."""
    from app.core.engine import agent_tool_setup
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    monkeypatch.setattr(agent_tool_setup.get_settings(), "sandbox_subagents_enabled", True)
    fake_sandbox = _make_mock_sandbox("parent-session")
    monkeypatch.setattr(agent_tool_setup, "DockerSandbox", lambda sid: fake_sandbox)
    monkeypatch.setattr(
        agent_tool_setup,
        "build_sandbox_binary_tool",
        lambda sandbox: [SimpleNamespace(name="sandbox_write_binary_file")],
    )
    monkeypatch.setattr(
        agent_tool_setup,
        "build_deployment_tools",
        lambda sandbox: [SimpleNamespace(name="deploy_app")],
    )

    async def _fake_build_subagents(*a, **k):
        return [], []

    monkeypatch.setattr(agent_tool_setup, "build_subagents", _fake_build_subagents)

    session, agent = _session_and_agent()
    log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={
            "memory": False,
            "skills": False,
            "tavily": False,
            "escalation": False,
            "sandbox": True,
            "deploy": True,
        },
        raw_tools_config={},
        db=MagicMock(),
        log=log,
        escalation_user_jid=None,
        sender_name="Bagas",
        user_message="jalankan script",
    )

    assert setup.sandbox is not None
    assert "sandbox" in setup.active_groups
    assert "deploy" in setup.active_groups

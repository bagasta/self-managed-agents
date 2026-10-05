from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.infra.computer_runtime import ComputerRuntime
from app.core.tools.computer_tool import build_computer_tools


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _runtime(**overrides) -> ComputerRuntime:
    base = dict(
        enabled=True,
        owner_id="owner-1",
        vnc_host="127.0.0.1",
        vnc_port=5902,
        viewer_url="http://127.0.0.1:6080/vnc.html",
        timeout_seconds=3,
        max_actions=2,
    )
    base.update(overrides)
    return ComputerRuntime(**base)


def test_computer_runtime_requires_the_assigned_owner(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())

    assert runtime.status(owner_id="another-owner")["code"] == "computer_not_assigned"
    ready = runtime.status(owner_id="owner-1")
    assert ready["ok"] is True
    assert ready["takeover_supported"] is True


def test_computer_runtime_opens_url_without_using_docker(monkeypatch):
    runtime = _runtime()
    calls = []
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.core.infra.computer_runtime.subprocess.run", fake_run)
    result = runtime.open_url(owner_id="owner-1", url="https://example.com/path")

    assert result["ok"] is True
    assert calls == [[
        "vncdotool", "-s", "127.0.0.1::5902", "key", "CTRL-L",
        "type", "https://example.com/path", "key", "ENTER",
    ]]


def test_computer_runtime_blocks_sensitive_input_and_action_overrun(monkeypatch):
    runtime = _runtime(max_actions=1)
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    monkeypatch.setattr(
        "app.core.infra.computer_runtime.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert runtime.type_text(owner_id="owner-1", text="password=secret")["code"] == "sensitive_input_blocked"
    assert runtime.press_key(owner_id="owner-1", key="CTRL-L")["ok"] is True
    assert runtime.press_key(owner_id="owner-1", key="ENTER")["code"] == "computer_action_limit"


def test_computer_tools_expose_only_bounded_vnc_actions():
    names = {item.name for item in build_computer_tools(_runtime(), owner_id="owner-1")}
    assert names == {
        "computer_get_status",
        "computer_open_url",
        "computer_click",
        "computer_type_text",
        "computer_press_key",
    }


@pytest.mark.asyncio
async def test_agent_tool_setup_adds_computer_without_replacing_docker(monkeypatch):
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    owner_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), owner_user_id=owner_id, capabilities=[])
    session = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent.id, channel_type="api", channel_config={}, external_user_id="owner",
    )
    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={
            "computer": True,
            "sandbox": False,
            "memory": False,
            "skills": False,
            "escalation": False,
            "tavily": False,
        },
        raw_tools_config={},
        db=AsyncMock(),
        log=MagicMock(),
        escalation_user_jid=None,
        sender_name=None,
        user_message="open browser",
    )

    assert "computer" in setup.active_groups
    assert setup.sandbox is None
    assert {item.name for item in setup.tools} == {
        "computer_get_status", "computer_open_url", "computer_click", "computer_type_text", "computer_press_key",
    }

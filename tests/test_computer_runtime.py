from __future__ import annotations

import asyncio
import base64
from pathlib import Path
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


def test_default_computer_budget_allows_a_multifield_visual_flow():
    from app.config import Settings

    assert Settings().computer_runtime_max_actions_per_run == 60


def test_computer_runtime_requires_the_assigned_owner(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())

    assert runtime.status(owner_id="another-owner")["code"] == "computer_not_assigned"
    ready = runtime.status(owner_id="owner-1")
    assert ready["ok"] is True
    assert ready["takeover_supported"] is True
    assert runtime.status(owner_id=None, is_platform_admin=True)["ok"] is True


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
    assert calls[0][1:] == [
        "-s", "127.0.0.1::5902", "key", "ctrl-l",
        "type", "https://example.com/path", "key", "enter",
    ]
    assert calls[0][0].endswith("vncdotool")


def test_computer_runtime_keeps_virtualenv_driver_path(monkeypatch, tmp_path):
    runtime = _runtime()
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    driver = venv_bin / "vncdotool"
    driver.touch()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    monkeypatch.setattr("app.core.infra.computer_runtime.sys.executable", str(venv_bin / "python"))
    calls = []
    monkeypatch.setattr(
        "app.core.infra.computer_runtime.subprocess.run",
        lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert runtime.press_key(owner_id="owner-1", key="ENTER")["ok"] is True
    assert calls[0][0] == str(driver)


def test_computer_runtime_blocks_sensitive_input_and_action_overrun(monkeypatch):
    runtime = _runtime(max_actions=1)
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    monkeypatch.setattr(
        "app.core.infra.computer_runtime.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    blocked = runtime.type_text(owner_id="owner-1", text="password=secret")
    assert blocked["code"] == "sensitive_input_blocked"
    assert blocked["requires_human_takeover"] is True
    assert blocked["continue_independent_tasks"] is True
    assert runtime.press_key(owner_id="owner-1", key="CTRL-L")["ok"] is True
    assert runtime.press_key(owner_id="owner-1", key="ENTER")["code"] == "computer_action_limit"


def test_computer_runtime_translates_friendly_key_names_for_vnc(monkeypatch):
    runtime = _runtime()
    commands = []
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    monkeypatch.setattr(
        "app.core.infra.computer_runtime.subprocess.run",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert runtime.press_key(owner_id="owner-1", key="ALT+TAB")["ok"] is True
    assert commands[0][-2:] == ["key", "alt-tab"]


def test_computer_runtime_captures_and_removes_the_vnc_frame(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())

    def fake_run(command, **_kwargs):
        assert command[-2] == "capture"
        Path(command[-1]).write_bytes(b"screen-bytes")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.core.infra.computer_runtime.subprocess.run", fake_run)
    screen = runtime.capture_screen(owner_id="owner-1")

    assert screen["ok"] is True
    assert base64.b64decode(screen["image_base64"]) == b"screen-bytes"


def test_visual_computer_tools_return_a_screen_after_an_action(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr(runtime, "open_url", lambda **_kwargs: {"ok": True, "status": "action_sent"})
    monkeypatch.setattr(runtime, "capture_screen", lambda **_kwargs: {"ok": True, "mime_type": "image/png", "image_base64": "c2NyZWVu"})
    tools = {item.name: item for item in build_computer_tools(runtime, owner_id="owner-1", visual_observation=True)}

    result = tools["computer_open_url"].invoke({"url": "https://example.com"})

    assert "computer_screenshot" in tools
    assert result[0]["type"] == "text"
    assert result[1]["image_url"]["url"] == "data:image/png;base64,c2NyZWVu"


def test_computer_tools_expose_only_bounded_vnc_actions():
    names = {item.name for item in build_computer_tools(_runtime(), owner_id="owner-1")}
    assert names == {
        "computer_get_status",
        "computer_open_url",
        "computer_click",
        "computer_type_text",
        "computer_request_human_takeover",
        "computer_press_key",
    }


def test_computer_handoff_preserves_the_running_automation(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())

    result = runtime.request_human_takeover(
        owner_id="owner-1",
        reason="Masukkan password pada halaman login.",
    )

    assert result["ok"] is True
    assert result["status"] == "human_takeover_required"
    assert result["viewer_url_available"] is True


def test_computer_api_allows_platform_admin_or_assigned_owner(monkeypatch):
    from app.api import computer as computer_api
    from app.api.workforce import WorkforcePrincipal

    runtime = _runtime()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    monkeypatch.setattr(computer_api.ComputerRuntime, "from_settings", lambda _settings: runtime)

    owner = computer_api._runtime_result(WorkforcePrincipal(owner_user_id="owner-1"))
    admin = computer_api._runtime_result(WorkforcePrincipal(owner_user_id=None, is_platform_admin=True))
    assert owner["ok"] is True
    assert admin["ok"] is True


def test_computer_api_snapshot_uses_the_same_vnc_runtime_for_admin(monkeypatch):
    from app.api import computer as computer_api
    from app.api.workforce import WorkforcePrincipal

    runtime = _runtime()
    monkeypatch.setattr("app.core.infra.computer_runtime.socket.create_connection", lambda *_args, **_kwargs: _Socket())
    monkeypatch.setattr(runtime, "capture_screen", lambda **kwargs: {
        "ok": True,
        "status": "screen_captured",
        "mime_type": "image/png",
        "image_base64": "c2NyZWVu",
        "is_platform_admin": kwargs["is_platform_admin"],
    })
    monkeypatch.setattr(computer_api.ComputerRuntime, "from_settings", lambda _settings: runtime)

    result = asyncio.run(
        computer_api.computer_snapshot(WorkforcePrincipal(owner_user_id=None, is_platform_admin=True))
    )

    assert result["image_base64"] == "c2NyZWVu"
    assert result["is_platform_admin"] is True


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
        "computer_get_status", "computer_open_url", "computer_click", "computer_type_text",
        "computer_request_human_takeover", "computer_press_key",
    }


@pytest.mark.asyncio
async def test_team_chat_does_not_expose_whatsapp_escalation_tools(monkeypatch):
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    owner_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), owner_user_id=owner_id, capabilities=[])
    session = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent.id, channel_type=None, channel_config={}, external_user_id="owner",
    )
    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={
            "computer": False,
            "sandbox": False,
            "memory": False,
            "skills": False,
            "escalation": True,
            "tavily": False,
        },
        raw_tools_config={},
        db=AsyncMock(),
        log=MagicMock(),
        escalation_user_jid=None,
        sender_name=None,
        user_message="Cari lowongan kerja yang relevan.",
    )

    assert "escalation" not in setup.active_groups
    assert "escalate_to_human" not in {item.name for item in setup.tools}


@pytest.mark.asyncio
async def test_gpt6_computer_agent_gets_visual_observation_tools(monkeypatch):
    """GPT-6 agents must observe the screen, not receive blind action_sent replies."""
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    owner_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        owner_user_id=owner_id,
        capabilities=[],
        model="openai/gpt-6-luna",
    )
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

    assert "computer_visual" in setup.active_groups
    assert "computer_screenshot" in {item.name for item in setup.tools}

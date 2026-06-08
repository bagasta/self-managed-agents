"""
test_deploy_path.py — Verifikasi runtime wiring untuk coding/deploy path.

Run with:
    PYTHONPATH=/path/to/project pytest tests/test_deploy_path.py -v
    # or:
    PYTHONPATH=/path/to/project python tests/test_deploy_path.py

Tests:
  1. _build_system_subagent: sys_coder returns CompiledSubAgent (runnable key)
  2. _build_system_subagent: sys_writer returns plain SubAgent dict (system_prompt key)
  3. _build_system_subagent: workspace isolation — sub_sandbox workspace != parent workspace
  4. build_deployment_tools: deploy_app uses sub_sandbox.workspace_dir (not a different dir)
  5. DockerBackend: write() writes to correct workspace_dir
  6. build_subagents: custom DB sandbox subagent returns CompiledSubAgent
  7. Integration check: verify no workspace mismatch between write_file backend and deploy_app

Run with:
    pytest tests/test_deploy_path.py -v
    # or without pytest:
    python tests/test_deploy_path.py
"""
from __future__ import annotations

import asyncio
import sys
import pathlib

# Ensure project root is on path when run directly
_project_root = str(pathlib.Path(__file__).parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: mock DockerSandbox without Docker
# ---------------------------------------------------------------------------

def _make_mock_sandbox(session_id: str) -> MagicMock:
    sandbox = MagicMock()
    sandbox.session_id = session_id
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
    # Use a real temp dir that persists for the test duration
    _tmpdir = tempfile.mkdtemp()
    workspace = Path(_tmpdir) / "workspace"
    workspace.mkdir()
    sandbox.workspace_dir = workspace
    sandbox.bash = MagicMock(return_value="ok")
    sandbox.write_file = MagicMock()
    sandbox.write_binary_file = MagicMock(return_value="ok")
    return sandbox


# ---------------------------------------------------------------------------
# Test 1: sys_coder returns CompiledSubAgent (has 'runnable', no 'system_prompt')
# ---------------------------------------------------------------------------

def test_sys_coder_returns_compiled_subagent():
    """sys_coder (sandbox=True, deploy=True) must be returned as a CompiledSubAgent."""
    parent_session_id = uuid.uuid4()

    sys_coder_spec = None
    from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS
    for spec in _SYSTEM_SUBAGENTS:
        if spec["name"] == "sys_coder":
            sys_coder_spec = spec
            break

    assert sys_coder_spec is not None, "sys_coder not in _SYSTEM_SUBAGENTS"
    assert sys_coder_spec["tools_config"]["sandbox"] is True
    assert sys_coder_spec["tools_config"]["deploy"] is True

    mock_sandbox = _make_mock_sandbox(f"{parent_session_id}_sys_sys_coder")

    with (
        patch("app.core.engine.subagent_builder.DockerSandbox", return_value=mock_sandbox),
        patch("app.core.engine.subagent_builder.build_sandbox_binary_tool", return_value=[]),
        patch("app.core.engine.subagent_builder.build_deployment_tools", return_value=[]),
        patch("deepagents.create_deep_agent", return_value=MagicMock()) as mock_cda,
    ):
        from app.core.engine.subagent_builder import _build_system_subagent
        sa, ssb = _build_system_subagent(sys_coder_spec, parent_session_id)

    # Must be a CompiledSubAgent (has 'runnable', NOT 'system_prompt' at top level)
    assert "runnable" in sa, f"Expected CompiledSubAgent with 'runnable' key, got: {list(sa.keys())}"
    assert "system_prompt" not in sa, "CompiledSubAgent should not have 'system_prompt' (it's inside the compiled graph)"
    assert sa["name"] == "sys_coder"
    assert ssb is not None, "sub_sandbox should be returned for cleanup"

    # create_deep_agent must have been called with backend= (DockerBackend)
    mock_cda.assert_called_once()
    call_kwargs = mock_cda.call_args.kwargs
    assert "backend" in call_kwargs, "create_deep_agent for sys_coder must receive backend= kwarg"
    print("✓ sys_coder returns CompiledSubAgent with backend")


def test_sys_coder_web_prompt_forces_vanilla_static_files():
    """Web work must stay lightweight: no frontend framework, no inline CSS/JS."""
    from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS

    sys_coder_spec = next(s for s in _SYSTEM_SUBAGENTS if s["name"] == "sys_coder")
    prompt = sys_coder_spec["system_prompt"].lower()

    assert "vanilla html, css, dan javascript saja" in prompt
    assert "index.html" in prompt
    assert "styles.css" in prompt
    assert "script.js" in prompt
    assert "jangan inline css" in prompt
    assert "jangan inline javascript" in prompt
    assert "jangan pakai react" in prompt
    assert "tailwind" in prompt
    assert "npm" in prompt


def test_builder_coding_preset_instructs_vanilla_web_stack():
    """Arthur-created coding agents should pass the same lightweight web rule to sys_coder."""
    from app.core.tools.builder_tools import AGENT_PRESETS, _SOUL_TEMPLATES

    skeleton = AGENT_PRESETS["coding_deploy_agent"]["instruction_skeleton"].lower()
    soul_template = _SOUL_TEMPLATES["coding_deploy_agent"].lower()

    for text in (skeleton, soul_template):
        assert "vanilla html/css/javascript" in text
        assert "index.html" in text
        assert "styles.css" in text
        assert "script.js" in text
        assert "tanpa framework" in text or "jangan pakai react" in text


# ---------------------------------------------------------------------------
# Test 2: sys_writer returns plain SubAgent dict (no sandbox)
# ---------------------------------------------------------------------------

def test_sys_writer_returns_plain_subagent():
    """sys_writer (sandbox=False) must be returned as plain SubAgent dict with 'system_prompt'."""
    parent_session_id = uuid.uuid4()

    sys_writer_spec = None
    from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS
    for spec in _SYSTEM_SUBAGENTS:
        if spec["name"] == "sys_writer":
            sys_writer_spec = spec
            break

    assert sys_writer_spec is not None
    assert not sys_writer_spec["tools_config"].get("sandbox", False)

    from app.core.engine.subagent_builder import _build_system_subagent
    sa, ssb = _build_system_subagent(sys_writer_spec, parent_session_id)

    assert "system_prompt" in sa, "Non-sandbox subagent should be plain SubAgent with 'system_prompt'"
    assert "runnable" not in sa
    assert ssb is None, "Non-sandbox subagent should not create a DockerSandbox"
    print("✓ sys_writer returns plain SubAgent dict (no sandbox)")


# ---------------------------------------------------------------------------
# Test 3: Workspace isolation — sub_sandbox uses different dir than parent
# ---------------------------------------------------------------------------

def test_workspace_isolation():
    """sub_sandbox for sys_coder must use a different session_id/dir than parent."""
    parent_session_id = uuid.uuid4()
    parent_str = str(parent_session_id)

    captured_session_ids = []

    def fake_docker_sandbox(session_id, parent_session_id=None):
        captured_session_ids.append(str(session_id))
        return _make_mock_sandbox(str(session_id))

    sys_coder_spec = next(s for s in __import__("app.core.engine.subagent_builder", fromlist=["_SYSTEM_SUBAGENTS"])._SYSTEM_SUBAGENTS if s["name"] == "sys_coder")

    with (
        patch("app.core.engine.subagent_builder.DockerSandbox", side_effect=fake_docker_sandbox),
        patch("app.core.engine.subagent_builder.build_sandbox_binary_tool", return_value=[]),
        patch("app.core.engine.subagent_builder.build_deployment_tools", return_value=[]),
        patch("deepagents.create_deep_agent", return_value=MagicMock()),
    ):
        from app.core.engine.subagent_builder import _build_system_subagent
        _build_system_subagent(sys_coder_spec, parent_session_id)

    assert len(captured_session_ids) == 1
    sub_session_id = captured_session_ids[0]
    assert sub_session_id != parent_str, "sub_sandbox session_id must differ from parent"
    assert parent_str in sub_session_id, f"sub_sandbox id should contain parent id, got: {sub_session_id}"
    assert "sys_coder" in sub_session_id
    print(f"✓ Workspace isolation: parent={parent_str[:8]}..., sub={sub_session_id[:8]}...")


# ---------------------------------------------------------------------------
# Test 4: deploy_app workspace matches write_file workspace
# ---------------------------------------------------------------------------

def test_deploy_and_write_same_workspace():
    """
    The workspace used by deploy_app must be the same as what DockerBackend exposes
    for write_file. Both must point to sub_sandbox.workspace_dir.
    """
    from app.core.engine.deep_agent_backend import DockerBackend

    sub_sandbox = _make_mock_sandbox("test-sub-session")
    sub_backend = DockerBackend(sub_sandbox)

    # write_file via DockerBackend writes to sub_sandbox.workspace_dir
    test_file = "index.html"
    test_content = "<h1>Hello</h1>"
    result = sub_backend.write(test_file, test_content)

    assert result.error is None, f"write() failed: {result.error}"
    expected_path = sub_sandbox.workspace_dir / test_file
    assert expected_path.exists(), f"File not written to sub_sandbox.workspace_dir: {expected_path}"
    assert expected_path.read_text() == test_content

    # deploy_app (via build_deployment_tools) also uses sub_sandbox.workspace_dir
    # Verify by checking what workspace_dir build_deployment_tools captures
    from app.core.tools.deployment_tools import build_deployment_tools
    from app.config import get_settings
    settings = get_settings()

    deploy_tools = build_deployment_tools(
        session_id=sub_sandbox.session_id,
        workspace_dir=sub_sandbox.workspace_dir,
        sandbox_image=settings.docker_sandbox_image,
    )

    # deploy_app tool closure captures workspace_dir = sub_sandbox.workspace_dir
    deploy_app_tool = next(t for t in deploy_tools if t.name == "deploy_app")

    # The tool's closure should capture the same workspace_dir as the backend
    # We can't easily introspect the closure, but we can verify the tool exists
    # and the captured wdir matches via a mock deploy call
    captured_workspaces = []

    def fake_deploy_app(session_id, workspace_dir, command, port, sandbox_image):
        captured_workspaces.append(workspace_dir)
        return {"url": "https://test.trycloudflare.com", "status": "running", "command": command}

    with patch("app.core.tools.deployment_tools._svc.deploy_app", side_effect=fake_deploy_app):
        deploy_app_tool.invoke({"command": "python3 -m http.server 8080", "port": 8080})

    assert len(captured_workspaces) == 1
    assert captured_workspaces[0] == sub_sandbox.workspace_dir, (
        f"deploy_app workspace mismatch!\n"
        f"  deploy_app workspace: {captured_workspaces[0]}\n"
        f"  sub_sandbox.workspace_dir: {sub_sandbox.workspace_dir}\n"
        f"  This means write_file and deploy_app point to different directories!"
    )
    print(f"✓ deploy_app and write_file both target: {sub_sandbox.workspace_dir}")


# ---------------------------------------------------------------------------
# Test 5: DockerBackend.write() correctly writes to workspace_dir
# ---------------------------------------------------------------------------

def test_docker_backend_write():
    """DockerBackend.write() must write to the correct path inside workspace_dir."""
    from app.core.engine.deep_agent_backend import DockerBackend

    sandbox = _make_mock_sandbox("test-backend-session")
    backend = DockerBackend(sandbox)

    result = backend.write("subdir/test.py", "print('hello')")
    assert result.error is None
    assert result.path == "subdir/test.py"
    expected = sandbox.workspace_dir / "subdir" / "test.py"
    assert expected.exists()
    assert expected.read_text() == "print('hello')"
    print("✓ DockerBackend.write() writes to correct workspace path")


@pytest.mark.asyncio
async def test_parent_deploy_tools_require_deploy_enabled(monkeypatch):
    """sandbox:true alone must not expose public deployment tools."""
    from app.core.engine import agent_tool_setup
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    fake_sandbox = _make_mock_sandbox("parent-session")
    deploy_called = False

    def fake_build_deployment_tools(sandbox):
        nonlocal deploy_called
        deploy_called = True
        return [SimpleNamespace(name="deploy_app")]

    monkeypatch.setattr(agent_tool_setup, "DockerSandbox", lambda sid: fake_sandbox)
    monkeypatch.setattr(agent_tool_setup, "build_sandbox_binary_tool", lambda sandbox: [SimpleNamespace(name="sandbox_write_binary_file")])
    monkeypatch.setattr(agent_tool_setup, "build_deployment_tools", fake_build_deployment_tools)

    agent = SimpleNamespace(id=uuid.uuid4(), capabilities=[])
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        channel_type="api",
        channel_config={},
        external_user_id="628111",
    )
    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={
            "sandbox": True,
            "deploy": False,
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
        user_message="run local script",
    )

    assert deploy_called is False
    assert "sandbox" in setup.active_groups
    assert "deploy" not in setup.active_groups
    assert {tool.name for tool in setup.tools} == {"sandbox_write_binary_file"}


def test_docker_sandbox_blocks_path_traversal(tmp_path):
    """Direct DockerSandbox file helpers must not write outside workspace."""
    from app.core.infra.sandbox import DockerSandbox

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.session_id = "test"
    sandbox.workspace_dir = tmp_path / "workspace"
    sandbox.workspace_dir.mkdir()
    sandbox.shared_dir = sandbox.workspace_dir / "shared"
    sandbox.shared_dir.mkdir()
    sandbox.parent_session_id = None

    result = sandbox.write_file("../escape.txt", "owned")

    assert result.startswith("[error]")
    assert not (tmp_path / "escape.txt").exists()


def test_deploy_followup_needed_after_subagent_codes_without_url():
    """deploy=true website runs must continue if code exists but no public URL was returned."""
    from app.core.engine.agent_runner import _needs_deploy_followup

    steps = [
        {
            "tool": "task",
            "args": {"name": "sys_coder"},
            "result": "Saya sudah membuat file /workspace/src/index.html dan /workspace/src/styles.css.",
        }
    ]

    assert _needs_deploy_followup(
        "buatkan website portfolio modern",
        {"deploy": True},
        steps,
        "Website sudah dibuat di sandbox.",
    ) is True


def test_deploy_followup_not_needed_when_public_url_exists():
    """A returned deploy URL is the completion proof; do not redeploy."""
    from app.core.engine.agent_runner import _needs_deploy_followup

    steps = [
        {
            "tool": "task",
            "result": "Deploy berhasil: https://abc-def.trycloudflare.com",
        }
    ]

    assert _needs_deploy_followup(
        "buatkan website portfolio modern",
        {"deploy": True},
        steps,
        "Ini URL-nya: https://abc-def.trycloudflare.com",
    ) is False


def test_deploy_followup_requires_deploy_enabled():
    """Sandbox coding agents without deploy=true should not be forced into public tunnels."""
    from app.core.engine.agent_runner import _needs_deploy_followup

    steps = [
        {
            "tool": "task",
            "result": "File /workspace/src/index.html sudah dibuat.",
        }
    ]

    assert _needs_deploy_followup(
        "buatkan website portfolio modern",
        {"deploy": False},
        steps,
        "Website sudah dibuat.",
    ) is False


def test_deploy_followup_ignores_non_website_requests():
    """Code evidence alone is not enough; the user request must need a web/app URL."""
    from app.core.engine.agent_runner import _needs_deploy_followup

    steps = [
        {
            "tool": "execute",
            "result": "created report.csv",
        }
    ]

    assert _needs_deploy_followup(
        "buatkan script analisis csv",
        {"deploy": True},
        steps,
        "Script sudah dibuat.",
    ) is False


# ---------------------------------------------------------------------------
# Test 6: build_subagents with system defaults includes sys_coder as CompiledSubAgent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_subagents_system_defaults_compiled():
    """build_subagents with empty agent_ids should produce sys_coder as CompiledSubAgent."""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=Exception("no seeded system agents"))

    mock_log = MagicMock()
    mock_log.info = MagicMock()

    parent_session_id = uuid.uuid4()

    with (
        patch("app.core.engine.subagent_builder.DockerSandbox", side_effect=lambda sid, parent_session_id=None: _make_mock_sandbox(str(sid))),
        patch("app.core.engine.subagent_builder.build_sandbox_binary_tool", return_value=[]),
        patch("app.core.engine.subagent_builder.build_deployment_tools", return_value=[]),
        patch("app.core.engine.subagent_builder.build_http_tools", return_value=[]),
        patch("deepagents.create_deep_agent", return_value=MagicMock()),
    ):
        from app.core.engine import subagent_builder

        subagents, sandboxes = await subagent_builder.build_subagents(
            agent_ids=[],
            parent_session_id=parent_session_id,
            db=mock_db,
            log=mock_log,
        )

    # sys_coder and sys_analyst should be CompiledSubAgents
    sandbox_agents = {"sys_coder", "sys_analyst"}
    plain_agents = {"sys_critic", "sys_researcher", "sys_writer", "sys_system_message_builder"}

    for sa in subagents:
        name = sa["name"]
        if name in sandbox_agents:
            assert "runnable" in sa, f"{name} should be CompiledSubAgent with 'runnable'"
            assert "system_prompt" not in sa
        elif name in plain_agents:
            assert "system_prompt" in sa, f"{name} should be plain SubAgent with 'system_prompt'"
            assert "runnable" not in sa

    # Sandboxes should be returned for cleanup
    assert len(sandboxes) >= 2  # sys_coder + sys_analyst at minimum
    print(f"✓ build_subagents: {len(subagents)} subagents built, {len(sandboxes)} sandboxes registered for cleanup")


@pytest.mark.asyncio
async def test_custom_subagent_memory_uses_parent_user_scope():
    """Custom subagent memory must not fall back to agent-global scope."""
    from app.core.engine import subagent_builder

    agent_id = uuid.uuid4()
    parent_scope = "628111111111@s.whatsapp.net"
    agent_row = SimpleNamespace(
        id=agent_id,
        name="Custom Researcher",
        description="",
        instructions="You help research.",
        model="openai/gpt-4o-mini",
        temperature=0.3,
        tools_config={
            "memory": True,
            "skills": False,
            "http": False,
            "tavily": False,
            "sandbox": False,
        },
        is_deleted=False,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = agent_row
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    captured: list[tuple[uuid.UUID, str | None]] = []

    def fake_build_memory_tools(agent_id_arg, _db_factory, scope=None):
        captured.append((agent_id_arg, scope))
        return []

    with (
        patch("app.core.engine.subagent_builder.build_memory_tools", side_effect=fake_build_memory_tools),
        patch("app.core.engine.subagent_builder._make_sub_llm", return_value=MagicMock()),
    ):
        subagents, sandboxes = await subagent_builder.build_subagents(
            agent_ids=[str(agent_id)],
            parent_session_id=uuid.uuid4(),
            db=db,
            log=MagicMock(),
            memory_scope=parent_scope,
        )

    assert [s["name"] for s in subagents] == ["Custom Researcher"]
    assert sandboxes == []
    assert captured == [(agent_id, parent_scope)]


# ---------------------------------------------------------------------------
# Main runner (for running without pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_sys_coder_returns_compiled_subagent,
        test_sys_writer_returns_plain_subagent,
        test_workspace_isolation,
        test_deploy_and_write_same_workspace,
        test_docker_backend_write,
    ]

    async def run_async_test():
        await test_build_subagents_system_defaults_compiled()

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"✗ {test_fn.__name__}: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    try:
        asyncio.run(run_async_test())
        passed += 1
    except Exception as exc:
        print(f"✗ test_build_subagents_system_defaults_compiled: {exc}")
        import traceback
        traceback.print_exc()
        failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

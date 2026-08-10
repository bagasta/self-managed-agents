"""Tests for the orphan sandbox reaper and graceful subagent-compile degradation."""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.infra import sandbox as sandbox_mod


def _settings(tmp_path, container_ttl=900, workspace_ttl=86400):
    return SimpleNamespace(
        sandbox_base_dir=str(tmp_path),
        sandbox_host_base_dir="",
        docker_host="unix:///run/docker.sock",
        docker_sandbox_image="img",
        max_concurrent_sandboxes=6,
        sandbox_mem_limit="1g",
        sandbox_nano_cpus=1_000_000_000,
        sandbox_container_ttl_seconds=container_ttl,
        sandbox_workspace_ttl_seconds=workspace_ttl,
    )


def test_reaper_removes_old_workspace_dirs(tmp_path, monkeypatch):
    """Idle workspace dirs older than TTL are removed; fresh ones are kept."""
    monkeypatch.setattr(sandbox_mod, "get_settings", lambda: _settings(tmp_path, workspace_ttl=3600))

    old = tmp_path / "old-session"
    old.mkdir()
    fresh = tmp_path / "fresh-session"
    fresh.mkdir()

    # Age the old dir beyond TTL.
    stale = time.time() - 7200
    import os
    os.utime(old, (stale, stale))

    # Docker unavailable here is fine — reaper logs and continues to dir cleanup.
    with patch.object(sandbox_mod, "_connect_docker", side_effect=Exception("no docker")):
        result = sandbox_mod.cleanup_orphan_sandboxes()

    assert not old.exists()
    assert fresh.exists()
    assert result["workspace_dirs_removed"] == 1


def test_reaper_kills_old_containers(tmp_path, monkeypatch):
    """Labeled containers older than TTL are force-removed; recent ones are kept."""
    monkeypatch.setattr(sandbox_mod, "get_settings", lambda: _settings(tmp_path, container_ttl=900))

    old_ts = "2000-01-01T00:00:00.000000000Z"
    new_ts = "2999-01-01T00:00:00.000000000Z"
    old_c = MagicMock()
    old_c.attrs = {"Created": old_ts}
    new_c = MagicMock()
    new_c.attrs = {"Created": new_ts}

    client = MagicMock()
    client.containers.list.return_value = [old_c, new_c]

    with patch.object(sandbox_mod, "_connect_docker", return_value=client):
        result = sandbox_mod.cleanup_orphan_sandboxes()

    old_c.remove.assert_called_once_with(force=True)
    new_c.remove.assert_not_called()
    assert result["containers_killed"] == 1


def test_parse_docker_ts_handles_nanoseconds():
    ts = sandbox_mod._parse_docker_ts("2026-06-23T10:00:00.123456789Z")
    assert ts is not None and ts > 0
    assert sandbox_mod._parse_docker_ts(None) is None
    assert sandbox_mod._parse_docker_ts("garbage") is None


@pytest.mark.asyncio
async def test_subagent_compile_failure_skips_not_crashes():
    """A subagent that fails to compile is skipped; the rest of the run survives."""
    from app.core.engine import subagent_builder

    parent_session_id = uuid.uuid4()
    mock_db = MagicMock()

    async def _execute(*a, **k):
        raise Exception("no seeded system agents")

    mock_db.execute = _execute

    def _mock_sandbox(sid, parent_session_id=None):
        sb = MagicMock()
        sb.session_id = str(sid)
        sb.close = MagicMock()
        return sb

    with (
        patch("app.core.engine.subagent_builder.DockerSandbox", side_effect=_mock_sandbox),
        patch("app.core.engine.subagent_builder.build_sandbox_binary_tool", return_value=[]),
        patch("app.core.engine.subagent_builder.build_deployment_tools", return_value=[]),
        patch("app.core.engine.subagent_builder.build_http_tools", return_value=[]),
        # Force compile failure for sandbox subagents.
        patch("deepagents.create_deep_agent", side_effect=TypeError("backend mismatch")),
    ):
        subagents, sandboxes = await subagent_builder.build_subagents(
            agent_ids=[],
            parent_session_id=parent_session_id,
            db=mock_db,
            log=MagicMock(),
        )

    names = {s["name"] for s in subagents}
    # Sandbox subagents (sys_coder/sys_analyst) were skipped on compile failure.
    assert "sys_coder" not in names
    assert "sys_analyst" not in names
    # Non-sandbox subagents still present → run is alive.
    assert "sys_writer" in names
    assert sandboxes == []

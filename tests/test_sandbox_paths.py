"""Tests for host-path translation used by DinD (sibling-container) bind mounts."""
from __future__ import annotations

from types import SimpleNamespace

from app.core.infra import sandbox_paths


def _patch_settings(monkeypatch, base: str, host: str) -> None:
    settings = SimpleNamespace(sandbox_base_dir=base, sandbox_host_base_dir=host)
    monkeypatch.setattr(sandbox_paths, "get_settings", lambda: settings)


def test_no_translation_when_host_base_empty(monkeypatch):
    """Dev / app-on-host: empty host base => path returned unchanged."""
    _patch_settings(monkeypatch, base="/tmp/agent-sandboxes", host="")
    p = "/tmp/agent-sandboxes/sess-1/workspace"
    assert sandbox_paths.to_host_path(p) == p


def test_translates_path_under_base(monkeypatch):
    """A path under the internal base maps to the same relative location under host base."""
    _patch_settings(monkeypatch, base="/tmp/agent-sandboxes", host="/opt/host-sandboxes")
    result = sandbox_paths.to_host_path("/tmp/agent-sandboxes/sess-1/shared/out.txt")
    assert result == "/opt/host-sandboxes/sess-1/shared/out.txt"


def test_path_outside_base_returned_unchanged(monkeypatch):
    """Paths not under the internal base are not part of the translated tree."""
    _patch_settings(monkeypatch, base="/tmp/agent-sandboxes", host="/opt/host-sandboxes")
    assert sandbox_paths.to_host_path("/var/data/x") == "/var/data/x"


def test_accepts_path_object(monkeypatch):
    """Path objects are accepted and stringified."""
    from pathlib import Path

    _patch_settings(monkeypatch, base="/tmp/agent-sandboxes", host="/opt/host-sandboxes")
    result = sandbox_paths.to_host_path(Path("/tmp/agent-sandboxes/sess-2"))
    assert result == "/opt/host-sandboxes/sess-2"

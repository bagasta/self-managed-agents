"""Host-path translation for Docker bind mounts under Docker-in-Docker (sibling) setups.

When the app runs inside a container and spawns sandbox/deploy containers as siblings via
the mounted host Docker socket, bind-mount sources in `containers.run(volumes=...)` are
resolved by the HOST daemon, not by the app container's filesystem. If the app writes files
to `sandbox_base_dir` (e.g. a named volume mounted in the app container) but spawns a
container with that same path as the bind source, the daemon mounts a *different* host
directory — files written by the app are invisible to the sandbox and vice versa.

`to_host_path()` rewrites an app-internal path under `sandbox_base_dir` to the equivalent
host path under `sandbox_host_base_dir`, so both sides target the same directory.

When `sandbox_host_base_dir` is empty (dev / app-on-host), translation is a no-op.
"""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings


def to_host_path(internal_path: str | Path) -> str:
    """Translate an app-internal sandbox path to the host path used as a bind source.

    - If `sandbox_host_base_dir` is unset, returns the input unchanged (no translation).
    - If `internal_path` is under `sandbox_base_dir`, returns the same relative location
      under `sandbox_host_base_dir`.
    - If `internal_path` is not under `sandbox_base_dir`, returns it unchanged (best effort;
      such paths are not part of the translated tree).
    """
    settings = get_settings()
    host_base = (settings.sandbox_host_base_dir or "").strip()
    if not host_base:
        return str(internal_path)

    base = Path(settings.sandbox_base_dir).resolve()
    target = Path(internal_path).resolve()
    try:
        rel = target.relative_to(base)
    except ValueError:
        return str(internal_path)
    return str(Path(host_base) / rel)

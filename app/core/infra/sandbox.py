"""
Docker sandbox manager.

Design: ephemeral container per message + persistent workspace directory per session.

Each call to sandbox.bash() starts a fresh Docker container with
{SANDBOX_BASE_DIR}/{session_id}/ mounted at /workspace, runs the command,
captures output, then removes the container.

Files written in one turn are visible in the next because the directory
lives on the host between message calls.

Sandbox runs with FULL access:
  - Full internet connectivity (bridge network, no firewall)
  - Can install any package via pip/apt/npm (pip installs persist in /workspace/.pyvenv)
  - 1 CPU core, 1 GB RAM
  - No capability restrictions

For gVisor isolation in dev: install gVisor (runsc), configure Docker daemon.json,
then set DOCKER_RUNTIME=runsc. Default is the standard Docker runtime.
"""
from __future__ import annotations

import asyncio
import functools
import os
import shlex
import uuid
from pathlib import Path

import docker
import docker.errors
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

# Socket paths to probe in order when the configured path is unavailable.
_FALLBACK_SOCKETS = [
    "unix:///run/docker.sock",
    "unix:///var/run/docker.sock",
]


def _connect_docker(preferred: str) -> docker.DockerClient:
    """
    Try to connect to Docker using `preferred` first, then fallback sockets.
    Skips paths whose socket file does not exist to give a fast, clear error.
    """
    candidates = [preferred] + [s for s in _FALLBACK_SOCKETS if s != preferred]
    last_exc: Exception | None = None
    for url in candidates:
        # Strip the unix:// prefix to get the filesystem path for existence check
        sock_path = url.removeprefix("unix://")
        if not Path(sock_path).exists():
            continue
        try:
            client = docker.DockerClient(base_url=url)
            client.ping()          # fast connectivity check
            logger.debug("sandbox.docker.connected", socket=url)
            return client
        except Exception as exc:
            last_exc = exc
            continue
    raise docker.errors.DockerException(
        f"Could not connect to Docker. Tried: {candidates}. Last error: {last_exc}"
    )


_WORKSPACE_SUBDIRS = ("output", "src", "data", "assets", "tmp", "shared")


def get_workspace_dir(session_id: str | uuid.UUID) -> Path:
    settings = get_settings()
    workspace = Path(settings.sandbox_base_dir) / str(session_id)
    workspace.mkdir(parents=True, exist_ok=True)
    for sub in _WORKSPACE_SUBDIRS:
        (workspace / sub).mkdir(exist_ok=True)
    return workspace


def get_shared_dir(parent_session_id: str | uuid.UUID) -> Path:
    """Return the per-session shared directory used for cross-subagent collaboration.

    Scoped to parent_session_id, which is unique per user session, so cross-user
    leakage is impossible — different users always have different session ids.
    """
    return get_workspace_dir(parent_session_id) / "shared"


class DockerSandbox:
    """
    Manages sandbox operations for one agent session.

    All file operations (write_file, read_file, list_files) act directly on the
    host workspace directory — no container needed. Only bash() spins up a container.
    """

    def __init__(
        self,
        session_id: str | uuid.UUID,
        parent_session_id: str | uuid.UUID | None = None,
    ) -> None:
        self.session_id = str(session_id)
        self.workspace_dir = get_workspace_dir(session_id)
        self._settings = get_settings()
        self._client: docker.DockerClient | None = None

        # Per-parent-session shared dir. Subagents pass parent_session_id so they
        # all see the same /workspace/shared mount (collaboration). Main agent's
        # parent_session_id is None — its shared dir lives inside its own workspace.
        self.parent_session_id: str | None = (
            str(parent_session_id) if parent_session_id else None
        )
        if self.parent_session_id:
            self.shared_dir = get_shared_dir(self.parent_session_id)
            # Replace empty `shared/` subdir with a symlink to parent's shared dir
            # so host-side filesystem ops (DockerBackend.read/write) and the container
            # mount agree on a single location.
            local_shared = self.workspace_dir / "shared"
            try:
                if local_shared.is_symlink():
                    if local_shared.resolve() != self.shared_dir.resolve():
                        local_shared.unlink()
                        local_shared.symlink_to(self.shared_dir, target_is_directory=True)
                elif local_shared.is_dir() and not any(local_shared.iterdir()):
                    local_shared.rmdir()
                    local_shared.symlink_to(self.shared_dir, target_is_directory=True)
            except OSError as exc:
                logger.warning(
                    "sandbox.shared_symlink_failed",
                    session_id=self.session_id,
                    error=str(exc),
                )
        else:
            self.shared_dir = self.workspace_dir / "shared"

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = _connect_docker(self._settings.docker_host)
        return self._client

    def bash_result(self, cmd: str, timeout: int | None = None) -> tuple[str, int | None]:
        """
        Run a bash command in an ephemeral Docker container.
        The sandbox workspace is mounted at /workspace inside the container.
        """
        client = self._get_client()
        
        # Check concurrent limits to prevent host CPU/Memory exhaustion
        try:
            running = client.containers.list(filters={"label": "managed-agent-sandbox=true"})
            if len(running) >= self._settings.max_concurrent_sandboxes:
                return f"[sandbox error] Too many concurrent sandbox executions ({len(running)}). Try again later."
        except Exception as e:
            logger.warning("sandbox.bash.check_running_failed", error=str(e))

        log = logger.bind(session_id=self.session_id, cmd_preview=cmd[:120])
        log.debug("sandbox.bash.start")

        # PYTHONUSERBASE points inside /workspace/.pyvenv so pip installs
        # survive across ephemeral container runs (only /workspace is mounted
        # on the host). Agents can install anything — full internet is available.
        pyvenv_dir = "/workspace/.pyvenv"
        volumes: dict = {
            str(self.workspace_dir): {"bind": "/workspace", "mode": "rw"}
        }
        # Subagent: mount parent's shared dir explicitly so /workspace/shared works
        # inside the container (the on-host symlink doesn't resolve across the bind boundary).
        if self.parent_session_id:
            volumes[str(self.shared_dir)] = {"bind": "/workspace/shared", "mode": "rw"}

        actual_cmd = cmd
        if timeout is not None and timeout > 0:
            actual_cmd = f"timeout {int(timeout)}s bash -lc {shlex.quote(cmd)}"

        run_kwargs: dict = dict(
            image=self._settings.docker_sandbox_image,
            command=["bash", "-c", actual_cmd],
            volumes=volumes,
            working_dir="/workspace",
            environment={
                # Pip installs land here and persist between messages
                "PYTHONUSERBASE": pyvenv_dir,
                "PATH": f"{pyvenv_dir}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "PIP_USER": "1",
            },
            # Full resource headroom — agents can build/compile/install freely
            mem_limit="1g",
            nano_cpus=int(1.0e9),  # 1 full CPU core
            # Full internet — bridge network with no firewall restrictions
            network_mode="bridge",
            # No capability restrictions: agents have full container privileges
            labels={"managed-agent-sandbox": "true", "managed-agent-session": self.session_id},
            remove=True,
            stdout=True,
            stderr=True,
        )

        # Optionally enable gVisor runtime (set DOCKER_RUNTIME=runsc in env)
        runtime = os.getenv("DOCKER_RUNTIME")
        if runtime:
            run_kwargs["runtime"] = runtime

        try:
            raw = client.containers.run(**run_kwargs)
            output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            log.debug("sandbox.bash.done", output_len=len(output))
            return output or "(no output)", 0
        except docker.errors.ContainerError as exc:
            stdout = getattr(exc, "stdout", None)
            stdout_text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (str(stdout) if stdout else "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            log.warning(
                "sandbox.bash.container_error",
                exit_status=exc.exit_status,
                stderr=stderr[:2000],
            )
            output = "\n".join(part for part in (stdout_text, stderr) if part)
            return f"[exit {exc.exit_status}]\n{output}", int(exc.exit_status)
        except docker.errors.ImageNotFound:
            log.error("sandbox.bash.image_not_found", image=self._settings.docker_sandbox_image)
            return f"[error] Docker image not found: {self._settings.docker_sandbox_image}", None
        except Exception as exc:
            log.error("sandbox.bash.error", error=str(exc))
            return f"[sandbox error] {exc}", None

    def bash(self, cmd: str) -> str:
        output, _exit_code = self.bash_result(cmd)
        return output

    def write_file(self, path: str, content: str) -> str:
        """Write content to a file in the workspace. Creates parent dirs as needed."""
        target = self.workspace_dir / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {path}"

    def write_binary_file(self, path: str, base64_content: str) -> str:
        """Decode base64 string and write as binary file to the workspace."""
        import base64
        target = self.workspace_dir / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = base64.b64decode(base64_content)
            target.write_bytes(raw)
            return f"Written {len(raw)} bytes to {path}"
        except Exception as exc:
            return f"[error] Failed to write binary file: {exc}"

    def read_file(self, path: str) -> str:
        """Read a file from the workspace."""
        target = self.workspace_dir / path.lstrip("/")
        if not target.exists():
            return f"[error] File not found: {path}"
        if not target.is_file():
            return f"[error] Not a file: {path}"
        return target.read_text(encoding="utf-8")

    def list_files(self, directory: str = ".") -> str:
        """List all files under a workspace directory."""
        target = self.workspace_dir / directory.lstrip("/")
        if not target.exists():
            return f"[error] Directory not found: {directory}"
        entries = sorted(p for p in target.rglob("*") if p.is_file() and not p.name.startswith("."))
        lines = [str(p.relative_to(self.workspace_dir)) for p in entries]
        return "\n".join(lines) if lines else "(empty)"

    async def abash(self, cmd: str) -> str:
        """Non-blocking wrapper around bash() — cancellable via asyncio.CancelledError.

        When the task is cancelled while a Docker container is running, we kill the
        container by its label so the blocking thread unblocks quickly.
        """
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, functools.partial(self.bash, cmd))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Kill any running sandbox containers belonging to this session
            try:
                client = self._get_client()
                containers = client.containers.list(
                    filters={"label": f"managed-agent-session={self.session_id}"}
                )
                for c in containers:
                    try:
                        c.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            raise

    async def abash_result(self, cmd: str, timeout: int | None = None) -> tuple[str, int | None]:
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, functools.partial(self.bash_result, cmd, timeout))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                client = self._get_client()
                containers = client.containers.list(
                    filters={"label": f"managed-agent-session={self.session_id}"}
                )
                for c in containers:
                    try:
                        c.kill()
                    except Exception:
                        pass
            except Exception:
                pass
            raise

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    async def aclose(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.close)

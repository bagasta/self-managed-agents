"""
Docker sandbox manager.

Design: ephemeral container per message + persistent workspace directory per session.

Each call to sandbox.bash() starts a fresh Docker container with
{SANDBOX_BASE_DIR}/{session_id}/ mounted at /workspace, runs the command,
captures output, then removes the container.

Files written in one turn are visible in the next because the directory
lives on the host between message calls.

For gVisor isolation in dev: install gVisor (runsc), configure Docker daemon.json,
then set DOCKER_RUNTIME=runsc. Default is the standard Docker runtime.
"""
from __future__ import annotations

import os
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


def get_workspace_dir(session_id: str | uuid.UUID) -> Path:
    settings = get_settings()
    workspace = Path(settings.sandbox_base_dir) / str(session_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


class DockerSandbox:
    """
    Manages sandbox operations for one agent session.

    All file operations (write_file, read_file, list_files) act directly on the
    host workspace directory — no container needed. Only bash() spins up a container.
    """

    def __init__(self, session_id: str | uuid.UUID) -> None:
        self.session_id = str(session_id)
        self.workspace_dir = get_workspace_dir(session_id)
        self._settings = get_settings()
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = _connect_docker(self._settings.docker_host)
        return self._client

    def bash(self, cmd: str) -> str:
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

        # PYTHONUSERBASE points inside /workspace so pip installs survive across
        # ephemeral container runs (only /workspace is mounted on the host).
        run_kwargs: dict = dict(
            image=self._settings.docker_sandbox_image,
            command=["bash", "-c", cmd],
            volumes={
                str(self.workspace_dir): {"bind": "/workspace", "mode": "rw"}
            },
            working_dir="/workspace",
            environment={},
            mem_limit="256m",
            nano_cpus=int(0.25e9),  # 25% CPU
            network_mode="bridge",
            security_opt=["no-new-privileges:true"],
            cap_drop=["ALL"],
            labels={"managed-agent-sandbox": "true"},
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
            return output or "(no output)"
        except docker.errors.ContainerError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            log.warning(
                "sandbox.bash.container_error",
                exit_status=exc.exit_status,
                stderr=stderr[:2000],
            )
            return f"[exit {exc.exit_status}]\n{stderr}"
        except docker.errors.ImageNotFound:
            log.error("sandbox.bash.image_not_found", image=self._settings.docker_sandbox_image)
            return f"[error] Docker image not found: {self._settings.docker_sandbox_image}"
        except Exception as exc:
            log.error("sandbox.bash.error", error=str(exc))
            return f"[sandbox error] {exc}"

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

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

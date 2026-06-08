"""
deployment_service.py — Manages persistent Docker containers + Cloudflare Quick Tunnels.

Each session can have one active deployment:
  - App container: named madeploy-app-{session_id[:12]}, mounts workspace
  - CF container:  named madeploy-cf-{session_id[:12]}, tunnels to app container

URL changes every time deploy_app() is called (Cloudflare Quick Tunnel limitation).

Resource limits:
  - MAX_DEPLOYMENTS: max concurrent deployments (oldest evicted when exceeded)
  - DEPLOYMENT_TTL_SECONDS: auto-kill deployments older than this (default: 24 hours)
    Deployed containers are automatically stopped after 24 hours to free resources.
"""
from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Any

import docker
import structlog

log = structlog.get_logger()

_CF_IMAGE = "cloudflare/cloudflared:latest"
_URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")

_MAX_DEPLOYMENTS = int(os.environ.get("MAX_DEPLOYMENTS", "10"))
_DEPLOYMENT_TTL_SECONDS = int(os.environ.get("DEPLOYMENT_TTL_SECONDS", str(24 * 3600)))
_DEPLOYMENT_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("DEPLOYMENT_CLEANUP_INTERVAL_SECONDS", "300"))

# module-level registry: session_id -> deployment state dict
_deployments: dict[str, dict[str, Any]] = {}

# Cached Docker client — reused across calls to avoid reconnect overhead
_docker_client: docker.DockerClient | None = None

# Matches: printf '...' > file  or  printf "..." > file  (with optional leading path/mkdir chain)
# Group 1: everything before the printf/echo token (e.g. "mkdir -p /workspace && ")
# Group 2: the file content between quotes
# Group 3: the output filename
# Group 4: remainder after the file write (e.g. " && python3 -m http.server 8080")
_PRINTF_RE = re.compile(
    r"^(.*?)"                           # prefix (mkdir, cd, etc.)
    r"(?:printf|echo)\s+"               # printf or echo keyword
    r"(?:\$?'((?:[^'\\]|\\.)*)'|"       # single-quoted content  (group 2a)
    r'"((?:[^"\\]|\\.)*)")'             # double-quoted content  (group 2b)
    r"\s*>\s*(\S+)"                     # redirect target        (group 3 or 4)
    r"(.*?)$",                          # suffix                 (group 5)
    re.DOTALL,
)


def _make_safe_command(command: str) -> str:
    """
    Detect printf/echo-based file-write patterns and replace them with a
    Python base64-decode write that is immune to shell quoting issues.
    """
    m = _PRINTF_RE.match(command)
    if not m:
        return command

    prefix = m.group(1) or ""
    raw_content: str = m.group(2) if m.group(2) is not None else (m.group(3) or "")
    raw_content = raw_content.replace("\\'", "'").replace('\\"', '"')
    outfile = m.group(4)
    suffix = m.group(5) or ""

    b64 = base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
    py_write = (
        f"python3 -c \"import base64,pathlib; "
        f"pathlib.Path('{outfile}').write_bytes(base64.b64decode('{b64}'))\""
    )
    safe = f"{prefix}{py_write}{suffix}"
    log.info("deployment.command_sanitized", original_len=len(command), safe_len=len(safe))
    return safe


def _short(session_id: str) -> str:
    return session_id.replace("-", "")[:12]


def _names(session_id: str) -> tuple[str, str, str]:
    s = _short(session_id)
    return f"madeploy-app-{s}", f"madeploy-cf-{s}", f"madeploy-net-{s}"


def _docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is not None:
        try:
            _docker_client.ping()
            return _docker_client
        except Exception:
            _docker_client = None

    for sock in ["unix:///var/run/docker.sock", "unix:///run/docker.sock"]:
        if Path(sock.removeprefix("unix://")).exists():
            try:
                c = docker.DockerClient(base_url=sock)
                c.ping()
                _docker_client = c
                return c
            except Exception:
                continue
    raise RuntimeError("Cannot connect to Docker daemon")


def _pull_cf_image(client: docker.DockerClient) -> None:
    try:
        client.images.get(_CF_IMAGE)
    except docker.errors.ImageNotFound:
        log.info("deployment.pulling_cf_image")
        client.images.pull(_CF_IMAGE)


def deployment_ttl_seconds() -> int:
    return _DEPLOYMENT_TTL_SECONDS


def deployment_cleanup_interval_seconds() -> int:
    return max(60, _DEPLOYMENT_CLEANUP_INTERVAL_SECONDS)


def _kill_existing(client: docker.DockerClient, session_id: str) -> int:
    app_name, cf_name, _ = _names(session_id)
    removed = 0
    for name in [cf_name, app_name]:
        try:
            c = client.containers.get(name)
            c.remove(force=True)
            removed += 1
            log.info("deployment.removed_container", name=name)
        except docker.errors.NotFound:
            pass
    return removed


def _evict_expired(client: docker.DockerClient) -> int:
    """Kill all madeploy-* containers older than TTL — checks Docker labels, not just in-memory registry.

    This handles containers from previous server restarts that are no longer in `_deployments`.
    """
    now = time.time()
    removed = 0

    # Evict from in-memory registry
    expired_in_mem = [
        sid for sid, state in list(_deployments.items())
        if now - state.get("deployed_at", 0) > _DEPLOYMENT_TTL_SECONDS
    ]
    for sid in expired_in_mem:
        removed += _kill_existing(client, sid)
        _deployments.pop(sid, None)
        log.info("deployment.evicted_expired", session_id=sid[:12])

    # Evict orphaned containers (from previous server restarts) using Docker label
    try:
        for c in client.containers.list(all=True, filters={"name": "madeploy-app-"}):
            label_ts = c.labels.get("madeploy.deployed_at")
            if label_ts:
                try:
                    age = now - float(label_ts)
                    if age > _DEPLOYMENT_TTL_SECONDS:
                        c.remove(force=True)
                        removed += 1
                        log.info("deployment.evicted_orphan_by_label", name=c.name, age_hours=round(age / 3600, 1))
                except (ValueError, Exception):
                    pass
            else:
                # No label = pre-label legacy container; remove if not in registry
                known_app_names = {s.get("app_container") for s in _deployments.values()}
                if c.name not in known_app_names:
                    c.remove(force=True)
                    removed += 1
                    log.info("deployment.evicted_orphan_no_label", name=c.name)
        # Also clean up orphaned CF containers
        for c in client.containers.list(all=True, filters={"name": "madeploy-cf-"}):
            known_cf_names = {s.get("cf_container") for s in _deployments.values()}
            label_ts = c.labels.get("madeploy.deployed_at")
            if label_ts:
                try:
                    age = now - float(label_ts)
                    if age > _DEPLOYMENT_TTL_SECONDS:
                        c.remove(force=True)
                        removed += 1
                        log.info("deployment.evicted_cf_orphan_by_label", name=c.name, age_hours=round(age / 3600, 1))
                except (ValueError, Exception):
                    pass
            elif c.name not in known_cf_names:
                c.remove(force=True)
                removed += 1
                log.info("deployment.evicted_cf_orphan", name=c.name)
    except Exception as exc:
        log.warning("deployment.evict_orphan_error", error=str(exc))
    return removed


def cleanup_expired_deployments() -> dict[str, Any]:
    """Public cleanup entrypoint for startup/background maintenance."""
    client = _docker()
    removed = _evict_expired(client)
    return {
        "status": "ok",
        "evicted": removed,
        "ttl_seconds": _DEPLOYMENT_TTL_SECONDS,
    }


def _get_cf_url(client: docker.DockerClient, cf_name: str, timeout: int = 40) -> str | None:
    """Poll cloudflared container logs until trycloudflare.com URL appears."""
    deadline = time.time() + timeout
    log_since = 0  # unix timestamp — only fetch new log lines each iteration
    while time.time() < deadline:
        try:
            container = client.containers.get(cf_name)
            # Fetch only logs since last check to avoid re-scanning growing output
            kwargs = {"stdout": True, "stderr": True}
            if log_since > 0:
                # Docker API expects a float/int timestamp or a datetime object
                kwargs["since"] = log_since
            logs = container.logs(**kwargs).decode("utf-8", errors="replace")
            log_since = time.time()  # Keep as float to avoid the strict '<class 'int'>' error in some docker SDK versions
            match = _URL_RE.search(logs)
            if match:
                return match.group(0)
        except docker.errors.NotFound:
            return None
        time.sleep(0.5)
    return None


def deploy_app(
    session_id: str,
    workspace_dir: Path,
    command: str,
    port: int,
    sandbox_image: str = "python:3.11-slim",
) -> dict[str, Any]:
    """
    Start persistent app container + Cloudflare tunnel.
    Returns {"url": str, "status": "running"} or {"error": str}.
    """
    client = _docker()

    # Evict expired deployments first
    _evict_expired(client)

    # Enforce max concurrent limit: kill oldest if at cap
    if session_id not in _deployments and len(_deployments) >= _MAX_DEPLOYMENTS:
        oldest_sid = min(_deployments, key=lambda s: _deployments[s].get("deployed_at", 0))
        log.warning("deployment.evicting_oldest_for_cap", evicted=oldest_sid[:12], cap=_MAX_DEPLOYMENTS)
        _kill_existing(client, oldest_sid)
        _deployments.pop(oldest_sid, None)

    _pull_cf_image(client)
    _kill_existing(client, session_id)

    # Mount workspace_dir directly — DockerSandbox.write_file writes here.
    # Do NOT mount a workspace/ subdirectory: if one exists it will be empty
    # because write_file never uses it, causing the container to fail on startup.
    actual_workspace = workspace_dir

    app_name, cf_name, _ = _names(session_id)
    safe_command = _make_safe_command(command)

    # Start app container
    try:
        _deploy_ts = str(time.time())
        app_container = client.containers.run(
            sandbox_image,
            command=["bash", "-c", safe_command],
            name=app_name,
            detach=True,
            volumes={str(actual_workspace): {"bind": "/workspace", "mode": "rw"}},
            working_dir="/workspace",
            mem_limit="512m",
            restart_policy={"Name": "unless-stopped"},
            labels={
                "madeploy.deployed_at": _deploy_ts,
                "madeploy.session_id": session_id[:12],
                "madeploy.ttl_seconds": str(_DEPLOYMENT_TTL_SECONDS),
            },
        )
        log.info("deployment.app_started", name=app_name, command=safe_command, port=port)
    except Exception as exc:
        return {"error": f"Failed to start app container: {exc}"}

    # Brief wait to catch instant-exit failures (bad command, missing file, etc.)
    # 5s is enough — if the container exits this fast it's a real error, not slow startup.
    deadline = time.time() + 5
    while time.time() < deadline:
        app_container.reload()
        if app_container.status in ("exited", "dead"):
            logs = app_container.logs(tail=15).decode("utf-8", errors="replace")
            app_container.remove(force=True)
            return {"error": f"App container exited immediately. Logs:\n{logs}"}
        if app_container.status == "running":
            break
        time.sleep(0.3)

    # CF container shares the app container's network namespace — needs container ID
    for attempt in range(3):
        try:
            client.containers.run(
                _CF_IMAGE,
                command=["tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
                name=cf_name,
                detach=True,
                network_mode=f"container:{app_container.id}",
                mem_limit="64m",
                restart_policy={"Name": "unless-stopped"},
                labels={
                    "madeploy.deployed_at": _deploy_ts,
                    "madeploy.session_id": session_id[:12],
                    "madeploy.ttl_seconds": str(_DEPLOYMENT_TTL_SECONDS),
                },
            )
            log.info("deployment.cf_started", name=cf_name, attempt=attempt)
            break
        except Exception as exc:
            if attempt == 2:
                app_container.remove(force=True)
                return {"error": f"Failed to start cloudflared after 3 attempts: {exc}"}
            time.sleep(1)

    # Wait for cloudflared to emit its public URL (typically 3-7s)
    url = _get_cf_url(client, cf_name, timeout=40)
    if not url:
        cf_logs = ""
        try:
            cf_c = client.containers.get(cf_name)
            cf_logs = cf_c.logs(tail=20).decode("utf-8", errors="replace")
        except Exception:
            pass
        return {"error": f"Cloudflare tunnel started but URL not captured within 40s.\nCF logs:\n{cf_logs}"}

    _deployments[session_id] = {
        "app_container": app_name,
        "cf_container": cf_name,
        "network": None,
        "url": url,
        "command": command,
        "port": port,
        "deployed_at": time.time(),
    }

    log.info("deployment.ready", session_id=session_id[:12], url=url)
    return {"url": url, "status": "running", "command": command}


def stop_deployment(session_id: str) -> dict[str, Any]:
    """Kill containers and clean up network for this session."""
    client = _docker()
    _kill_existing(client, session_id)
    _deployments.pop(session_id, None)
    return {"status": "stopped"}


def get_deployment_status(session_id: str) -> dict[str, Any]:
    """Return current deployment state for this session."""
    state = _deployments.get(session_id)
    if not state:
        return {"status": "not_deployed"}
    if time.time() - state.get("deployed_at", 0) > _DEPLOYMENT_TTL_SECONDS:
        client = _docker()
        _kill_existing(client, session_id)
        _deployments.pop(session_id, None)
        return {"status": "not_deployed", "expired": True}

    client = _docker()
    app_name, cf_name, _ = _names(session_id)

    try:
        app_c = client.containers.get(app_name)
        app_status = app_c.status
    except docker.errors.NotFound:
        app_status = "not_found"

    try:
        cf_c = client.containers.get(cf_name)
        cf_status = cf_c.status
        # Re-capture URL if it was lost (e.g. process restart)
        if not state.get("url"):
            url = _get_cf_url(client, cf_name, timeout=5)
            if url:
                state["url"] = url
    except docker.errors.NotFound:
        cf_status = "not_found"

    return {
        "status": "running" if app_status == "running" and cf_status == "running" else "degraded",
        "url": state.get("url"),
        "command": state.get("command"),
        "app_container": app_status,
        "cf_container": cf_status,
    }


def get_app_logs(session_id: str, tail: int = 50) -> str:
    """Return last N lines of app container logs."""
    state = _deployments.get(session_id)
    if not state:
        return "[no active deployment]"
    client = _docker()
    try:
        c = client.containers.get(state["app_container"])
        return c.logs(tail=tail).decode("utf-8", errors="replace")
    except docker.errors.NotFound:
        return "[app container not found]"

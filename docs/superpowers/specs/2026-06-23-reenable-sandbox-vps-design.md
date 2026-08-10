# Re-enable Sandbox/Subagent/Deploy + VPS Stability

Date: 2026-06-23
Status: approved

## Problem

Sandbox, deploy, tool_creator, and subagents were disabled via a launch kill switch
(`sandbox_subagents_enabled=False`, commit `ea90d16`) because they were unstable on the
production VPS. Root cause analysis identified the primary failure plus several secondary
weaknesses.

### Primary root cause: DinD bind-mount path mismatch

The app runs **inside a container** and spawns sandbox containers as **siblings** via the
mounted host Docker socket. Bind-mount sources in `containers.run(volumes={...})` are
resolved by the **host daemon**, not the app container's filesystem.

`docker-compose.yml` mounts the workspace as a **named volume**
(`sandbox_data:/tmp/agent-sandboxes`). So:

- `write_file`/`read_file`/`edit` (DockerBackend, host-side ops in the app container) write
  into the named volume.
- `execute` / `deploy_app` (sibling container, bind source resolved on host) mount a
  literal host path `/tmp/agent-sandboxes/<sid>` that is a different, empty directory.

→ Two different directories. Files written are invisible to bash → "file not found",
random failures. Works in dev (app runs on host, paths are real) but breaks in the
container deployment. Same bug applies in `deployment_service.py`.

### Secondary causes

- Concurrency check only counts containers and returns an error string when full — no
  queue. Under load it fails instead of waiting.
- Blocking `containers.run()` runs on the default thread-pool executor → starves the event
  loop, slowing unrelated API requests.
- Orphaned containers + workspace dirs accumulate across crashes (no scheduled cleanup).
- Subagent compile failure raises `RuntimeError` that aborts the entire run.
- `default_subagent_max_tokens=2048` truncates subagent output before tool calls finish → stall.

## Approach (chosen: A — fix root causes + re-enable)

### 1. Host-path translation (primary fix)
Add setting `sandbox_host_base_dir` (env `SANDBOX_HOST_BASE_DIR`). A single helper
`to_host_path(internal_path) -> str` maps an app-internal path under `sandbox_base_dir` to
the host path under `sandbox_host_base_dir`. Used when building `volumes={}` in both
`infra/sandbox.py` and `infra/deployment_service.py`. Default: equal to `sandbox_base_dir`
(no-op → dev unchanged). Compose changed to a bind dir at an identical host:container path
(`/opt/agent-sandboxes:/opt/agent-sandboxes`, `SANDBOX_BASE_DIR=/opt/agent-sandboxes`).

### 2. Concurrency semaphore (queue, not fail)
Replace count-and-fail with a bounded `asyncio.Semaphore(max_concurrent_sandboxes)`; the
Nth request waits its turn. Keep the live-container hard-cap as a safety net.

### 3. Dedicated Docker executor
Run blocking Docker ops on a dedicated `ThreadPoolExecutor`, not the default executor, so
the event loop and other API requests are not blocked.

### 4. Orphan reaper
Startup + periodic (APScheduler) cleanup of labeled containers older than a TTL and
workspace dirs idle beyond a TTL.

### 5. Graceful degradation (no fatal crash)
- Docker unavailable while `sandbox=true`: log + run without sandbox tools + note in prompt.
- Subagent compile failure: skip that subagent + log; the run continues.

### 6. Limit tuning
- `default_subagent_max_tokens`: 2048 → 8192.
- `mem_limit` / `nano_cpus` made env-configurable; defaults 1g / 1 core.

### 7. Flip kill switch
`sandbox_subagents_enabled` default `True` (env-overridable). Relax the Arthur
"Launch-Safe Temporary Limits" prompt block.

## Testing
- `to_host_path()`: correct mapping, no-op default, traversal safety.
- Semaphore bounds concurrency (mocked docker).
- Degradation: Docker connect failure → run continues without sandbox; subagent compile
  failure → subagent skipped.
- Reaper: old containers/dirs removed, fresh ones kept.
- Regression: sandbox tools still work in the default (dev) path.

## Components touched
`app/config.py`, `app/core/infra/sandbox.py`, `app/core/infra/deployment_service.py`,
`app/core/engine/subagent_builder.py`, `app/core/engine/prompt_builder.py`,
`app/core/launch_safety.py`, `docker-compose.yml`, `.env.example`, tests.

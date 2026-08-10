from __future__ import annotations

import docker


class _FakeContainer:
    def __init__(self, name: str, labels: dict[str, str] | None = None):
        self.name = name
        self.labels = labels or {}
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainerCollection:
    def __init__(self, containers: list[_FakeContainer]):
        self._containers = {container.name: container for container in containers}

    def get(self, name: str) -> _FakeContainer:
        container = self._containers.get(name)
        if not container or container.removed:
            raise docker.errors.NotFound("missing")
        return container

    def list(self, all: bool = False, filters: dict | None = None) -> list[_FakeContainer]:
        name_filter = (filters or {}).get("name", "")
        return [
            container
            for container in self._containers.values()
            if not container.removed and name_filter in container.name
        ]


class _FakeDockerClient:
    def __init__(self, containers: list[_FakeContainer]):
        self.containers = _FakeContainerCollection(containers)


def test_default_deployment_ttl_is_24_hours() -> None:
    from app.core.infra import deployment_service as svc

    assert svc.deployment_ttl_seconds() == 24 * 3600


def test_cleanup_expired_deployments_removes_labeled_orphans(monkeypatch) -> None:
    from app.core.infra import deployment_service as svc

    now = 1_000_000.0
    expired_at = now - svc.deployment_ttl_seconds() - 1
    fresh_at = now - svc.deployment_ttl_seconds() + 60
    expired_app = _FakeContainer("madeploy-app-expired", {"madeploy.deployed_at": str(expired_at)})
    expired_cf = _FakeContainer("madeploy-cf-expired", {"madeploy.deployed_at": str(expired_at)})
    fresh_app = _FakeContainer("madeploy-app-fresh", {"madeploy.deployed_at": str(fresh_at)})
    fake_client = _FakeDockerClient([expired_app, expired_cf, fresh_app])

    monkeypatch.setattr(svc, "_docker", lambda: fake_client)
    monkeypatch.setattr(svc.time, "time", lambda: now)

    result = svc.cleanup_expired_deployments()

    assert result["status"] == "ok"
    assert result["ttl_seconds"] == 24 * 3600
    assert result["evicted"] == 2
    assert expired_app.removed is True
    assert expired_cf.removed is True
    assert fresh_app.removed is False


def test_get_deployment_status_stops_expired_in_memory_deployment(monkeypatch) -> None:
    from app.core.infra import deployment_service as svc

    now = 1_000_000.0
    session_id = "session-expired"
    app_name, cf_name, _ = svc._names(session_id)
    app_container = _FakeContainer(app_name)
    cf_container = _FakeContainer(cf_name)
    fake_client = _FakeDockerClient([app_container, cf_container])
    svc._deployments[session_id] = {
        "app_container": app_name,
        "cf_container": cf_name,
        "url": "https://demo.trycloudflare.com",
        "command": "python3 -m http.server 8080",
        "port": 8080,
        "deployed_at": now - svc.deployment_ttl_seconds() - 1,
    }

    monkeypatch.setattr(svc, "_docker", lambda: fake_client)
    monkeypatch.setattr(svc.time, "time", lambda: now)

    try:
        status = svc.get_deployment_status(session_id)
    finally:
        svc._deployments.pop(session_id, None)

    assert status == {"status": "not_deployed", "expired": True}
    assert app_container.removed is True
    assert cf_container.removed is True


# ── deploy_app: cloudflared name-conflict recovery ──────────────────────────


class _RunContainer:
    _ids = 0

    def __init__(self, name: str, status: str = "created"):
        self.name = name
        self.status = status
        self.labels = {}
        self.removed = False
        type(self)._ids += 1
        self.id = f"cid{type(self)._ids}"

    def reload(self) -> None:
        # Simulate a container that comes up healthy.
        if self.status != "running":
            self.status = "running"

    def logs(self, tail: int = 15) -> bytes:
        return b""

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _RunCollection:
    def __init__(self, existing: list[_RunContainer], cf_image: str, cf_fail_times: int = 0):
        self._by_name = {c.name: c for c in existing}
        self._cf_image = cf_image
        self._cf_fail_remaining = cf_fail_times
        self.run_calls: list[str] = []

    def _present(self, name: str) -> _RunContainer | None:
        c = self._by_name.get(name)
        return c if (c and not c.removed) else None

    def get(self, name: str) -> _RunContainer:
        c = self._present(name)
        if not c:
            raise docker.errors.NotFound("missing")
        return c

    def run(self, image: str, name: str | None = None, **kwargs) -> _RunContainer:
        self.run_calls.append(name or "")
        if image == self._cf_image and self._cf_fail_remaining > 0:
            self._cf_fail_remaining -= 1
            raise docker.errors.APIError("boom transient cf failure")
        if name and self._present(name):
            raise docker.errors.APIError(
                f'409 Conflict: container name "/{name}" is already in use'
            )
        c = _RunContainer(name or "anon")
        self._by_name[name or c.id] = c
        return c


class _RunClient:
    def __init__(self, containers: _RunCollection):
        self.containers = containers


def _patch_deploy_env(monkeypatch, svc, client):
    monkeypatch.setattr(svc, "_docker", lambda: client)
    monkeypatch.setattr(svc, "_pull_cf_image", lambda c: None)
    monkeypatch.setattr(svc, "_evict_expired", lambda c: 0)
    monkeypatch.setattr(svc, "to_host_path", lambda p: p)
    monkeypatch.setattr(svc, "_get_cf_url", lambda c, name, timeout=40: "https://x.trycloudflare.com")
    monkeypatch.setattr(svc.time, "sleep", lambda s: None)


def test_deploy_recovers_from_stale_cf_name_conflict(monkeypatch, tmp_path) -> None:
    """A leftover CF container holding the deterministic name must not block deploy."""
    from app.core.infra import deployment_service as svc

    session_id = "0659c995-07bf-4cea-84fe-3eda16281f61"
    _, cf_name, _ = svc._names(session_id)
    stale_cf = _RunContainer(cf_name, status="running")
    collection = _RunCollection([stale_cf], cf_image=svc._CF_IMAGE)
    _patch_deploy_env(monkeypatch, svc, _RunClient(collection))

    try:
        result = svc.deploy_app(session_id, tmp_path, "python3 backend.py", 8080)
    finally:
        svc._deployments.pop(session_id, None)

    assert result["status"] == "running"
    assert result["url"] == "https://x.trycloudflare.com"
    assert stale_cf.removed is True  # stale container cleared
    assert collection.get(cf_name) is not stale_cf  # a fresh CF container holds the name


def test_deploy_retries_cf_on_transient_failure(monkeypatch, tmp_path) -> None:
    """A transient cloudflared start failure is retried, not fatal."""
    from app.core.infra import deployment_service as svc

    session_id = "11112222-3333-4444-5555-666677778888"
    _, cf_name, _ = svc._names(session_id)
    collection = _RunCollection([], cf_image=svc._CF_IMAGE, cf_fail_times=1)
    _patch_deploy_env(monkeypatch, svc, _RunClient(collection))

    try:
        result = svc.deploy_app(session_id, tmp_path, "python3 backend.py", 8080)
    finally:
        svc._deployments.pop(session_id, None)

    assert result["status"] == "running"
    assert collection.run_calls.count(cf_name) == 2  # failed once, succeeded on retry

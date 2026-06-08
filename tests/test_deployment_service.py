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

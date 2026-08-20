import json
from unittest.mock import AsyncMock

import pytest

from app.main import health_detailed


@pytest.mark.asyncio
async def test_detailed_health_requires_live_external_scheduler(monkeypatch) -> None:
    class FakeDB:
        async def execute(self, statement):
            return None

    class FakeResponse:
        status_code = 200

    class FakeHTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("app.main.settings.embedded_scheduler_enabled", False)
    monkeypatch.setattr(
        "app.core.workers.scheduler_service.get_external_scheduler_health",
        AsyncMock(return_value="ok"),
    )
    monkeypatch.setattr(
        "app.main.httpx.AsyncClient",
        lambda **kwargs: FakeHTTPClient(),
    )

    response = await health_detailed(FakeDB())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["checks"] == {
        "database": "ok",
        "scheduler": "ok",
        "wa_service": "ok",
    }


@pytest.mark.asyncio
async def test_detailed_health_degrades_when_external_scheduler_stops(monkeypatch) -> None:
    class FakeDB:
        async def execute(self, statement):
            return None

    class FakeResponse:
        status_code = 200

    class FakeHTTPClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("app.main.settings.embedded_scheduler_enabled", False)
    monkeypatch.setattr(
        "app.core.workers.scheduler_service.get_external_scheduler_health",
        AsyncMock(return_value="stopped"),
    )
    monkeypatch.setattr(
        "app.main.httpx.AsyncClient",
        lambda **kwargs: FakeHTTPClient(),
    )

    response = await health_detailed(FakeDB())
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["checks"]["scheduler"] == "stopped"

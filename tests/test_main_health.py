import json

import pytest

from app.main import health_detailed


@pytest.mark.asyncio
async def test_detailed_health_accepts_external_scheduler(monkeypatch) -> None:
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
        "app.main.httpx.AsyncClient",
        lambda **kwargs: FakeHTTPClient(),
    )

    response = await health_detailed(FakeDB())
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["checks"] == {
        "database": "ok",
        "scheduler": "external",
        "wa_service": "ok",
    }

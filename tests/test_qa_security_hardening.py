from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.marketplace import SkillInstall, _clawhub_items, _matches_native_query, _skill_file_bytes, install_skill
from app.middleware.security_headers import SecurityHeadersMiddleware


def _request(*, scheme: str = "https") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "headers": [(b"x-forwarded-proto", scheme.encode())],
        "scheme": scheme, "server": ("example.test", 443),
    })


def test_security_headers_protect_browser_responses() -> None:
    middleware = SecurityHeadersMiddleware(SimpleNamespace())
    async def next_response(_: Request) -> Response:
        return Response("ok")

    response = asyncio.run(middleware.dispatch(_request(), next_response))

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_security_headers_allow_only_configured_computer_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.middleware.security_headers.get_settings", lambda: SimpleNamespace(
        computer_runtime_viewer_url="https://viewer.example.test/vnc.html",
    ))
    middleware = SecurityHeadersMiddleware(SimpleNamespace())
    async def next_response(_: Request) -> Response:
        return Response("ok")

    response = asyncio.run(middleware.dispatch(_request(), next_response))
    assert "frame-src 'self' https://viewer.example.test;" in response.headers["content-security-policy"]


def test_native_catalog_item_respects_search_query() -> None:
    item = {"name": "Google Workspace", "description": "Drive and Docs", "publisher": "Clevio", "category": "Productivity"}
    assert _matches_native_query(item, "google")
    assert not _matches_native_query(item, "zzqx")


@pytest.mark.asyncio
async def test_clawhub_skill_search_uses_search_endpoint_and_does_not_cache_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import marketplace

    requests = []
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"slug": "web-search", "displayName": "Web Search", "ownerHandle": "maker"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            requests.append((url, kwargs["params"]["q"]))
            return FakeResponse()

    monkeypatch.setattr(marketplace.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    marketplace._catalog_cache.clear()
    items = await _clawhub_items("skill", "web")
    assert requests == [("https://clawhub.ai/api/v1/search", "web")]
    assert items[0]["id"] == "clawhub:skill:maker:web-search"


@pytest.mark.asyncio
async def test_markdown_only_skill_includes_reference_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import marketplace

    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("skill/SKILL.md", "# Demo\nRead references/steps.md")
        archive.writestr("skill/references/steps.md", "Follow these steps.")

    async def download(*_args, **_kwargs):
        return SimpleNamespace(content=buffer.getvalue(), headers={"content-type": "application/zip"}, raise_for_status=lambda: None)

    monkeypatch.setattr(marketplace, "_clawhub_get", download)
    content = await _skill_file_bytes(SimpleNamespace(), "demo", "1.0.0")
    assert b"Follow these steps." in content


@pytest.mark.asyncio
async def test_skill_with_executable_hook_is_not_imported_as_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import marketplace

    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("skill/SKILL.md", "# Demo")
        archive.writestr("skill/hooks/openclaw/index.js", "module.exports = {}")

    async def download(*_args, **_kwargs):
        return SimpleNamespace(content=buffer.getvalue(), headers={"content-type": "application/zip"}, raise_for_status=lambda: None)

    monkeypatch.setattr(marketplace, "_clawhub_get", download)
    with pytest.raises(HTTPException) as error:
        await _skill_file_bytes(SimpleNamespace(), "demo", "1.0.0")
    assert error.value.status_code == 422
    assert error.value.headers["X-Marketplace-Error-Code"] == "incompatible"


def test_community_install_respects_operator_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.marketplace.get_settings", lambda: SimpleNamespace(marketplace_community_skill_install_enabled=False))
    payload = SkillInstall(
        agent_id="00000000-0000-0000-0000-000000000001",
        owner_handle="publisher",
    )

    with pytest.raises(Exception) as error:
        asyncio.run(install_skill("safe-skill", payload, SimpleNamespace(), SimpleNamespace()))

    assert getattr(error.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_compatible_skill_installs_without_mandatory_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import marketplace

    owner_id, agent_id = uuid.uuid4(), uuid.uuid4()
    markdown = "# Safe test skill\nUse only owner-provided data."
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    agent = SimpleNamespace(id=agent_id, owner_user_id=owner_id, tools_config={}, capabilities=[], version=1)

    class Result:
        def scalar_one_or_none(self):
            return agent

    class Database:
        async def execute(self, _query):
            return Result()

        async def commit(self):
            return None

    async def verified(*_args):
        return markdown, digest, "1.0.0"

    async def save(*_args):
        return SimpleNamespace(id=uuid.uuid4(), name="clawhub-maker-safe-skill")

    monkeypatch.setattr(marketplace, "get_settings", lambda: SimpleNamespace(marketplace_community_skill_install_enabled=True))
    monkeypatch.setattr(marketplace, "_fetch_verified_skill", verified)
    monkeypatch.setattr(marketplace, "create_or_update_skill", save)
    payload = SkillInstall(agent_id=str(agent_id), owner_handle="maker")
    result = await install_skill("safe-skill", payload, Database(), SimpleNamespace(owner_user_id=owner_id))
    assert result["agent_id"] == str(agent_id)
    assert result["status"] == "installed"

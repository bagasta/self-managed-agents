import pytest

from app.core.engine.agent_runner import (
    _build_google_mcp_unavailable_reply,
    _extract_google_mcp_step_error,
    _is_google_auth_or_scope_error,
)
from app.core.engine.google_mcp_support import (
    GoogleMcpRuntime,
    _fetch_google_auth_link,
    _has_google_mcp_step,
    _is_google_mcp_intent,
    _looks_like_google_auth_recovery_reply,
    apply_google_mcp_reply_overrides,
    prepare_google_mcp_runtime,
)


def test_google_scope_error_markers_include_google_api_scope_messages() -> None:
    err = "Request had insufficient authentication scopes. Required scope: https://www.googleapis.com/auth/presentations"
    assert _is_google_auth_or_scope_error(err) is True


def test_google_auth_error_markers_include_preflight_not_connected_message() -> None:
    err = "Google Workspace belum terhubung atau token sudah expired"
    assert _is_google_auth_or_scope_error(err) is True


def test_google_mcp_intent_detects_google_auth_requests() -> None:
    assert _is_google_mcp_intent("sambungkan akun Google saya ke MCP")
    assert _is_google_mcp_intent("tolong login google dulu")


def test_google_auth_recovery_reply_is_not_success_claim() -> None:
    assert _looks_like_google_auth_recovery_reply(
        "Karena saat ini saya belum terhubung dengan akun Gmail kamu melalui MCP, "
        "saya tidak bisa langsung cek email terbaru."
    )


@pytest.mark.asyncio
async def test_fetch_google_auth_link_accepts_short_start_url(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "auth_url": "https://devtunnel.example/v1/integrations/google/start?t=abc"
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    auth_url = await _fetch_google_auth_link(
        integration_url="https://devtunnel.example",
        api_key="test",
        agent_id="00000000-0000-0000-0000-000000000000",
        candidate_user_ids=["628111"],
    )

    assert auth_url == "https://devtunnel.example/v1/integrations/google/start?t=abc"


@pytest.mark.asyncio
async def test_prepare_google_mcp_runtime_uses_agent_owner_fallback_for_auth(monkeypatch) -> None:
    calls = []

    class FakeSettings:
        google_integration_service_url = "https://devtunnel.example"

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return FakeResponse(200, {"connected": False})

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return FakeResponse(
                200,
                {"auth_url": "https://devtunnel.example/v1/integrations/google/start?t=abc"},
            )

    monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    runtime = await prepare_google_mcp_runtime(
        tools_config={
            "mcp": {
                "enabled": True,
                "servers": {
                    "google_workspace": {
                        "url": "http://localhost:8002/mcp",
                        "transport": "streamable_http",
                    }
                },
            }
        },
        tools=[],
        active_groups=[],
        session=type("Session", (), {"channel_config": {}, "external_user_id": None})(),
        agent_id="00000000-0000-0000-0000-000000000000",
        memory_scope=None,
        api_key="test",
        user_message="sambungkan google",
        system_prompt="",
        log=type("Log", (), {"warning": lambda *args, **kwargs: None, "info": lambda *args, **kwargs: None})(),
        fallback_external_user_id="62895619356936",
    )

    assert runtime.auth_url == "https://devtunnel.example/v1/integrations/google/start?t=abc"
    assert runtime.candidate_user_ids[0] == "62895619356936"
    assert any(call[0] == "POST" and call[2]["json"]["external_user_id"] == "62895619356936" for call in calls)


@pytest.mark.asyncio
async def test_google_auth_tool_returns_preflight_auth_url_without_refetch(monkeypatch) -> None:
    class FakeSettings:
        google_integration_service_url = "https://devtunnel.example"

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse(200, {"connected": False})

        async def post(self, *args, **kwargs):
            return FakeResponse(
                200,
                {"auth_url": "https://devtunnel.example/v1/integrations/google/start?t=preflight"},
            )

    monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    tools = []
    runtime = await prepare_google_mcp_runtime(
        tools_config={
            "mcp": {
                "enabled": True,
                "servers": {
                    "google_workspace": {
                        "url": "http://localhost:8002/mcp",
                        "transport": "streamable_http",
                    }
                },
            }
        },
        tools=tools,
        active_groups=[],
        session=type("Session", (), {"channel_config": {}, "external_user_id": None})(),
        agent_id="00000000-0000-0000-0000-000000000000",
        memory_scope=None,
        api_key="test",
        user_message="cek gmail terbaru",
        system_prompt="",
        log=type("Log", (), {"warning": lambda *args, **kwargs: None, "info": lambda *args, **kwargs: None})(),
        fallback_external_user_id="62895619356936",
    )

    auth_tool = next(tool for tool in tools if tool.name == "get_google_workspace_auth_link")

    async def fail_refetch(*args, **kwargs):
        raise AssertionError("auth link should not be refetched when preflight URL exists")

    monkeypatch.setattr("app.core.engine.google_mcp_support._fetch_google_auth_link", fail_refetch)

    assert runtime.auth_url == "https://devtunnel.example/v1/integrations/google/start?t=preflight"
    assert await auth_tool.ainvoke({}) == runtime.auth_url


def test_extract_google_mcp_step_error_detects_scope_failure_from_tool_result() -> None:
    steps = [
        {
            "tool": "create_presentation",
            "result": "Google API error: Request had insufficient authentication scopes. Required scope: https://www.googleapis.com/auth/presentations",
        }
    ]
    assert _extract_google_mcp_step_error(steps) is not None


def test_google_mcp_step_detection_includes_high_level_workspace_tools() -> None:
    assert _has_google_mcp_step([{"tool": "create_slide_deck", "result": "ok"}])
    assert _has_google_mcp_step([{"tool": "create_survey_form", "result": "ok"}])
    assert _has_google_mcp_step([{"tool": "manage_event", "result": "ok"}])
    assert _has_google_mcp_step(
        [
            {
                "tool": "task",
                "result": "URL: https://docs.google.com/presentation/d/presentation-id/edit",
            }
        ]
    )
    assert not _has_google_mcp_step([{"tool": "task", "result": "claimed ok"}])


def test_extract_google_mcp_step_error_detects_forms_scope_failure() -> None:
    steps = [
        {
            "tool": "create_survey_form",
            "result": "Google API error: Request had insufficient authentication scopes. Required scope: https://www.googleapis.com/auth/forms.body",
        }
    ]
    assert _extract_google_mcp_step_error(steps) is not None


def test_unavailable_reply_for_timeout_does_not_claim_progress() -> None:
    reply = _build_google_mcp_unavailable_reply(
        "Server error '504 Gateway Timeout' for url 'https://example.com/mcp'"
    )
    lowered = reply.lower()
    assert "belum berhasil" in lowered
    assert "masih berjalan" not in lowered
    assert "akan kirim" not in lowered


@pytest.mark.asyncio
async def test_google_mcp_success_claim_without_google_tool_is_overridden() -> None:
    runtime = GoogleMcpRuntime(
        enabled=True,
        workspace_server={},
        connected_user_id="user@example.com",
        auth_url=None,
        preflight_error=None,
        integration_url="http://localhost:8002",
        candidate_user_ids=["user@example.com"],
        system_prompt="",
    )

    reply, steps, _ = await apply_google_mcp_reply_overrides(
        final_reply=(
            "Presentasi Google Slides tentang bahaya merokok dekat anak kecil "
            "sudah saya buat menggunakan MCP tool. Link presentasi sudah saya siapkan."
        ),
        steps=[{"tool": "task", "result": "done"}],
        mcp_errors={},
        runtime=runtime,
        auth_url=None,
        llm_raw=None,
        user_message="tolong buatkan slide dengan mcp google slide",
        agent_id="00000000-0000-0000-0000-000000000000",
        api_key="test",
        log=type("Log", (), {"warning": lambda *args, **kwargs: None})(),
    )

    lowered = reply.lower()
    assert "belum berhasil" in lowered or "belum ada" in lowered
    assert "tidak memanggil tool google mcp" in lowered
    assert steps == [{"tool": "task", "result": "done"}]


@pytest.mark.asyncio
async def test_google_auth_recovery_reply_is_preserved_and_gets_link() -> None:
    runtime = GoogleMcpRuntime(
        enabled=True,
        workspace_server={},
        connected_user_id="628111",
        auth_url="https://devtunnel.example/v1/integrations/google/start?t=abc",
        preflight_error=None,
        integration_url="https://devtunnel.example",
        candidate_user_ids=["628111"],
        system_prompt="",
    )
    original = (
        "Karena saat ini saya belum terhubung dengan akun Gmail kamu melalui MCP, "
        "saya tidak bisa langsung cek email terbaru. Kalau kamu mau, saya bisa buatkan "
        "link otentikasi Google terbaru."
    )

    reply, steps, auth_url = await apply_google_mcp_reply_overrides(
        final_reply=original,
        steps=[],
        mcp_errors={},
        runtime=runtime,
        auth_url=runtime.auth_url,
        llm_raw=None,
        user_message="cek gmail terbaru",
        agent_id="00000000-0000-0000-0000-000000000000",
        api_key="test",
        log=type("Log", (), {"warning": lambda *args, **kwargs: None})(),
    )

    assert "Run ini tidak memanggil tool Google MCP" not in reply
    assert original in reply
    assert "https://devtunnel.example/v1/integrations/google/start?t=abc" in reply
    assert steps == []
    assert auth_url == runtime.auth_url


@pytest.mark.asyncio
async def test_google_auth_recovery_reply_with_link_is_preserved() -> None:
    runtime = GoogleMcpRuntime(
        enabled=True,
        workspace_server={},
        connected_user_id="628111",
        auth_url="https://devtunnel.example/v1/integrations/google/start?t=abc",
        preflight_error=None,
        integration_url="https://devtunnel.example",
        candidate_user_ids=["628111"],
        system_prompt="",
    )
    original = (
        "Ini link otentikasi Google-nya: "
        "https://devtunnel.example/v1/integrations/google/start?t=abc"
    )

    reply, _, _ = await apply_google_mcp_reply_overrides(
        final_reply=original,
        steps=[],
        mcp_errors={},
        runtime=runtime,
        auth_url=runtime.auth_url,
        llm_raw=None,
        user_message="iya tolong buatkan",
        agent_id="00000000-0000-0000-0000-000000000000",
        api_key="test",
        log=type("Log", (), {"warning": lambda *args, **kwargs: None})(),
    )

    assert reply == original


@pytest.mark.asyncio
async def test_google_mcp_artifact_inside_task_is_not_overridden() -> None:
    runtime = GoogleMcpRuntime(
        enabled=True,
        workspace_server={},
        connected_user_id="user@example.com",
        auth_url=None,
        preflight_error=None,
        integration_url="http://localhost:8002",
        candidate_user_ids=["user@example.com"],
        system_prompt="",
    )
    final_reply = (
        "Presentasi Google Slides sudah saya buat: "
        "https://docs.google.com/presentation/d/presentation-id/edit"
    )

    reply, _, _ = await apply_google_mcp_reply_overrides(
        final_reply=final_reply,
        steps=[
            {
                "tool": "task",
                "result": (
                    "Created and populated slide deck. URL: "
                    "https://docs.google.com/presentation/d/presentation-id/edit"
                ),
            }
        ],
        mcp_errors={},
        runtime=runtime,
        auth_url=None,
        llm_raw=None,
        user_message="tolong buatkan slide dengan mcp google slide",
        agent_id="00000000-0000-0000-0000-000000000000",
        api_key="test",
        log=type("Log", (), {"warning": lambda *args, **kwargs: None})(),
    )

    assert reply == final_reply

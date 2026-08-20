from arthur.tools.builder_create_tools import _capability_context_for_memory
from app.core.engine.agent_google_routing import (
    _builder_google_auth_agent_id,
    _extract_auth_url_from_builder_steps,
)


def test_capability_context_persists_scheduler_contract_without_secrets() -> None:
    context = _capability_context_for_memory({"scheduler": True})

    assert "Scheduler aktif" in context
    assert "set_reminder" in context
    assert "OAuth" not in context


def test_capability_context_records_google_auth_requirement_without_auth_url() -> None:
    context = _capability_context_for_memory(
        {
            "mcp": {
                "enabled": True,
                "servers": {"google_workspace": {"allowed_services": ["gmail"]}},
            }
        }
    )

    assert "Google Workspace aktif untuk gmail" in context
    assert "OAuth owner" in context
    assert "http" not in context.lower()


def test_google_auth_guard_supports_optimized_create_result() -> None:
    auth_url = "https://auth.example/google/start?t=one-time-token"
    steps = [
        {
            "tool": "create_agent_from_brief",
            "result": {
                "success": True,
                "agent_id": "a78c2fc0-016b-44d7-9584-62d779d5fae4",
                "needs_google_auth": True,
                "google_auth": {"connected": False, "auth_url": auth_url},
            },
        }
    ]

    assert _extract_auth_url_from_builder_steps(steps) == auth_url
    assert _builder_google_auth_agent_id(steps) == "a78c2fc0-016b-44d7-9584-62d779d5fae4"

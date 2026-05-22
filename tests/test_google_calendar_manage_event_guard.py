from types import SimpleNamespace

import pytest
from pydantic import create_model

from app.core.engine.google_mcp_support import sanitize_google_forms_tools


GetEventsArgs = create_model(
    "GetEventsArgs",
    calendar_id=(str, "primary"),
    time_min=(str | None, None),
    time_max=(str | None, None),
    max_results=(int, 25),
    query=(str | None, None),
    detailed=(bool, False),
)

ManageEventArgs = create_model(
    "ManageEventArgs",
    action=(str, ...),
    event_id=(str | None, None),
    summary=(str | None, None),
    calendar_id=(str, "primary"),
    start_time=(str | None, None),
    end_time=(str | None, None),
)


class FakeTool:
    def __init__(self, name, result=None, args_schema=None):
        self.name = name
        self.description = name
        self.args_schema = args_schema
        self.result = result
        self.calls = []

    async def ainvoke(self, kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_manage_event_without_event_id_auto_looks_up_and_retries() -> None:
    get_events = FakeTool(
        "get_events",
        'Successfully retrieved 1 events:\n- "Cancel trial" (Starts: 2026-06-18T07:00:00+07:00, Ends: 2026-06-18T07:30:00+07:00) ID: evt123 | Link: https://example.test',
        args_schema=GetEventsArgs,
    )
    manage_event = FakeTool(
        "manage_event",
        "Successfully modified event 'Cancel trial' (ID: evt123)",
        args_schema=ManageEventArgs,
    )

    wrapped = sanitize_google_forms_tools([get_events, manage_event], SimpleNamespace(warning=lambda *a, **k: None))
    guarded_manage_event = next(tool for tool in wrapped if tool.name == "manage_event")

    result = await guarded_manage_event.ainvoke(
        {
            "action": "update",
            "event_id": None,
            "summary": "Cancel trial",
            "calendar_id": "primary",
            "start_time": "2026-06-18T07:00:00+07:00",
            "end_time": "2026-06-18T07:30:00+07:00",
        }
    )

    assert "Successfully modified event" in result
    assert get_events.calls[0]["query"] == "Cancel trial"
    assert manage_event.calls[0]["event_id"] == "evt123"


@pytest.mark.asyncio
async def test_manage_event_without_event_id_returns_instruction_when_lookup_missing() -> None:
    manage_event = FakeTool("manage_event", "should not be called", args_schema=ManageEventArgs)

    wrapped = sanitize_google_forms_tools([manage_event], SimpleNamespace(warning=lambda *a, **k: None))
    guarded_manage_event = next(tool for tool in wrapped if tool.name == "manage_event")

    result = await guarded_manage_event.ainvoke({"action": "update", "event_id": None})

    assert result.startswith("CALENDAR_EVENT_ID_REQUIRED")
    assert not manage_event.calls

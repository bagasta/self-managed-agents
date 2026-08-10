"""AgentStepLogger step logging: tidy INFO + complete DEBUG, with step numbers,
tool names on both ends, and per-tool durations so a run is easy to follow.
"""
from __future__ import annotations

import asyncio

from app.core.engine.agent_callbacks import AgentStepLogger


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def info(self, event, **kw):
        self.calls.append(("info", event, kw))

    def debug(self, event, **kw):
        self.calls.append(("debug", event, kw))

    def warning(self, event, **kw):
        self.calls.append(("warning", event, kw))

    def _events(self, name):
        return [kw for lvl, ev, kw in self.calls if ev == name]


def test_tool_end_includes_tool_name_step_and_duration():
    log = _FakeLog()
    logger = AgentStepLogger(log)

    _run(logger.on_tool_start({"name": "plan_agent"}, "the input", tool_call_id="t1"))
    _run(logger.on_tool_end("the result", tool_call_id="t1"))

    start = log._events("agent_step.tool_start")[0]
    end = log._events("agent_step.tool_end")[0]
    assert start["tool"] == "plan_agent"
    assert start["step"] == 1
    assert end["tool"] == "plan_agent"   # name present on tool_end, not just id
    assert end["step"] == 1               # same step number as its start
    assert "duration_ms" in end
    assert end["status"] == "ok"


def test_step_counter_increments_across_tools():
    log = _FakeLog()
    logger = AgentStepLogger(log)

    _run(logger.on_tool_start({"name": "plan_agent"}, "i", tool_call_id="t1"))
    _run(logger.on_tool_end("r", tool_call_id="t1"))
    _run(logger.on_tool_start({"name": "create_agent"}, "i", tool_call_id="t2"))
    _run(logger.on_tool_end("r", tool_call_id="t2"))

    steps = [e["step"] for e in log._events("agent_step.tool_start")]
    assert steps == [1, 2]


def test_debug_full_payload_emitted_for_complete_mode():
    log = _FakeLog()
    logger = AgentStepLogger(log)

    big_input = "x" * 5000
    _run(logger.on_tool_start({"name": "compose_agent_instructions"}, big_input, tool_call_id="t1"))
    _run(logger.on_tool_end("y" * 5000, tool_call_id="t1"))

    # INFO stays concise; DEBUG carries the full payload for deep debugging.
    info_start = log._events("agent_step.tool_start")[0]
    assert len(str(info_start["input"])) <= 400
    full = log._events("agent_step.tool_start.full")
    assert full and len(str(full[0]["input"])) > 400

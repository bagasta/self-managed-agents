import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.core.engine.computer_execution import (
    ComputerProgressReviewMiddleware,
    computer_graph_recursion_limit,
    finalize_computer_run,
)
from app.core.tools.computer_tool import build_computer_tools


def test_click_coordinates_follow_resized_screenshot():
    runtime = SimpleNamespace(
        capture_screen=Mock(return_value={
            "ok": True, "mime_type": "image/jpeg", "image_base64": "c2NyZWVu",
            "desktop_width": 1920, "desktop_height": 1080,
            "image_width": 1280, "image_height": 720,
        }),
        click=Mock(return_value={"ok": True, "status": "action_sent"}),
    )
    tools = {t.name: t for t in build_computer_tools(runtime, owner_id="owner", visual_observation=True)}
    tools["computer_screenshot"].invoke({})
    tools["computer_click"].invoke({"x": 640, "y": 360})
    runtime.click.assert_called_once_with(owner_id="owner", x=960, y=540)
    invalid = tools["computer_click"].invoke({"x": 1280, "y": 360})
    assert invalid["code"] == "invalid_coordinates"
    assert runtime.click.call_count == 1


def test_repeated_screens_expose_stalled_progress():
    runtime = SimpleNamespace(capture_screen=Mock(return_value={
        "ok": True, "mime_type": "image/jpeg", "image_base64": "c2NyZWVu",
    }))
    tools = {t.name: t for t in build_computer_tools(runtime, owner_id="owner", visual_observation=True)}
    for _ in range(4):
        screen = tools["computer_screenshot"].invoke({})
    assert json.loads(screen[0]["text"])["verification"]["unchanged_screen_streak"] == 3


@pytest.mark.asyncio
async def test_stalled_screen_triggers_bounded_decision_review():
    router = SimpleNamespace(decide=AsyncMock(return_value={
        "status": "uncertain", "visual_progress": {"choice": "replan", "accepted": True},
    }))
    middleware = ComputerProgressReviewMiddleware(router)
    message = ToolMessage(name="computer_screenshot", tool_call_id="screen1", content=[
        {"type": "text", "text": json.dumps({"ok": True, "verification": {"unchanged_screen_streak": 3}})},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ==" }},
    ])
    result = await middleware.abefore_model({"messages": [message]}, None)
    assert "replan" in result["messages"][-1].content
    assert router.decide.call_args.kwargs["review_screen"] is True
    await middleware.abefore_model({"messages": [message]}, None)
    assert router.decide.call_count == 1


@pytest.mark.asyncio
async def test_bounded_final_report_receives_real_evidence_and_no_tools():
    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="Blok 1 telah diperiksa; blok lain belum.")))
    message = ToolMessage(name="computer_click", tool_call_id="c1", content='{"ok": true, "observation": "Blok 1"}')
    answer = await finalize_computer_run(
        llm=llm, messages=[message], request="Periksa semua blok", reason="limit", callbacks=[],
    )
    assert "blok lain belum" in answer.content
    prompt = llm.ainvoke.call_args.args[0]
    assert "Blok 1" in prompt[-1].content
    assert "No tools are available" in prompt[0].content


@pytest.mark.asyncio
async def test_final_report_provider_failure_still_produces_visible_reply():
    answer = await finalize_computer_run(
        llm=SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("provider"))),
        messages=[], request="Periksa semua blok", reason="limit", callbacks=[],
    )
    assert "belum selesai" in answer.content


@pytest.mark.asyncio
async def test_deep_agent_model_limit_precedes_graph_recursion_limit():
    from deepagents import create_deep_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver

    from app.core.engine.agent_middleware import ToolErrorRecoveryMiddleware

    class FakeToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def computer_screenshot() -> str:
        """Synthetic screenshot for a backend-only test."""
        return '{"ok": true}'

    budget = 24
    model = FakeToolModel(responses=[
        AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": "computer_screenshot", "args": {}}])
        for i in range(budget + 1)
    ])
    graph = create_deep_agent(model=model, tools=[computer_screenshot], checkpointer=MemorySaver(), middleware=[
        ToolErrorRecoveryMiddleware(), ComputerProgressReviewMiddleware(),
        ModelCallLimitMiddleware(run_limit=budget, exit_behavior="error"),
    ])
    config = {"recursion_limit": computer_graph_recursion_limit(budget), "configurable": {"thread_id": "test"}}
    with pytest.raises(ModelCallLimitExceededError):
        await graph.ainvoke({"messages": [{"role": "user", "content": "Inspect synthetic screen"}]}, config)
    snapshot = await graph.aget_state(config)
    assert len([m for m in snapshot.values["messages"] if isinstance(m, ToolMessage)]) == budget

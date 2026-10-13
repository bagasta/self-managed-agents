from langchain_core.messages import ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from app.core.engine.agent_middleware import VisualObservationCompactionMiddleware


def _screen(call_id: str, image: str) -> ToolMessage:
    return ToolMessage(
        content=[
            {"type": "text", "text": "screen"},
            {"type": "image_url", "image_url": {"url": image}},
        ],
        tool_call_id=call_id,
        name="computer_screenshot",
    )


def test_visual_history_keeps_only_latest_frame():
    older = _screen("one", "data:image/jpeg;base64,b2xk")
    latest = _screen("two", "data:image/jpeg;base64,bmV3")

    result = VisualObservationCompactionMiddleware().before_model(
        {"messages": [older, latest]}, runtime=None,
    )

    assert result is not None
    messages = result["messages"]
    assert messages[0].id == REMOVE_ALL_MESSAGES
    assert isinstance(messages[1].content, str)
    assert "screen" in messages[1].content
    assert messages[2].content == latest.content


def test_compaction_preserves_action_failure_and_progress_evidence():
    older = _screen("one", "data:image/jpeg;base64,b2xk")
    older.content[0]["text"] = '{"ok": true, "verification": {"unchanged_screen_streak": 3}}'
    result = VisualObservationCompactionMiddleware().before_model(
        {"messages": [older, _screen("two", "data:image/jpeg;base64,bmV3")]}, None,
    )
    assert '"unchanged_screen_streak": 3' in result["messages"][1].content
    assert "base64" not in result["messages"][1].content

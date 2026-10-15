from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from app.core.engine.arthur_context import builder_recovery_messages, terminal_generation_truncated
import pytest


def test_reasoning_only_terminal_generation_triggers_recovery():
    terminal = AIMessage(content="", usage_metadata={"input_tokens": 25000, "output_tokens": 4096, "total_tokens": 29096})
    assert terminal_generation_truncated({"messages": [terminal]}, 4096)
    assert terminal_generation_truncated({"messages": [AIMessage(content="", response_metadata={"finish_reason": "length"})]}, 16384)


def test_aggregate_usage_does_not_trigger_false_recovery():
    messages = [AIMessage(content="", usage_metadata={"input_tokens": 10, "output_tokens": 3000, "total_tokens": 3010}) for _ in range(3)]
    assert not terminal_generation_truncated({"messages": messages}, 4096)


def test_operational_recovery_keeps_write_receipt_without_builder_directives():
    human = HumanMessage(content="Simpan jawaban saya")
    write = AIMessage(content="", tool_calls=[{"id": "saved", "name": "modify_sheet_values", "args": {"range_name": "Jawaban!A2:C2"}}])
    receipt = ToolMessage(content="Successfully updated 3 cells", tool_call_id="saved")
    terminal = AIMessage(content="", response_metadata={"finish_reason": "length"})
    recovered = builder_recovery_messages({"messages": [human, write, receipt, terminal]}, [human], is_builder=False)
    assert recovered[:3] == [human, write, receipt]
    assert "Do not repeat successful mutations" in recovered[-1].content
    assert "Verify required writes" in recovered[-1].content
    assert "creation/update tool" not in recovered[-1].content


def test_unknown_write_outcome_blocks_recovery():
    write = AIMessage(content="", tool_calls=[{"id": "unknown", "name": "modify_sheet_values", "args": {}}])
    assert builder_recovery_messages({"messages": [write]}, [], is_builder=False) == []


@pytest.mark.asyncio
async def test_graph_continuation_verifies_completed_write_without_reexecuting_it():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    invoked = []

    @tool
    def modify_sheet_values() -> str:
        """Write the participant row."""
        invoked.append("write")
        return "Successfully updated"

    @tool
    def read_sheet_values() -> str:
        """Verify the saved participant row."""
        invoked.append("read")
        return "Successfully read saved participant answer"

    prior = [HumanMessage(content="Simpan jawaban"),
             AIMessage(content="", tool_calls=[{"id": "saved", "name": "modify_sheet_values", "args": {}}]),
             ToolMessage(content="Successfully updated", tool_call_id="saved"),
             AIMessage(content="", response_metadata={"finish_reason": "length"})]
    model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"id": "verify", "name": "read_sheet_values", "args": {}}]), AIMessage(content="Jawaban sudah tersimpan.")])
    graph = create_react_agent(model, tools=[modify_sheet_values, read_sheet_values])
    output = await graph.ainvoke({"messages": builder_recovery_messages({"messages": prior}, [], is_builder=False)})
    assert invoked == ["read"]
    assert output["messages"][-1].content == "Jawaban sudah tersimpan."

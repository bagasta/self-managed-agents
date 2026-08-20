import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.engine.result_parser import parse_agent_result


class _Log:
    def debug(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass


def test_tool_call_message_is_not_returned_as_final_reply() -> None:
    user = HumanMessage(content="Buatkan PDF dari spreadsheet")
    call_id = "call_pdf"
    intermediate = AIMessage(
        content="Mantap, data dapet! Sekarang gua generate PDF di sandbox.",
        tool_calls=[{"name": "execute", "args": {"command": "python"}, "id": call_id}],
    )
    result = {
        "messages": [
            user,
            intermediate,
            ToolMessage(content="ok", name="execute", tool_call_id=call_id),
        ]
    }

    parsed = parse_agent_result(
        result,
        input_messages=[user],
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step_start=1,
        log=_Log(),
    )

    assert parsed["final_reply"] == ""
    assert parsed["steps"][0]["tool"] == "execute"
    assert parsed["has_output"] is True


def test_terminal_ai_message_is_returned_as_final_reply() -> None:
    user = HumanMessage(content="Statusnya?")
    result = {"messages": [user, AIMessage(content="PDF sudah tersedia: https://example.test/report.pdf")]}

    parsed = parse_agent_result(
        result,
        input_messages=[user],
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step_start=1,
        log=_Log(),
    )

    assert parsed["final_reply"] == "PDF sudah tersedia: https://example.test/report.pdf"

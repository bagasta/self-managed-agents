"""
Regression tests for tool-call orchestration safety.

Verifies that:
1. ensure_tool_messages_complete patches dangling tool calls
2. sanitize_input_messages strips orphaned tool_calls from history
3. The fix is idempotent (already-complete histories pass through unchanged)
"""
from __future__ import annotations

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.engine.result_parser import (
    ensure_tool_messages_complete,
    parse_agent_result,
    sanitize_input_messages,
)


# ──────────────────────────────────────────────────────────────────────
# ensure_tool_messages_complete
# ──────────────────────────────────────────────────────────────────────

class TestEnsureToolMessagesComplete:

    def test_no_dangling_calls(self):
        """Complete history should pass through unchanged."""
        msgs = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"name": "ls", "args": {}, "id": "tc_1"}],
            ),
            ToolMessage(content="file.txt", tool_call_id="tc_1", name="ls"),
            AIMessage(content="Done!"),
        ]
        result = ensure_tool_messages_complete(msgs)
        assert len(result) == len(msgs)
        # No synthetic ToolMessage injected
        assert all(
            not (isinstance(m, ToolMessage) and "did not produce output" in m.content)
            for m in result
        )

    def test_single_dangling_call_patched(self):
        """A single orphaned tool_call should get a synthetic ToolMessage."""
        msgs = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"name": "execute", "args": {"cmd": "ls"}, "id": "tc_orphan"}],
            ),
            # Missing ToolMessage for tc_orphan
            AIMessage(content="Hmm, something went wrong."),
        ]
        result = ensure_tool_messages_complete(msgs)
        # Should have original 3 + 1 synthetic
        assert len(result) == 4
        patched = [m for m in result if isinstance(m, ToolMessage)]
        assert len(patched) == 1
        assert patched[0].tool_call_id == "tc_orphan"
        assert patched[0].status == "error"
        assert "did not produce output" in patched[0].content

    def test_parallel_dangling_calls_patched(self):
        """Multiple orphaned tool_calls from one AIMessage get individual patches."""
        msgs = [
            HumanMessage(content="list both"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "ls", "args": {"path": "/a"}, "id": "tc_a"},
                    {"name": "ls", "args": {"path": "/b"}, "id": "tc_b"},
                ],
            ),
            # Only tc_a has a response
            ToolMessage(content="/a/file1", tool_call_id="tc_a", name="ls"),
            AIMessage(content="Partial results."),
        ]
        result = ensure_tool_messages_complete(msgs)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 2
        ids = {m.tool_call_id for m in tool_msgs}
        assert ids == {"tc_a", "tc_b"}

    def test_idempotent(self):
        """Running twice should produce the same result."""
        msgs = [
            HumanMessage(content="test"),
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": "tc_x"}],
            ),
        ]
        first = ensure_tool_messages_complete(msgs)
        second = ensure_tool_messages_complete(first)
        assert len(first) == len(second)


# ──────────────────────────────────────────────────────────────────────
# sanitize_input_messages
# ──────────────────────────────────────────────────────────────────────

class TestSanitizeInputMessages:

    def test_clean_history_unchanged(self):
        """Complete history passes through without modification."""
        msgs = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"name": "ls", "args": {}, "id": "tc_1"}],
            ),
            ToolMessage(content="file.txt", tool_call_id="tc_1", name="ls"),
            AIMessage(content="Done!"),
        ]
        result = sanitize_input_messages(msgs)
        assert len(result) == len(msgs)

    def test_strips_dangling_tool_calls_keeps_content(self):
        """AIMessage with content + orphaned tool_calls: keep content, strip calls."""
        msgs = [
            HumanMessage(content="do stuff"),
            AIMessage(
                content="Let me try two things",
                tool_calls=[
                    {"name": "ls", "args": {}, "id": "tc_good"},
                    {"name": "execute", "args": {}, "id": "tc_orphan"},
                ],
            ),
            ToolMessage(content="result", tool_call_id="tc_good", name="ls"),
        ]
        result = sanitize_input_messages(msgs)
        ai_msgs = [m for m in result if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 1
        # tc_orphan should be stripped, tc_good kept
        assert len(ai_msgs[0].tool_calls) == 1
        assert ai_msgs[0].tool_calls[0]["id"] == "tc_good"
        assert ai_msgs[0].content == "Let me try two things"

    def test_drops_empty_ai_message(self):
        """AIMessage with ONLY orphaned tool_calls and no content: drop entirely."""
        msgs = [
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"name": "execute", "args": {}, "id": "tc_orphan"}],
            ),
            # No matching ToolMessage
            HumanMessage(content="try again"),
        ]
        result = sanitize_input_messages(msgs)
        ai_msgs = [m for m in result if isinstance(m, AIMessage)]
        assert len(ai_msgs) == 0
        # Should keep both HumanMessages
        human_msgs = [m for m in result if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 2

    def test_preserves_ai_with_content_only(self):
        """AIMessage with content but no tool_calls: untouched."""
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(content="Hello! How can I help?"),
        ]
        result = sanitize_input_messages(msgs)
        assert len(result) == 2
        assert result[1].content == "Hello! How can I help?"

    def test_no_tool_calls_at_all(self):
        """History with zero tool interactions passes through unchanged."""
        msgs = [
            HumanMessage(content="what time is it"),
            AIMessage(content="I don't have access to real-time data."),
            HumanMessage(content="ok thanks"),
        ]
        result = sanitize_input_messages(msgs)
        assert result == msgs

    def test_multiple_turns_partial_dangling(self):
        """Older answered turns preserved; only dangling turn is cleaned."""
        ai_ok = AIMessage(
            content="",
            tool_calls=[{"name": "recall", "args": {}, "id": "tc_ok"}],
        )
        tm_ok = ToolMessage(content="memory result", tool_call_id="tc_ok", name="recall")
        ai_final = AIMessage(content="Remembered.")
        ai_dangling = AIMessage(
            content="",
            tool_calls=[{"name": "remember", "args": {}, "id": "tc_dangling"}],
        )
        msgs = [
            HumanMessage(content="q1"), ai_ok, tm_ok, ai_final,
            HumanMessage(content="q2"), ai_dangling,
        ]
        result = sanitize_input_messages(msgs)
        # Answered AIMessage survives with its tool_calls
        answered = [m for m in result if isinstance(m, AIMessage) and m.tool_calls]
        assert len(answered) == 1
        assert answered[0].tool_calls[0]["id"] == "tc_ok"
        # Dangling AIMessage is dropped (no content, no answered calls)
        for m in result:
            if isinstance(m, AIMessage):
                for tc in (m.tool_calls or []):
                    assert tc["id"] != "tc_dangling"


# ──────────────────────────────────────────────────────────────────────
# Regression: pre-invoke sanitization prevents first-call failure
# ──────────────────────────────────────────────────────────────────────

class TestPreInvokeSanitizationRegression:
    """
    Regression test for the bug where prior_messages was passed directly to
    graph.ainvoke without sanitization. If history contained an AIMessage with
    dangling tool_calls, the provider would reject the first call with:
    "No tool output found for function call ..."

    Fix: sanitize_input_messages(prior_messages) is now applied before
    building input_messages in agent_runner.run_agent().
    """

    def test_dangling_in_prior_is_stripped_before_first_invoke(self):
        """
        Simulates agent_runner.py normal path: sanitize prior_messages FIRST,
        then append current user message. Resulting input_messages must have
        no AIMessage with dangling tool_calls.
        """
        dangling_ai = AIMessage(
            content="I was checking",
            tool_calls=[{"id": "tc_old", "name": "recall", "args": {}}],
        )
        prior = [
            HumanMessage(content="old msg"),
            dangling_ai,  # no ToolMessage follows in history
        ]
        current_user = HumanMessage(content="new question")

        # This mirrors what agent_runner.py now does
        sanitized_prior = sanitize_input_messages(prior)
        input_messages = sanitized_prior + [current_user]

        for msg in input_messages:
            if isinstance(msg, AIMessage):
                assert not msg.tool_calls, (
                    "AIMessage with dangling tool_calls must not reach the provider. "
                    "Prior message sanitization should have removed them."
                )

    def test_clean_prior_plus_new_user_message_is_valid(self):
        """Normal case: clean prior history + new user message = valid input."""
        prior = [
            HumanMessage(content="old question"),
            AIMessage(content="old answer"),
        ]
        sanitized_prior = sanitize_input_messages(prior)
        input_messages = sanitized_prior + [HumanMessage(content="new question")]

        # All AIMessages must have no dangling tool_calls
        for msg in input_messages:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                # If there are tool_calls, they must all have matching ToolMessages
                answered_ids = {
                    m.tool_call_id for m in input_messages
                    if isinstance(m, ToolMessage) and hasattr(m, "tool_call_id")
                }
                for tc in msg.tool_calls:
                    assert tc.get("id") in answered_ids


class TestParseAgentResult:
    def test_out_of_order_tool_messages_match_by_tool_call_id(self):
        class Log:
            def warning(self, *args, **kwargs):
                pass

            def debug(self, *args, **kwargs):
                pass

        input_messages = [HumanMessage(content="run both")]
        result = {
            "messages": [
                *input_messages,
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "first_tool", "args": {}, "id": "tc_first"},
                        {"name": "second_tool", "args": {}, "id": "tc_second"},
                    ],
                ),
                ToolMessage(content="second result", tool_call_id="tc_second", name="second_tool"),
                ToolMessage(content="first result", tool_call_id="tc_first", name="first_tool"),
                AIMessage(content="done"),
            ]
        }

        parsed = parse_agent_result(
            result=result,
            input_messages=input_messages,
            session_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            step_start=1,
            log=Log(),
        )

        by_tool = {step["tool"]: step["result"] for step in parsed["steps"]}
        assert by_tool == {
            "first_tool": "first result",
            "second_tool": "second result",
        }

    def test_long_structured_tool_result_remains_complete_and_parseable(self):
        class Log:
            def warning(self, *args, **kwargs):
                pass

            def debug(self, *args, **kwargs):
                pass

        input_messages = [HumanMessage(content="plan")]
        payload = {
            "plan_status": "needs_clarification",
            "next_questions": [{"topic": "capabilities", "question": "Q" * 5000}],
        }
        output = json.dumps(payload)
        result = {
            "messages": [
                *input_messages,
                AIMessage(
                    content="",
                    tool_calls=[{"name": "plan_agent", "args": {}, "id": "tc_plan"}],
                ),
                ToolMessage(content=output, tool_call_id="tc_plan", name="plan_agent"),
            ]
        }

        parsed = parse_agent_result(
            result=result,
            input_messages=input_messages,
            session_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            step_start=1,
            log=Log(),
        )

        stored = parsed["steps"][0]["result"]
        assert len(stored) > 4000
        assert json.loads(stored) == payload
        tool_record = next(message for message in parsed["db_messages"] if message.role == "tool")
        assert json.loads(tool_record.tool_result) == payload

"""Regression test for Arthur stalling after plan_agent (false entitlement bail).

Incident (2026-06-05): user asked Arthur to build a laundry CS agent. Arthur
called plan_agent (status ready, entitlement allowed) then stopped without
calling create_agent. The runtime's auto-continue (_needs_builder_create_completion)
should have pushed it through to create_agent, but it returned False because
plan_agent's SUCCESS output contains the field "creation_entitlement_check" —
the naive `"entitlement" in result_text` check mistook the routine success
check for an entitlement BLOCK. Result: the user got "Maaf, lagi ada kendala
sistem ... coba lagi" instead of a created agent.
"""
import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.engine.agent_followups import (
    _builder_whatsapp_action_directive,
    _needs_builder_create_completion,
    _needs_builder_whatsapp_action_completion,
    _requested_builder_whatsapp_action,
)


def _plan_step(*, allowed: bool):
    return {
        "tool": "plan_agent",
        "result": json.dumps(
            {
                "plan_status": "ready",
                "agent_name": "Admin Laundry Kiloan",
                "creation_entitlement_check": {
                    "checked": True,
                    "allowed": allowed,
                    "plan_code": "trial",
                    "agents_used": 0,
                    "agents_limit": 1,
                },
            }
        ),
    }


def test_continues_when_plan_succeeded_but_create_not_called():
    steps = [
        {"tool": "remember", "result": "ok"},
        {"tool": "get_user_subscription", "result": "{}"},
        _plan_step(allowed=True),
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is True


def test_does_not_continue_on_real_entitlement_block():
    steps = [
        {"tool": "get_user_subscription", "result": "{}"},
        _plan_step(allowed=False),
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_does_not_continue_when_create_already_called():
    steps = [_plan_step(allowed=True), {"tool": "create_agent", "result": '{"success": true}'}]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_does_not_continue_when_plan_still_needs_discovery():
    steps = [
        {
            "tool": "plan_agent",
            "result": json.dumps(
                {
                    "plan_status": "needs_clarification",
                    "discovery_progress": {"next_group": {"id": "agent_behavior"}},
                }
            ),
        }
    ]
    assert _needs_builder_create_completion(steps, is_builder=True) is False


def test_completion_directive_never_authorizes_invented_details():
    from app.core.engine.agent_followups import _builder_create_completion_directive

    directive = _builder_create_completion_directive().lower()

    assert "dilarang menambah asumsi" in directive
    assert "pakai asumsi wajar" not in directive


def test_retryable_plan_evidence_failure_gets_one_internal_retry():
    from app.core.engine.agent_followups import _needs_builder_retryable_plan

    steps = [
        {
            "tool": "plan_agent",
            "result": json.dumps(
                {
                    "plan_status": "temporarily_unavailable",
                    "retryable": True,
                }
            ),
        }
    ]

    assert _needs_builder_retryable_plan(steps, is_builder=True) is True


def test_ready_or_clarification_plan_is_not_a_retryable_technical_failure():
    from app.core.engine.agent_followups import _needs_builder_retryable_plan

    assert _needs_builder_retryable_plan([_plan_step(allowed=True)], is_builder=True) is False
    clarification = {
        "tool": "plan_agent",
        "result": json.dumps({"plan_status": "needs_clarification", "retryable": False}),
    }
    assert _needs_builder_retryable_plan([clarification], is_builder=True) is False


def test_affirmative_after_trial_offer_requires_trial_link_tool():
    messages = [
        AIMessage(content="Mau aku buatin link trial supaya Minsel bisa langsung dicoba?"),
        HumanMessage(content="iya mau"),
    ]
    action = _requested_builder_whatsapp_action("iya mau", messages)

    assert action == "trial_link"
    assert _needs_builder_whatsapp_action_completion(
        action,
        [{"tool": "get_agent_detail", "result": '{"id":"agent-1"}'}],
        is_builder=True,
    )
    assert not _needs_builder_whatsapp_action_completion(
        action,
        [{"tool": "create_wa_dev_trial_link", "result": '{"success":true}'}],
        is_builder=True,
    )


def test_explicit_dedicated_number_requires_qr_tool():
    action = _requested_builder_whatsapp_action(
        "Saya pilih nomor khusus, kirim QR",
        [],
    )

    assert action == "dedicated_qr"
    assert _needs_builder_whatsapp_action_completion(action, [], is_builder=True)
    directive = _builder_whatsapp_action_directive(action).lower()
    assert "send_agent_wa_qr" in directive
    assert "jangan arahkan user ke dashboard" in directive


def test_informal_demo_request_requires_trial_link_not_qr():
    action = _requested_builder_whatsapp_action(
        "mau test pake nomer demo",
        [],
    )

    assert action == "trial_link"
    assert _needs_builder_whatsapp_action_completion(action, [], is_builder=True)
    directive = _builder_whatsapp_action_directive(action).lower()
    assert "create_wa_dev_trial_link" in directive


def test_code_followup_after_demo_reply_requires_trial_link():
    messages = [
        AIMessage(content="Minsel sudah aktif di nomor demo Arthur."),
        HumanMessage(content="kodenya mana?"),
    ]

    assert (
        _requested_builder_whatsapp_action("kodenya mana?", messages)
        == "trial_link"
    )


def test_generic_whatsapp_setup_question_does_not_choose_for_user():
    assert (
        _requested_builder_whatsapp_action(
            "gimana cara pasang ke whatsappnya?",
            [],
        )
        is None
    )


@pytest.mark.asyncio
async def test_deterministic_demo_fallback_calls_trial_tool_not_qr():
    from app.core.engine.agent_runner import (
        _invoke_builder_whatsapp_action_tool,
    )

    calls = []

    class FakeTool:
        def __init__(self, name):
            self.name = name

        async def ainvoke(self, args):
            calls.append((self.name, args))
            return (
                '{"success":true,"agent_name":"Minsel","code":"ABC123",'
                '"wa_me_url":"https://wa.me/62822?text=ABC123"}'
            )

    parsed = {
        "final_reply": "",
        "steps": [],
        "total_tokens_used": 0,
        "has_output": True,
        "db_messages": [],
    }
    done = await _invoke_builder_whatsapp_action_tool(
        tools=[
            FakeTool("create_wa_dev_trial_link"),
            FakeTool("send_agent_wa_qr"),
        ],
        action="trial_link",
        parsed=parsed,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step_index=1,
        log=type(
            "Log",
            (),
            {
                "info": lambda *_args, **_kwargs: None,
                "warning": lambda *_args, **_kwargs: None,
                "error": lambda *_args, **_kwargs: None,
            },
        )(),
    )

    assert done is True
    assert calls == [("create_wa_dev_trial_link", {"send_contact": True})]
    assert parsed["steps"][0]["tool"] == "create_wa_dev_trial_link"
    assert parsed["db_messages"][0].tool_name == "create_wa_dev_trial_link"

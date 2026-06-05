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

from app.core.engine.agent_followups import _needs_builder_create_completion


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

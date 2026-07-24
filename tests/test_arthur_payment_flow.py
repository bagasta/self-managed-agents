import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.engine.agent_runner import _ensure_builder_payment_link
from app.core.tools.builder_payment_tools import (
    build_builder_payment_tools,
    payment_user_reply,
    verified_payment_payload,
)


class _PaymentTool:
    name = "get_payment_link"

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.result


def _payload(plan_code="tier_3", label="Enterprise", max_agents=None):
    return {
        "plan_code": plan_code,
        "plan_label": label,
        "max_agents": max_agents,
        "phone": "62895626765423",
        "payment_link": (
            "https://chiefaiofficer.id/pay?"
            f"plan={plan_code}&wa=62895626765423&request=abc123"
        ),
    }


def test_verified_payment_payload_rejects_wrong_host_or_plan():
    valid = _payload()
    assert verified_payment_payload(json.dumps(valid), expected_plan="tier_3") == valid

    wrong_host = {**valid, "payment_link": valid["payment_link"].replace(
        "chiefaiofficer.id", "example.com"
    )}
    assert verified_payment_payload(wrong_host, expected_plan="tier_3") is None
    assert verified_payment_payload(valid, expected_plan="tier_2") is None


def test_payment_reply_uses_official_capacity_and_checkout_for_price():
    reply = payment_user_reply(_payload())

    assert "Enterprise" in reply
    assert "agent tanpa batas" in reply
    assert "Harga dan periode" in reply
    assert "https://chiefaiofficer.id/pay?" in reply


@pytest.mark.asyncio
async def test_selected_plan_deterministically_invokes_payment_tool():
    result = json.dumps(_payload(), ensure_ascii=False)
    tool = _PaymentTool(result)
    parsed = {"steps": [], "db_messages": []}
    log = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    reply = await _ensure_builder_payment_link(
        tools=[tool],
        plan_code="tier_3",
        parsed=parsed,
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step_index=1,
        log=log,
    )

    assert tool.calls == [{"plan": "tier_3"}]
    assert parsed["steps"][0]["tool"] == "get_payment_link"
    assert parsed["steps"][0]["tool_call_id"] == "deterministic_builder_payment_link"
    assert "Enterprise" in reply
    assert "https://chiefaiofficer.id/pay?" in reply


@pytest.mark.asyncio
async def test_payment_tool_returns_authoritative_plan_capacities():
    tools = build_builder_payment_tools(
        owner_phone="62895626765423",
        default_target="",
    )
    payment_tool = tools["get_payment_link"]

    starter = json.loads(await payment_tool.ainvoke({"plan": "Starter"}))
    pro = json.loads(await payment_tool.ainvoke({"plan": "Pro"}))
    enterprise = json.loads(await payment_tool.ainvoke({"plan": "Enterprise"}))

    assert starter["max_agents"] == 1
    assert pro["max_agents"] == 2
    assert enterprise["max_agents"] is None
    assert verified_payment_payload(enterprise, expected_plan="tier_3") is not None

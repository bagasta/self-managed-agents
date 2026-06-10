import uuid
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_launch_safety_disables_sandbox_subagents_but_keeps_escalation_and_wa_media():
    from app.core.engine.agent_tool_setup import build_agent_tool_setup

    agent_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_type="whatsapp",
        channel_config={"user_phone": "62811@s.whatsapp.net", "device_id": "dev-1"},
        external_user_id="62811",
    )
    agent = SimpleNamespace(
        id=agent_id,
        capabilities=[],
        tools_config={},
    )
    logs: list[tuple[str, dict]] = []
    log = SimpleNamespace(
        info=lambda event, **kwargs: logs.append((event, kwargs)),
        warning=lambda event, **kwargs: logs.append((event, kwargs)),
    )

    setup = await build_agent_tool_setup(
        agent_model=agent,
        session=session,
        tools_config={
            "memory": False,
            "skills": False,
            "tavily": False,
            "escalation": True,
            "whatsapp_media": True,
            "sandbox": True,
            "deploy": True,
            "tool_creator": True,
            "subagents": {"enabled": True, "agent_ids": ["sys_coder"]},
        },
        raw_tools_config={},
        db=SimpleNamespace(),
        log=log,
        escalation_user_jid=None,
        sender_name="Bagas",
        user_message="halo",
        operating_manual={"maturity": "usable", "owner_review_required": False},
    )

    tool_names = {getattr(tool, "name", "") for tool in setup.tools}

    assert setup.sandbox is None
    assert setup.subagent_list == []
    assert setup.sub_sandboxes == []
    assert "sandbox" not in setup.active_groups
    assert "deploy" not in setup.active_groups
    assert not any(group.startswith("subagents") for group in setup.active_groups)
    assert "escalation" in setup.active_groups
    assert "whatsapp_media" in setup.active_groups
    assert "escalate_to_human" in tool_names
    assert "send_whatsapp_document" in tool_names
    assert any(event == "agent_run.sandbox_subagents_disabled_for_launch" for event, _ in logs)

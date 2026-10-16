"""Account setup links belong in Arthur management replies, never customer replies."""
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.engine.agent_google_routing import guard_customer_auth_links
from app.core.engine.agent_identity import _is_customer_whatsapp_session
from arthur_v2 import plugin
from test_arthur_p1_operations import OWNER, agent_row, database


META_LINK = "https://platform.example/v1/meta/signup/l/test-state"
GOOGLE_LINK = "https://platform.example/v1/integrations/google/start?t=test-state"
INTERNAL_OPERATOR = "6289999999999"


def management_context(builder_config, transport):
    agent = SimpleNamespace(
        id=uuid.uuid4(), owner_external_id=INTERNAL_OPERATOR,
        operator_ids=[INTERNAL_OPERATOR], escalation_config={},
        tools_config={"builder": True} if builder_config == "config" else {},
        capabilities=["builder"] if builder_config == "capability" else [],
    )
    session = SimpleNamespace(
        id=uuid.uuid4(), agent_id=agent.id, channel_type="whatsapp",
        external_user_id=OWNER,
        channel_config={"phone_number": OWNER, "device_id": transport},
    )
    return agent, session


@pytest.mark.parametrize("builder_config", ["config", "capability"])
@pytest.mark.parametrize("transport", ["meta", "wadev_test"])
@pytest.mark.parametrize("link", [META_LINK, GOOGLE_LINK])
def test_builder_client_receives_setup_link_without_becoming_system_operator(builder_config, transport, link):
    agent, session = management_context(builder_config, transport)
    # Tenant owners are not operators of the shared Arthur system agent.
    assert _is_customer_whatsapp_session(session, agent)
    reply = f"Buka link ini untuk menghubungkan assistant kamu:\n{link}"
    assert guard_customer_auth_links(reply, session, agent) == reply
    assert agent.operator_ids == [INTERNAL_OPERATOR]


@pytest.mark.parametrize("transport", ["meta", "wadev_test"])
@pytest.mark.parametrize("link", [META_LINK, GOOGLE_LINK])
def test_operational_customer_cannot_receive_owner_link_or_claim_builder_role(transport, link):
    agent, session = management_context("neither", transport)
    session.channel_config.update(builder=True, capabilities=["builder"], is_operator=True)
    reply = f"Login di {link}"
    guarded = guard_customer_auth_links(reply, session, agent)
    assert link not in guarded
    assert "Pelanggan tidak perlu melakukan login akun" in guarded


@pytest.mark.parametrize("link", [META_LINK, GOOGLE_LINK])
def test_operational_demo_customer_preview_keeps_owner_link_blocked(link):
    agent, session = management_context("neither", "wadev_test")
    agent.owner_external_id = OWNER
    agent.operator_ids = [OWNER]
    agent.tools_config = {"demo_customer_preview": True}
    assert link not in guard_customer_auth_links(f"Login di {link}", session, agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["owned", "foreign", "unconfirmed"])
async def test_meta_signup_tool_and_terminal_guard_preserve_ownership(database, monkeypatch, scenario):
    target = await agent_row(database, owner=INTERNAL_OPERATOR if scenario == "foreign" else OWNER)
    builder, session = management_context("config", "meta")
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(app_public_url="https://platform.example"))
    state_builder = Mock(return_value="test-state")
    monkeypatch.setattr("app.core.infra.meta_embedded_signup.build_signup_state", state_builder)
    tools = {t.name: t for t in plugin.build_arthur_v2_tools(
        db_factory=database, owner_phone=OWNER, self_agent_id=str(builder.id),
    )}
    result = await tools["connect_assistant_whatsapp_cloud"].ainvoke({
        "agent_id": str(target.id), "confirmed": scenario != "unconfirmed",
    })
    if scenario == "owned":
        assert result["ok"] is True
        assert result["signup_url"] == META_LINK
        reply = f"Buka link penyiapan WhatsApp:\n{result['signup_url']}"
        assert guard_customer_auth_links(reply, session, builder) == reply
        state_builder.assert_called_once_with(target.id)
    else:
        assert result["ok"] is False
        assert "signup_url" not in result
        state_builder.assert_not_called()

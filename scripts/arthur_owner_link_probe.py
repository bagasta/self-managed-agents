"""Read-only verification of management link routing on an existing conversation.

Uses placeholder URLs; never creates signup tokens, sends messages or changes data.
"""
import argparse
import asyncio
import json
from types import SimpleNamespace
import uuid

from sqlalchemy import text

from app.core.domain.agent_ownership import agent_belongs_to_owner
from app.core.engine.agent_google_routing import guard_customer_auth_links
from app.core.engine.agent_identity import _is_customer_whatsapp_session
from app.core.engine.agent_policy import build_agent_runtime_policy
from app.core.engine.agent_tool_setup import _resolve_builder_owner_phone
from app.database import AsyncSessionLocal, engine
from app.models.agent import Agent
from app.models.session import Session


async def main(session_id, target_id):
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        session = await db.get(Session, uuid.UUID(session_id))
        assert session is not None
        builder = await db.get(Agent, session.agent_id)
        target = await db.get(Agent, uuid.UUID(target_id))
        assert builder is not None and target is not None and not target.is_deleted
        assert build_agent_runtime_policy(builder, builder.tools_config or {}).is_builder
        assert agent_belongs_to_owner(target, _resolve_builder_owner_phone(session))
        customer = SimpleNamespace(channel_type="whatsapp", external_user_id="6280000000001",
                                   channel_config={"phone_number": "6280000000001", "device_id": "meta"})
        assert not build_agent_runtime_policy(target, target.tools_config or {}).is_builder
        assert _is_customer_whatsapp_session(customer, target)
        for url in ("https://platform.example/v1/meta/signup/l/placeholder",
                    "https://platform.example/v1/integrations/google/start?t=placeholder"):
            reply = f"Buka link ini untuk menghubungkan assistant kamu:\n{url}"
            assert guard_customer_auth_links(reply, session, builder) == reply
            assert url not in guard_customer_auth_links(reply, customer, target)
        print(json.dumps({"read_only": True, "actual_owner_session": True,
                          "assistant_ownership": "passed", "management_meta_link": "passed",
                          "management_google_link": "passed", "customer_links_blocked": "passed"}))
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--target-id", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.session_id, args.target_id))

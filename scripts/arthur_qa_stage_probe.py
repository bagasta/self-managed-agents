"""Integration checks confined to the restored, isolated staging database."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid

from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.database import AsyncSessionLocal, engine
from app.models.agent import Agent
from app.models.message import Message
from app.models.session import Session
from arthur_v2.plugin import build_arthur_v2_tools


async def main():
    settings = get_settings()
    host = make_url(settings.database_url).host or ""
    assert settings.environment == "test" and host.startswith("arthur-qa-pg-"), "Staging only"
    # Staging has no provider credentials or external network. Exercise the
    # reviewed DB/approval path with a deterministic writer, never in production.
    from arthur_v2 import plugin
    async def staging_writer(**kwargs):
        return kwargs["instructions"]
    plugin.compose_assistant_instructions = staging_writer
    async with AsyncSessionLocal() as db:
        target = (await db.execute(select(Agent).where(
            Agent.name == "CS Toko Kue QA", Agent.is_deleted.is_(False),
        ))).scalar_one()
        owner, target_id, original = target.owner_external_id, str(target.id), target.instructions
        creator = target.created_by_agent_id
        if creator is None:
            creator = (await db.execute(select(Agent.id).where(
                Agent.name == "Arthur", Agent.is_deleted.is_(False),
            ).limit(1))).scalar_one()
        session = Session(id=uuid.uuid4(), agent_id=creator, external_user_id=owner,
                          channel_type="whatsapp", channel_config={"phone_number": owner})
        db.add(session)
        await db.flush()
        start = datetime.now(timezone.utc)
        db.add(Message(session_id=session.id, role="user", content="Review perubahan staging", timestamp=start))
        await db.commit()
    settings.arthur_confirmation_owner_ids = [owner]
    tools = {tool.name: tool for tool in build_arthur_v2_tools(
        db_factory=AsyncSessionLocal, owner_phone=owner,
        self_agent_id=str(creator), session_id=str(session.id))}
    assert len(tools) == 35
    knowledge = await tools["list_assistant_knowledge"].ainvoke({"agent_id": target_id})
    assert knowledge["ok"]
    history = await tools["inspect_assistant_history"].ainvoke({"agent_id": target_id})
    assert history["ok"] and history["instructions"] == original
    changed = original + "\nSTAGING_ONLY_APPROVAL_PROBE"
    payload = {"agent_id": target_id, "instructions": changed, "confirmed": True}
    preview = await tools["update_assistant"].ainvoke(payload)
    assert preview.get("needs_confirmation"), "No write before a separate approval turn"
    async with AsyncSessionLocal() as db:
        assert (await db.get(Agent, uuid.UUID(target_id))).instructions == original
        db.add(Message(session_id=session.id, role="user", content="ya", timestamp=start + timedelta(seconds=1)))
        await db.commit()
    applied = await tools["update_assistant"].ainvoke(payload)
    assert applied.get("ok"), "Confirmed write failed"
    repeated = await tools["update_assistant"].ainvoke(payload)
    assert repeated.get("already_applied"), "Duplicate approval must not repeat writes"
    async with AsyncSessionLocal() as db:
        target = await db.get(Agent, uuid.UUID(target_id))
        assert "STAGING_ONLY_APPROVAL_PROBE" in target.instructions
        target.instructions = original
        await db.execute(delete(Session).where(Session.id == session.id))
        await db.commit()
    print(json.dumps({"staging_only": True, "core_tools": 35, "knowledge_read": "passed",
                      "brief_read": "passed", "preview_without_write": "passed",
                      "approval_then_write": "passed", "duplicate_approval": "passed"}))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

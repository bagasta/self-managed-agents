"""Replay the incident without outbound sends; optional reset only in staging."""
import argparse
import asyncio
import json
import uuid

from sqlalchemy import select, text, func

from app.config import get_settings
from app.database import AsyncSessionLocal, engine
from app.models.agent import Agent
from app.models.session import Session
from app.models.message import Message
from app.models.memory import Memory
from app.core.domain.agent_ownership import agent_belongs_to_owner
from app.core.engine.agent_followups import _needs_whatsapp_file_delivery_followup, _extract_shared_workspace_file_from_steps
from app.core.engine.agent_step_utils import _has_whatsapp_media_send_step
from app.core.engine.agent_tool_setup import _resolve_builder_owner_phone
from arthur_v2.plugin import build_arthur_v2_tools
from arthur_v2.memory_revision_tools import scope_condition


async def main(args):
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        owner_session = await db.get(Session, uuid.UUID(args.owner_session))
        customer_session = await db.get(Session, uuid.UUID(args.customer_session))
        assert owner_session and customer_session and owner_session.agent_id != customer_session.agent_id
        target = await db.get(Agent, customer_session.agent_id)
        owner_phone = _resolve_builder_owner_phone(owner_session)
        assert agent_belongs_to_owner(target, owner_phone)
        rows = (await db.execute(select(Message).where(Message.run_id == uuid.UUID(args.incident_run)).order_by(Message.timestamp))).scalars().all()
        assert rows and all(m.session_id == owner_session.id for m in rows)
        steps = [{"tool": m.tool_name, "args": m.tool_args or {}, "result": m.tool_result or ""} for m in rows if m.role == "tool"]
        request = next(m.content for m in rows if m.role == "user")
        reply = next(m.content for m in reversed(rows) if m.role == "agent")
        assert not _has_whatsapp_media_send_step(steps)
        assert _extract_shared_workspace_file_from_steps(steps, reply) is None
        assert _needs_whatsapp_file_delivery_followup(request, target.tools_config or {}, steps, reply) == (False, None)
        inspected = next(s for s in steps if s["tool"] == "inspect_assistant_memory")
        assert inspected["args"]["agent_id"] == str(target.id)
        scope = inspected["args"]["scope"]
        arthur_count = await db.scalar(select(func.count()).select_from(Memory).where(Memory.agent_id == owner_session.agent_id))
        target_count = await db.scalar(select(func.count()).select_from(Memory).where(Memory.agent_id == target.id, scope_condition(scope)))
        before_instructions = target.instructions
    tools = {t.name: t for t in build_arthur_v2_tools(db_factory=AsyncSessionLocal,
             owner_phone=owner_phone, self_agent_id=str(owner_session.agent_id))}
    preview = await tools["reset_assistant_context"].ainvoke({"agent_id": str(target.id), "scope": scope,
               "expected_count": target_count, "confirmed": False})
    assert preview.get("needs_confirmation")
    receipt = {"incident_replay": "passed", "memory_read_is_not_media_delivery": "passed",
               "failed_send_not_success": "passed", "agent_memory_namespaces_separate": True,
               "reset_tool_available": True, "reset_requires_confirmation": True}
    if args.reset_staging:
        settings = get_settings()
        assert settings.environment == "test" and "arthur-qa-pg-" in settings.database_url, "Staging only"
        reset = await tools["reset_assistant_context"].ainvoke({"agent_id": str(target.id), "scope": scope,
                 "expected_count": target_count, "confirmed": True})
        assert reset.get("ok"), "Staging reset failed"
        async with AsyncSessionLocal() as db:
            assert await db.scalar(select(func.count()).select_from(Memory).where(Memory.agent_id == target.id, scope_condition(scope))) == 0
            assert await db.scalar(select(func.count()).select_from(Memory).where(Memory.agent_id == owner_session.agent_id)) == arthur_count
            assert (await db.get(Agent, target.id)).instructions == before_instructions
            clean = await db.get(Session, customer_session.id)
            assert clean.metadata_.get("context_reset_at")
            assert not set(clean.metadata_) & {"current_attachment", "last_incoming_media", "latest_shared_artifact", "shared_artifacts"}
        receipt.update(staging_reset="passed", arthur_memory_unchanged=True, instructions_unchanged=True)
    else:
        receipt["production_data_unchanged"] = True
    print(json.dumps(receipt))
    await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--owner-session", required=True)
    p.add_argument("--customer-session", required=True)
    p.add_argument("--incident-run", required=True)
    p.add_argument("--reset-staging", action="store_true")
    asyncio.run(main(p.parse_args()))

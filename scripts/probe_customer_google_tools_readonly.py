"""Check configured Google tools for a real non-owner session without sending chat.

Reads only a spreadsheet header; never invokes writes, agent runs or delivery.
Prints diagnostics without spreadsheet contents, credentials or user identities.
"""
import argparse
import asyncio
import copy
import json
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.models.session import Session
from app.core.engine.agent_google_routing import _is_google_workspace_owner_or_operator, prepare_agent_google_workspace_runtime
from app.core.engine.google_mcp_support import filter_google_mcp_tools_by_services, sanitize_google_forms_tools
from app.core.tools.mcp_tool import mcp_client_context


async def main(agent_id):
    log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    async with AsyncSessionLocal() as db:
        row = await db.get(Agent, uuid.UUID(agent_id))
        assert row is not None
        agent = SimpleNamespace(owner_external_id=row.owner_external_id, operator_ids=list(row.operator_ids or []), escalation_config=copy.deepcopy(row.escalation_config or {}), tools_config=copy.deepcopy(row.tools_config or {}))
        sessions = (await db.execute(select(Session).where(Session.agent_id == row.id, Session.channel_type == "whatsapp").order_by(Session.updated_at.desc()))).scalars().all()
        session = next(s for s in sessions if not _is_google_workspace_owner_or_operator(s, agent))
        resource = agent.tools_config.get("google_workspace_resources") or {}
        spreadsheet_id = resource.get("default_spreadsheet_id")
        assert spreadsheet_id, "Owner-configured spreadsheet required"
        tools = []
        runtime = await prepare_agent_google_workspace_runtime(
            agent_model=agent, session=session, tools_config=agent.tools_config, tools=tools,
            agent_id=row.id, active_groups=[], memory_scope=session.external_user_id,
            api_key=get_settings().api_key, user_message="Baca header spreadsheet untuk validasi koneksi",
            system_prompt="", log=log,
        )
        if not (runtime.enabled and runtime.connected_user_id and not runtime.preflight_error):
            print(json.dumps({"probe_ok": False, "enabled": runtime.enabled, "connected": bool(runtime.connected_user_id), "preflight_error": str(runtime.preflight_error or "")[:300]}))
        assert runtime.enabled and runtime.connected_user_id and not runtime.preflight_error
        async with mcp_client_context(agent.tools_config) as (remote_tools, errors):
            assert not errors
            selected = sanitize_google_forms_tools(filter_google_mcp_tools_by_services(remote_tools, tools_config=agent.tools_config, requirement_text="Google Sheets baca tulis", log=log), log)
            names = [t.name for t in selected]
            read = next(t for t in selected if t.name == "read_sheet_values")
            output = await read.ainvoke({"spreadsheet_id": spreadsheet_id, "range_name": "Peserta!A1:C1"})
            success = "successfully read" in str(output).lower()
            print(json.dumps({"probe_ok": success, "non_owner_session": True, "agent_kind": (agent.tools_config.get("assistant_profile") or {}).get("kind"), "read_tool_available": "read_sheet_values" in names, "write_tool_available": "modify_sheet_values" in names, "tools": names, "writes_executed": 0, "messages_sent": 0}))
            assert success, "Header read failed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    asyncio.run(main(parser.parse_args().agent_id))

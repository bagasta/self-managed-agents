"""Replay a failed sheet workflow with live reads and simulated writes only.

Only spreadsheet read/info tools are invoked against production.
No database commit, notification, delivery, or real spreadsheet write occurs.
"""
import asyncio
import copy
import json
import uuid
from types import SimpleNamespace

from sqlalchemy import select
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

from app.database import AsyncSessionLocal
from app.config import get_settings
from app.models.agent import Agent
from app.models.session import Session
from app.models.message import Message
from app.models.run import Run
from app.core.engine.agent_llm import build_agent_llms
from app.core.engine.agent_google_routing import prepare_agent_google_workspace_runtime
from app.core.engine.arthur_context import builder_recovery_messages
from app.core.tools.mcp_tool import mcp_client_context


async def main():
    async with AsyncSessionLocal() as db:
        run = await db.get(Run, uuid.UUID("3a2939f1-53f1-4254-a84d-66674bc11836"))
        session = await db.get(Session, run.session_id)
        agent = await db.get(Agent, session.agent_id)
        agent = SimpleNamespace(**{column.name: copy.deepcopy(getattr(agent, column.name)) for column in agent.__table__.columns})
        rows = (await db.execute(select(Message).where(Message.session_id == session.id, Message.timestamp <= run.started_at, Message.role.in_(["user", "agent"])).order_by(Message.timestamp))).scalars().all()
        messages = [HumanMessage(content=m.content) if m.role == "user" else AIMessage(content=m.content) for m in rows if m.content][-50:]
        reads = (await db.execute(select(Message).where(Message.run_id == run.id, Message.role == "tool").order_by(Message.step_index))).scalars().all()
        snapshots = {}
        for i, m in enumerate(reads):
            assert m.tool_name in {"get_spreadsheet_info", "read_sheet_values"}
            snapshots[(m.tool_name, (m.tool_args or {}).get("range_name", ""))] = m.tool_result
            messages.extend([AIMessage(content="", tool_calls=[{"id": f"read-{i}", "name": m.tool_name, "args": m.tool_args}]), ToolMessage(content=m.tool_result, tool_call_id=f"read-{i}")])
        messages.append(AIMessage(content="", response_metadata={"finish_reason": "length"}))
        messages = builder_recovery_messages({"messages": messages}, [], is_builder=False)
        writes = []
        verified = []
        calls = []
        quiet = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None)
        runtime = await prepare_agent_google_workspace_runtime(
            agent_model=agent, session=session, tools_config=agent.tools_config, tools=[],
            agent_id=agent.id, active_groups=[], memory_scope=session.external_user_id,
            api_key=get_settings().api_key, user_message="", system_prompt="", log=quiet,
        )
        assert runtime.enabled and runtime.connected_user_id and not runtime.preflight_error
        # Refresh truncated audit snapshots with full read-only tool results.
        async with mcp_client_context(agent.tools_config) as (remote_tools, errors):
            assert not errors
            schema_tools = [t for t in remote_tools if t.name in {"get_spreadsheet_info", "read_sheet_values", "modify_sheet_values"}]
            read_tools = {t.name: t for t in schema_tools if t.name in {"get_spreadsheet_info", "read_sheet_values"}}
            for i, m in enumerate(reads):
                value = str(await read_tools[m.tool_name].ainvoke(m.tool_args))
                snapshots[(m.tool_name, (m.tool_args or {}).get("range_name", ""))] = value
                for message in messages:
                    if isinstance(message, ToolMessage) and message.tool_call_id == f"read-{i}":
                        message.content = value
        assert len(schema_tools) == 3

        def simulate(tool):
            async def invoke(**kwargs):
                calls.append(tool.name)
                target = kwargs.get("range_name", "")
                if tool.name == "modify_sheet_values":
                    writes.append(kwargs)
                    return "Successfully updated range " + target
                if tool.name == "read_sheet_values":
                    matching = [w for w in writes if w.get("range_name") == target]
                    if matching:
                        verified.append(target)
                        return "Successfully read range " + target + ": " + json.dumps(matching[-1].get("values"), ensure_ascii=False)
                exact = snapshots.get((tool.name, target))
                if exact:
                    return exact
                same_tab = [value for (name, rng), value in snapshots.items() if name == tool.name and rng.split("!")[0] == target.split("!")[0]]
                return same_tab[0] if same_tab else "Unavailable in captured read-only replay; use the captured header and rows."
            return StructuredTool.from_function(coroutine=invoke, name=tool.name, description=tool.description, args_schema=tool.args_schema)

        async def notify_owner(message: str):
            """Send a summary to the owner."""
            calls.append("notify_owner_simulated")
            return {"ok": True, "simulated": True}

        raw, llm = build_agent_llms(agent, get_settings(), 0.2)
        graph = create_react_agent(llm, tools=[simulate(t) for t in schema_tools] + [StructuredTool.from_function(coroutine=notify_owner)], prompt=agent.instructions)
        result = await graph.ainvoke({"messages": messages}, config={"recursion_limit": 30})
        final = result["messages"][-1]
        print(json.dumps({"max_tokens": raw.max_tokens, "calls": calls, "simulated_write_tabs": [w.get("range_name", "").split("!")[0] for w in writes], "verified_write_count": len(verified), "final_chars": len(str(final.content)), "real_writes": 0, "real_notifications": 0, "terminal_finish_reason": final.response_metadata.get("finish_reason")}))
        assert writes and verified and final.content and not final.tool_calls
        assert any("Jawaban" in w.get("range_name", "") for w in writes)
        assert any("Laporan" in w.get("range_name", "") for w in writes)


if __name__ == "__main__":
    asyncio.run(main())

"""Replay a real owner's context against the LLM with every mutation simulated.

No run_agent call, database commits, OAuth generation, or WhatsApp delivery.
Only live inventory/plan reads and model inference are allowed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.models.session import Session
from app.core.domain.memory_service import list_memories
from app.core.engine.agent_llm import build_agent_llms
from app.core.engine.agent_tool_setup import _resolve_builder_owner_phone
from app.core.engine.arthur_context import build_arthur_recall, bound_arthur_history, starts_new_project
from app.core.engine.context_service import load_history, db_messages_to_lc
from app.core.engine.prompt_builder import build_system_prompt
from arthur_v2.plugin import build_arthur_v2_tools, build_arthur_v2_system_prompt


async def main(args):
    async with AsyncSessionLocal() as db:
        session = await db.get(Session, uuid.UUID(args.session_id))
        if session is None:
            raise ValueError("Session not found")
        agent = await db.get(Agent, session.agent_id)
        assert (agent.tools_config or {}).get("system_plugin") == "arthur_v2"
        # Detached view: never mark an ORM record dirty or trigger autoflush.
        agent = SimpleNamespace(**{column.name: getattr(agent, column.name) for column in agent.__table__.columns})
        agent.instructions = build_arthur_v2_system_prompt()
        memories = await list_memories(agent.id, db, scope=session.external_user_id)
        memory = build_arthur_recall(memories, args.message)
        history = bound_arthur_history(db_messages_to_lc(await load_history(session.id, db, max_turns=15)))
        new_project = starts_new_project(args.message)
        if new_project:
            history = []
        prompt = build_system_prompt(
            agent_model=agent, session=session, active_groups=["builder", "memory"],
            saved_custom_tools=[], subagent_list=[], sender_name=None,
            context_summary="" if new_project else (session.metadata_ or {}).get("context_summary", "")[:3500],
            memory_block=memory, layered_memory={}, rag_context="", escalation_user_jid=None,
            escalation_context=None, is_operator_message=False, user_message=args.message,
        )
        tools = build_arthur_v2_tools(db_factory=AsyncSessionLocal, owner_phone=_resolve_builder_owner_phone(session), self_agent_id=str(agent.id), session_id=str(session.id))
        snapshots = {}
        for tool in tools:
            if tool.name in {"get_current_plan", "list_managed_assistants"}:
                snapshots[tool.name] = await tool.ainvoke({})
        calls = []
        creations = []

        def simulate(tool):
            async def invoke(**kwargs):
                calls.append(tool.name)
                if tool.name in snapshots:
                    return snapshots[tool.name]
                if tool.name == "create_assistant":
                    assert kwargs.get("confirmed") is True
                    creations.append(kwargs)
                    return {"ok": True, "simulated": True, "agent_id": "00000000-0000-0000-0000-000000000099", "assistant": {"id": "00000000-0000-0000-0000-000000000099", "name": kwargs["name"], "purpose": kwargs["purpose"], "active": True}, "google_workspace": {"status": "auth_required", "auth_url": "https://example.invalid/simulated-oauth"}}
                return {"ok": False, "simulated": True, "error": "Read-only replay: action disabled; report the current simulated result."}
            return StructuredTool.from_function(coroutine=invoke, name=tool.name, description=tool.description, args_schema=tool.args_schema)

        _, llm = build_agent_llms(agent, get_settings(), 0.2)
        graph = create_react_agent(llm, tools=[simulate(t) for t in tools], prompt=prompt)
        result = await graph.ainvoke({"messages": history + [HumanMessage(content=args.message)]}, config={"recursion_limit": 12})
        messages = result["messages"]
        final = messages[-1]
        print(json.dumps({
            "memory_count": len(memories), "recall_chars": len(memory), "system_chars": len(prompt),
            "history_chars": sum(len(str(m.content)) for m in history), "calls": calls,
            "simulated_creations": len(creations), "final_reply_chars": len(str(final.content)),
            "completion_tokens": sum((getattr(m, "usage_metadata", None) or {}).get("output_tokens", 0) for m in messages[len(history)+1:]),
            "instructions_chars": [len(c["instructions"]) for c in creations],
            "created_names": [c["name"] for c in creations],
            "google_services": [c.get("google_workspace_services", []) for c in creations],
        }))
        assert final.content and not getattr(final, "tool_calls", [])
        if args.expect_no_create:
            assert not creations, "A new incomplete project must not reuse a previous brief"
        else:
            assert len(creations) == 1, "Expected exactly one simulated creation"
            assert creations[0]["name"].casefold() == args.expected_name.casefold()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--message", default="buat lapibot")
    parser.add_argument("--expected-name", default="LapiBot")
    parser.add_argument("--expect-no-create", action="store_true")
    asyncio.run(main(parser.parse_args()))

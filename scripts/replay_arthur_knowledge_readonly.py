"""Replay the real refusal with model inference and simulated mutations only."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from sqlalchemy import select, text

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.models.message import Message
from app.models.session import Session
from app.core.domain.agent_ownership import agent_belongs_to_owner
from app.core.domain.memory_service import list_memories
from app.core.engine.agent_llm import build_agent_llms
from app.core.engine.agent_tool_setup import _resolve_builder_owner_phone
from app.core.engine.arthur_context import build_arthur_recall, bound_arthur_history
from app.core.engine.context_service import db_messages_to_lc, load_history
from app.core.engine.prompt_builder import build_system_prompt
from arthur_v2.plugin import build_arthur_v2_tools
from arthur_v2.knowledge_context import build_uploaded_knowledge_context


@asynccontextmanager
async def readonly():
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        yield db


async def main(args):
    async with readonly() as db:
        session = await db.get(Session, uuid.UUID(args.session_id))
        source_agent = await db.get(Agent, session.agent_id)
        target = await db.get(Agent, uuid.UUID(args.target_id))
        owner = _resolve_builder_owner_phone(session)
        assert agent_belongs_to_owner(target, owner)
        assert (source_agent.tools_config or {}).get('system_plugin') == 'arthur_v2'
        agent = SimpleNamespace(**{c.name: getattr(source_agent, c.name) for c in source_agent.__table__.columns})
        # Deliberately retain the old stored prompt: runtime contract must fix it.
        failure = (await db.execute(select(Message).where(Message.run_id == uuid.UUID(args.run_id), Message.role == 'agent').order_by(Message.timestamp.desc()).limit(1))).scalar_one()
        rows = await load_history(session.id, db, max_turns=15)
        rows = [r for r in rows if r.timestamp < failure.timestamp]
        history = bound_arthur_history(db_messages_to_lc(rows))
        assert history and isinstance(history[-1], HumanMessage)
        memory = build_arthur_recall(await list_memories(agent.id, db, scope=owner), '2')
        inventory = await build_uploaded_knowledge_context(str(session.id), owner, db)
        assert '"complete": true' in inventory
        specs = build_arthur_v2_tools(db_factory=readonly, owner_phone=owner, self_agent_id=str(agent.id), session_id=str(session.id))
        target_snapshot = {'id': str(target.id), 'name': target.name, 'instructions': target.instructions, 'version': target.version}

    _, model = build_agent_llms(agent, get_settings(), 0)
    for existing in [False, True]:
        calls = []
        def simulate(spec):
            async def invoke(**kwargs):
                calls.append(spec.name)
                if spec.name == 'list_managed_assistants':
                    return {'assistants': [target_snapshot]}
                if spec.name == 'inspect_managed_assistant' and kwargs.get('agent_id') == str(target.id):
                    return {'ok': True, 'assistant': target_snapshot}
                if spec.name == 'add_assistant_knowledge':
                    assert kwargs['agent_id'] == str(target.id) and kwargs.get('confirmed') is True
                    return {'ok': True, 'simulated': True, 'assistant': target_snapshot,
                            'chunks_added': 0 if existing else 13, 'chunks_existing': 13 if existing else 0,
                            'already_added': existing, 'rag_enabled': True}
                return {'ok': False, 'error': 'Read-only replay: unrelated actions are disabled.'}
            return StructuredTool.from_function(coroutine=invoke, name=spec.name, description=spec.description, args_schema=spec.args_schema)

        prompt = build_system_prompt(agent_model=agent, session=session, active_groups=['builder','memory'],
            saved_custom_tools=[], subagent_list=[], sender_name=None, context_summary='',
            memory_block=memory + ('\n\n' + inventory if existing else ''), layered_memory={}, rag_context='',
            escalation_user_jid=None, escalation_context=None, is_operator_message=False, user_message='2')
        graph = create_react_agent(model, tools=[simulate(spec) for spec in specs], prompt=prompt)
        async with asyncio.timeout(180):
            result = await graph.ainvoke({'messages': history}, config={'recursion_limit': 12})
        final = str(result['messages'][-1].content)
        wrong_refusal = any(phrase in final.casefold() for phrase in ['tidak bisa memilih opsi 2', 'belum saya tambahkan', 'tidak bisa menambahkan', 'saya tidak dapat menambahkan'])
        print(json.dumps({'scenario': 'already_imported' if existing else 'simulated_fresh_import',
            'calls': calls, 'final_chars': len(final), 'wrong_refusal': wrong_refusal,
            'runtime_contract_present': '## Arthur knowledge ingestion contract' in prompt}, ensure_ascii=False), flush=True)
        assert final.strip() and not wrong_refusal
        if not existing:
            if 'add_assistant_knowledge' not in calls:
                print(json.dumps({'replay_diagnostic': final[:1500]}, ensure_ascii=False), flush=True)
            assert 'add_assistant_knowledge' in calls, 'Expected authorized knowledge tool call'
        assert all(name in {'list_managed_assistants','inspect_managed_assistant','add_assistant_knowledge'} for name in calls)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--run-id', required=True)
    asyncio.run(main(parser.parse_args()))

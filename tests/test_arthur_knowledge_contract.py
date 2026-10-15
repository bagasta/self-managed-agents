import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_arthur_p1_operations import database, agent_row, OWNER
from app.models.document import Document
from app.core.engine.prompt_builder import build_system_prompt
from arthur_v2.knowledge_context import complete_import, build_uploaded_knowledge_context


def test_runtime_contract_applies_to_old_seed_without_replacing_instructions():
    old = 'Preserved system agent instructions from an older seed'
    agent = SimpleNamespace(id=uuid.uuid4(), name='Arthur', model='test', instructions=old,
        tools_config={'system_plugin': 'arthur_v2'}, capabilities=['builder'], safety_policy={}, escalation_config={})
    session = SimpleNamespace(id=uuid.uuid4(), agent_id=agent.id, channel_config={}, channel_type='whatsapp', external_user_id=OWNER, metadata_={})
    def prompt():
        return build_system_prompt(agent_model=agent, session=session, active_groups=['builder'], saved_custom_tools=[], subagent_list=[], sender_name=None, context_summary='', memory_block='', layered_memory={}, rag_context='', escalation_user_jid=None, escalation_context=None, is_operator_message=False)
    assert old in prompt()
    assert '## Arthur knowledge ingestion contract' in prompt()
    assert agent.instructions == old
    agent.tools_config = {'rag': True}
    assert '## Arthur knowledge ingestion contract' not in prompt()


@pytest.mark.parametrize('metadata,complete', [
    ([], False), ([{'chunk_index': 1, 'total_chunks': 1}], True),
    ([{'chunk_index': 1, 'total_chunks': 2}], False),
    ([{'chunk_index': 1, 'total_chunks': 2}, {'chunk_index': 1, 'total_chunks': 2}], False),
    ([{'chunk_index': 1, 'total_chunks': 2}, {'chunk_index': 2, 'total_chunks': 2}], True),
    ([{'chunk_index': 1, 'total_chunks': 2}, {'chunk_index': 2, 'total_chunks': 3}], False),
])
def test_completion_requires_one_consistent_full_chunk_set(metadata, complete):
    assert complete_import(metadata) is complete


@pytest.mark.asyncio
async def test_live_status_uses_exact_hash_and_owner_and_leaves_database_unchanged(database, tmp_path, monkeypatch):
    from app.core.infra import sandbox
    from app.models.agent import Agent
    from arthur_v2 import knowledge_context
    # SQLite has no PostgreSQL JSONB @> operator used by the shared legacy
    # ownership filter. These fixtures all have explicit owner_external_id.
    monkeypatch.setattr(knowledge_context, 'owner_filter', lambda phone: Agent.owner_external_id == phone)
    workspace = tmp_path / 'workspace'
    root = workspace / 'shared' / 'current_input'
    root.mkdir(parents=True)
    text = 'Original knowledge source'
    (root / 'knowledge.txt').write_text(text)
    (root / 'knowledge.extracted.txt').write_text('Generated preview')
    outside = tmp_path / 'private.txt'
    outside.write_text('Private external file')
    (root / 'escape.txt').symlink_to(outside)
    monkeypatch.setattr(sandbox, 'get_workspace_dir', lambda _: workspace)
    a = await agent_row(database, name='Owned target')
    foreign = await agent_row(database, owner='6289999999999', name='Foreign target')
    digest = hashlib.sha256(text.encode()).hexdigest()
    async with database() as db:
        await db.run_sync(lambda s: Document.__table__.create(s.connection()))
        for agent in [a, foreign]:
            db.add(Document(agent_id=agent.id, title='Source', content=text, source='knowledge.txt',
                doc_metadata={'content_sha256': digest, 'chunk_index': 1, 'total_chunks': 1}))
        await db.commit()
        status = await build_uploaded_knowledge_context('session', OWNER, db)
        assert 'Owned target' in status
        assert '"complete": true' in status
        assert 'Foreign target' not in status
        assert 'Original knowledge source' not in status
        assert 'extracted.txt' not in status
        assert 'escape.txt' not in status
        assert not db.dirty and not db.new
        assert len((await db.execute(select(Document))).scalars().all()) == 2
        assert await build_uploaded_knowledge_context('session', '', db) == ''
        (root / 'knowledge.txt').write_text('Different file contents, same name')
        changed = await build_uploaded_knowledge_context('session', OWNER, db)
        assert '"matching_owned_imports": []' in changed

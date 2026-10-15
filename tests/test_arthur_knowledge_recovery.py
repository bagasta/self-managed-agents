import io
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from test_arthur_p1_operations import database, agent_row, OWNER
from app.core.domain import file_processor as processor, document_service
from app.models.agent import Agent
from app.models.document import Document
from arthur_v2.plugin import build_arthur_v2_tools


def pdf_bytes(*texts):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


TEXT = 'Knowledge for a participant: ask their name and record the answer in the sheet.'


@pytest.mark.asyncio
async def test_pdf_uses_local_text_when_ocr_is_rate_limited(monkeypatch):
    request = httpx.Request('POST', 'https://api.mistral.ai/v1/ocr')
    failure = httpx.HTTPStatusError('rate limited', request=request, response=httpx.Response(429, request=request))
    monkeypatch.setattr(processor, '_extract_document_mistral', AsyncMock(side_effect=failure))
    result = await processor.extract_text(pdf_bytes(TEXT), 'knowledge.pdf', None, 'test-key')
    assert TEXT in result


@pytest.mark.asyncio
async def test_missing_ocr_key_still_reads_digital_pdf():
    assert TEXT in await processor.extract_text(pdf_bytes(TEXT), 'knowledge.pdf', None, '')


@pytest.mark.parametrize('data', [b'invalid pdf', pdf_bytes(''), pdf_bytes(TEXT, '')])
def test_local_extractor_rejects_invalid_scanned_and_partial_pdf(data):
    assert processor._extract_pdf_local(data) == ''


@pytest.mark.asyncio
async def test_scanned_pdf_preserves_provider_failure(monkeypatch):
    monkeypatch.setattr(processor, '_extract_document_mistral', AsyncMock(side_effect=RuntimeError('OCR unavailable')))
    with pytest.raises(RuntimeError, match='OCR unavailable'):
        await processor.extract_text(pdf_bytes(''), 'scan.pdf', None, 'test-key')


@pytest.mark.asyncio
@pytest.mark.parametrize('statuses,expected_calls,succeeds', [([429, 200], 2, True), ([503, 200], 2, True), ([400], 1, False), ([429, 429, 429], 3, False)])
async def test_ocr_retry_is_bounded_and_always_cleans_upload(monkeypatch, statuses, expected_calls, succeeds):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == 'DELETE':
            return httpx.Response(204)
        if request.url.path.endswith('/files'):
            return httpx.Response(200, json={'id': 'upload-1'})
        if request.url.path.endswith('/url'):
            return httpx.Response(200, json={'url': 'https://example.test/file'})
        count = sum(path.endswith('/ocr') for _, path in calls)
        status = statuses[min(count - 1, len(statuses) - 1)]
        return httpx.Response(status, json={'pages': [{'index': 0, 'markdown': TEXT}]}, headers={'Retry-After': '1'})
    client_cls = httpx.AsyncClient
    monkeypatch.setattr(processor.httpx, 'AsyncClient', lambda **kwargs: client_cls(transport=httpx.MockTransport(handler)))
    sleep = AsyncMock()
    monkeypatch.setattr(processor.asyncio, 'sleep', sleep)
    if succeeds:
        assert TEXT in await processor._extract_document_mistral(b'pdf', 'test.pdf', 'test-key')
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await processor._extract_document_mistral(b'pdf', 'test.pdf', 'test-key')
    assert sum(path.endswith('/ocr') for _, path in calls) == expected_calls
    assert calls.count(('POST', '/v1/files')) == 1
    assert calls[-1] == ('DELETE', '/v1/files/upload-1')
    assert sleep.await_count == expected_calls - 1


@pytest.mark.asyncio
async def test_tool_reads_original_not_preview_and_deduplicates(database, tmp_path, monkeypatch):
    from app.core.infra import sandbox
    async with database() as db:
        await db.run_sync(lambda s: Document.__table__.create(s.connection()))
        await db.commit()
    agent = await agent_row(database)
    workspace = tmp_path / 'workspace'
    current = workspace / 'shared' / 'current_input'
    current.mkdir(parents=True)
    (current / 'knowledge.txt').write_text(TEXT)
    (current / 'knowledge.extracted.txt').write_text('Truncated preview must never become knowledge')
    (workspace / 'unrelated.txt').write_text('Unrelated newer file')
    monkeypatch.setattr(sandbox, 'get_workspace_dir', lambda _: workspace)
    monkeypatch.setattr(document_service, '_generate_embedding', AsyncMock(return_value=None))
    monkeypatch.setenv('CHANNEL_SECRET_KEY', 'isolated-knowledge-test-key')
    tool = next(t for t in build_arthur_v2_tools(db_factory=database, owner_phone=OWNER, self_agent_id=None, session_id=str(uuid.uuid4())) if t.name == 'add_assistant_knowledge')
    args = {'agent_id': str(agent.id), 'confirmed': True}
    result = await tool.ainvoke(args)
    assert result['ok'] is True
    assert result['filename'] == 'knowledge.txt'
    assert result['chunks_added'] == 1
    extraction = AsyncMock(side_effect=RuntimeError('OCR now unavailable'))
    monkeypatch.setattr(processor, 'extract_text', extraction)
    repeat = await tool.ainvoke(args)
    assert repeat['already_added'] is True
    assert repeat['chunks_added'] == 0
    extraction.assert_not_awaited()
    async with database() as db:
        docs = (await db.execute(select(Document))).scalars().all()
        assert len(docs) == 1
        assert docs[0].content == TEXT
        assert (await db.get(Agent, agent.id)).tools_config['rag'] is True
    foreign = next(t for t in build_arthur_v2_tools(db_factory=database, owner_phone='6289999999999', self_agent_id=None, session_id=str(uuid.uuid4())) if t.name == 'add_assistant_knowledge')
    assert (await foreign.ainvoke(args))['ok'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['embedding', 'vector', 'empty'])
async def test_rag_context_and_explicit_tool_fall_back_to_keyword(database, monkeypatch, failure):
    from app.core.domain import embedding_service
    from app.core.engine.prompt_builder import build_rag_context
    from app.core.tools.rag_tool import build_rag_tools
    from types import SimpleNamespace

    agent_id = uuid.uuid4()
    doc = SimpleNamespace(title='Knowledge', source='knowledge.txt', content=TEXT)
    monkeypatch.setattr(embedding_service, 'embed_text', AsyncMock(side_effect=RuntimeError('offline')) if failure == 'embedding' else AsyncMock(return_value=[0.0]))
    monkeypatch.setattr(document_service, 'search_documents_vector', AsyncMock(side_effect=RuntimeError('vector offline')) if failure == 'vector' else AsyncMock(return_value=[]))
    keyword = AsyncMock(return_value=[doc])
    monkeypatch.setattr(document_service, 'search_documents_keyword', keyword)
    log = SimpleNamespace(debug=lambda *a, **kw: None, warning=lambda *a, **kw: None)
    async with database() as db:
        context = await build_rag_context(agent_id, 'participant', db, {'rag': True}, log)
    assert TEXT in context
    result = await build_rag_tools(agent_id, database, {'rag': True})[0].ainvoke({'query': 'participant'})
    assert result.startswith('Found 1 document(s)')
    assert TEXT in result
    assert keyword.await_count == 2
    assert all(call.args[0] == agent_id for call in keyword.await_args_list)

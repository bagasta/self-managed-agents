import base64
import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from docx import Document

from app.api.wa_helpers import process_wa_media, register_session_media
from app.core.domain import file_processor as processor
from test_arthur_knowledge_recovery import pdf_bytes, TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,mime", [(None, "application/pdf"), ("cv.bin", "application/pdf; charset=binary"), (None, None)])
async def test_pdf_without_extension_preserves_source_and_extracts_locally(tmp_path, monkeypatch, filename, mime):
    from app.core.infra import sandbox
    monkeypatch.setattr(sandbox, "get_workspace_dir", lambda _: tmp_path)
    ocr = AsyncMock(side_effect=AssertionError("Digital PDF must not require OCR"))
    monkeypatch.setattr(processor, "_extract_document_mistral", ocr)
    raw = pdf_bytes(TEXT)
    context, image, _, meta = await process_wa_media(media_type="document", media_data=base64.b64encode(raw).decode(),
        media_filename=filename, media_mimetype=mime, session_id=uuid.uuid4(), logger=structlog.get_logger())
    assert meta["filename"].endswith(".pdf") and meta["mimetype"] == "application/pdf"
    assert meta["processing_status"] == "completed" and TEXT in context
    assert image is None
    assert (tmp_path / meta["filename"]).read_bytes() == raw
    assert TEXT in (tmp_path / "shared/current_input" / meta["extracted_text_filename"]).read_text()
    ocr.assert_not_awaited()


@pytest.mark.asyncio
async def test_cv_photo_sent_as_document_uses_vision(tmp_path, monkeypatch):
    from app.core.infra import sandbox
    monkeypatch.setattr(sandbox, "get_workspace_dir", lambda _: tmp_path)
    data = base64.b64encode(b"test-image").decode()
    context, image, mime, meta = await process_wa_media(media_type="document", media_data=data,
        media_filename="cv", media_mimetype="image/png", session_id=uuid.uuid4(), logger=structlog.get_logger())
    assert image == data and mime == "image/png"
    assert meta["processing_route"] == "image" and meta["filename"] == "cv.png"
    assert "ditampilkan" in context


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_office_parser_survives_ocr_outage_and_empty_is_not_success(tmp_path, monkeypatch, empty):
    from app.core.infra import sandbox
    monkeypatch.setattr(sandbox, "get_workspace_dir", lambda _: tmp_path)
    request = httpx.Request("POST", "https://api.mistral.ai/v1/ocr")
    failure = httpx.HTTPStatusError("rate limited", request=request, response=httpx.Response(429, request=request))
    ocr = AsyncMock(side_effect=failure)
    monkeypatch.setattr(processor, "_extract_document_mistral", ocr)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(
        mistral_api_key="test-key", arthur_document_model="mistral-ocr-latest", media_doc_max_chars=12000))
    document = Document()
    if not empty:
        document.add_paragraph(TEXT)
    data = io.BytesIO()
    document.save(data)
    context, _, _, meta = await process_wa_media(media_type="document", media_data=base64.b64encode(data.getvalue()).decode(),
        media_filename="cv.docx", session_id=uuid.uuid4(), logger=structlog.get_logger(), arthur_model_routing=True)
    assert meta["processing_status"] == ("failed" if empty else "completed")
    assert ("ATTACHMENT_PROCESSING_FAILED" in context) is empty
    ocr.assert_awaited_once()
    if not empty:
        assert TEXT in context


@pytest.mark.asyncio
async def test_unsupported_document_remains_available_without_fake_read_receipt(tmp_path, monkeypatch):
    from app.core.infra import sandbox
    monkeypatch.setattr(sandbox, "get_workspace_dir", lambda _: tmp_path)
    context, _, _, meta = await process_wa_media(media_type="document", media_data=base64.b64encode(b"archive").decode(),
        media_filename="cv.zip", session_id=uuid.uuid4(), logger=structlog.get_logger())
    assert meta["processing_status"] == "unsupported"
    assert "ATTACHMENT_PROCESSING_FAILED" in context and "File asli tetap tersedia" in context


def test_attachment_lifecycle_clears_both_current_markers_but_retains_explicit_history():
    session = SimpleNamespace(metadata_={"unrelated": True})
    meta = {"filename": "cv.pdf", "workspace_path": "/source/cv.pdf", "current_input_path": "/workspace/shared/current_input/cv.pdf"}
    assert register_session_media(session, meta, source_message_id="wamid.cv")
    assert session.metadata_["current_attachment"]["input_path"] == meta["current_input_path"]
    assert session.metadata_["last_incoming_media"]["source_message_id"] == "wamid.cv"
    assert register_session_media(session, None)
    assert "current_turn_media" not in session.metadata_ and "current_attachment" not in session.metadata_
    assert session.metadata_["last_incoming_media"]["filename"] == "cv.pdf"
    assert session.metadata_["unrelated"]

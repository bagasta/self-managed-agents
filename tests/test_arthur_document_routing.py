import asyncio

import pytest

from app.core.domain import file_processor


@pytest.fixture(autouse=True)
def _leave_legacy_default_event_loop_available():
    yield
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_mime"),
    [
        ("guide.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
async def test_arthur_office_documents_use_mistral(monkeypatch, filename, expected_mime):
    captured = {}

    async def fake_mistral(content, actual_filename, api_key, *, model, content_type):
        captured.update(
            content=content,
            filename=actual_filename,
            api_key=api_key,
            model=model,
            content_type=content_type,
        )
        return "trusted document text"

    monkeypatch.setattr(file_processor, "_extract_document_mistral", fake_mistral)
    result = await file_processor.extract_text(
        b"office-bytes",
        filename,
        None,
        "mistral-key",
        use_mistral_for_office=True,
        mistral_model="mistral-ocr-latest",
    )
    assert result == "trusted document text"
    assert captured["model"] == "mistral-ocr-latest"
    assert captured["content_type"] == expected_mime


@pytest.mark.asyncio
async def test_arthur_office_document_without_mistral_key_fails_closed():
    with pytest.raises(ValueError, match="MISTRAL_API_KEY"):
        await file_processor.extract_text(
            b"office-bytes",
            "guide.docx",
            None,
            "",
            use_mistral_for_office=True,
        )

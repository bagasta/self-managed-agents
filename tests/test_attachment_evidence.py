import asyncio
from types import SimpleNamespace

import pytest

from app.core.engine import attachment_evidence


@pytest.fixture(autouse=True)
def _leave_legacy_default_event_loop_available():
    yield
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _Log:
    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


def _settings(**overrides):
    values = {
        "arthur_image_model": "openai/gpt-4.1-mini",
        "openrouter_api_key": "test-key",
        "llm_request_timeout_seconds": 1.0,
        "llm_max_retries": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_invalid_image_payload_fails_closed_without_model_call(monkeypatch):
    def fail_if_called(**_kwargs):
        raise AssertionError("vision provider must not be called")

    monkeypatch.setattr(attachment_evidence, "ChatOpenAI", fail_if_called)
    evidence = await attachment_evidence.extract_image_evidence(
        image_b64="not-base64",
        mime_type="image/png",
        filename="proof.png",
        user_request="baca gambar",
        settings=_settings(),
        log=_Log(),
    )
    assert evidence.status == "failed"
    assert "Dilarang menebak isi attachment" in evidence.to_prompt()


@pytest.mark.asyncio
async def test_image_is_read_by_gpt_41_mini_and_returns_evidence(monkeypatch):
    captured = {}

    class FakeVision:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content="Terlihat label produk berwarna biru; teks: Veselka.")

    monkeypatch.setattr(attachment_evidence, "ChatOpenAI", FakeVision)
    evidence = await attachment_evidence.extract_image_evidence(
        image_b64="aGVsbG8=",
        mime_type="image/jpeg",
        filename="product.jpg",
        user_request="Apa isi foto ini?",
        settings=_settings(),
        log=_Log(),
    )
    assert captured["model"] == "openai/gpt-4.1-mini"
    assert evidence.model == "openai/gpt-4.1-mini"
    assert evidence.status == "completed"
    assert "extracted_evidence" in evidence.to_prompt()
    assert "Veselka" in evidence.to_prompt()


@pytest.mark.asyncio
async def test_vision_provider_failure_does_not_cross_model_fallback(monkeypatch):
    calls = 0

    class FailingVision:
        def __init__(self, **_kwargs):
            pass

        async def ainvoke(self, _messages):
            nonlocal calls
            calls += 1
            raise RuntimeError("provider down")

    monkeypatch.setattr(attachment_evidence, "ChatOpenAI", FailingVision)
    evidence = await attachment_evidence.extract_image_evidence(
        image_b64="aGVsbG8=",
        mime_type="image/webp",
        filename="screen.webp",
        user_request="baca",
        settings=_settings(llm_max_retries=1),
        log=_Log(),
    )
    assert calls == 2
    assert evidence.status == "failed"
    assert evidence.model == "openai/gpt-4.1-mini"
    assert "provider down" in (evidence.warning or "")

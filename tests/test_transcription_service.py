"""
Tests untuk transcription_service.py (TDD).

Jalankan dengan:
    cd /home/bagas/managed-agents-project
    python -m pytest tests/test_transcription_service.py -v
"""
from __future__ import annotations

import pytest
import httpx
import pytest_asyncio

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

FAKE_KEY = "sk-or-test-123"
FAKE_B64 = "SGVsbG8gV29ybGQ="  # "Hello World" in base64
SUCCESS_RESPONSE = {
    "choices": [{"message": {"content": "Halo, ini transkrip audio."}}]
}
EMPTY_RESPONSE = {"choices": [{"message": {"content": ""}}]}
ERROR_RESPONSE = {"error": {"message": "model not found"}}


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


class TestTranscribeAudio:
    """Unit tests untuk fungsi transcribe_audio."""

    @pytest.mark.asyncio
    async def test_returns_transcript_on_success(self, respx_mock):
        """Harus mengembalikan teks transkripsi dari response OpenRouter."""
        from app.core.transcription_service import transcribe_audio

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        result = await transcribe_audio(
            FAKE_B64, "ogg", openrouter_api_key=FAKE_KEY
        )
        assert result == "Halo, ini transkrip audio."

    @pytest.mark.asyncio
    async def test_returns_fallback_on_empty_content(self, respx_mock):
        """Harus mengembalikan fallback jika konten response kosong."""
        from app.core.transcription_service import transcribe_audio, TRANSCRIPTION_FALLBACK

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=EMPTY_RESPONSE)
        )

        result = await transcribe_audio(
            FAKE_B64, "ogg", openrouter_api_key=FAKE_KEY
        )
        assert result == TRANSCRIPTION_FALLBACK

    @pytest.mark.asyncio
    async def test_returns_fallback_without_api_key(self):
        """Harus mengembalikan fallback jika api_key tidak disediakan."""
        from app.core.transcription_service import transcribe_audio, TRANSCRIPTION_FALLBACK

        result = await transcribe_audio(FAKE_B64, "ogg", openrouter_api_key="")
        assert result == TRANSCRIPTION_FALLBACK

    @pytest.mark.asyncio
    async def test_returns_fallback_on_http_error(self, respx_mock):
        """Harus mengembalikan fallback jika HTTP request gagal (4xx/5xx)."""
        from app.core.transcription_service import transcribe_audio, TRANSCRIPTION_FALLBACK

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "server error"})
        )

        result = await transcribe_audio(
            FAKE_B64, "ogg", openrouter_api_key=FAKE_KEY
        )
        assert result == TRANSCRIPTION_FALLBACK

    @pytest.mark.asyncio
    async def test_returns_fallback_on_network_error(self, respx_mock):
        """Harus mengembalikan fallback jika koneksi gagal."""
        from app.core.transcription_service import transcribe_audio, TRANSCRIPTION_FALLBACK

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        result = await transcribe_audio(
            FAKE_B64, "ogg", openrouter_api_key=FAKE_KEY
        )
        assert result == TRANSCRIPTION_FALLBACK

    @pytest.mark.asyncio
    async def test_returns_fallback_on_empty_audio(self, respx_mock):
        """Harus mengembalikan fallback jika audio_b64 kosong."""
        from app.core.transcription_service import transcribe_audio, TRANSCRIPTION_FALLBACK

        result = await transcribe_audio("", "ogg", openrouter_api_key=FAKE_KEY)
        assert result == TRANSCRIPTION_FALLBACK

    @pytest.mark.asyncio
    async def test_sends_correct_model_and_format(self, respx_mock):
        """Request harus menggunakan model gpt-audio-mini dan format yang benar."""
        import json
        from app.core.transcription_service import transcribe_audio, TRANSCRIPTION_MODEL

        captured = {}

        def capture_request(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=SUCCESS_RESPONSE)

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            side_effect=capture_request
        )

        await transcribe_audio(FAKE_B64, "mp3", openrouter_api_key=FAKE_KEY)

        body = captured["body"]
        assert body["model"] == TRANSCRIPTION_MODEL
        content = body["messages"][0]["content"]
        audio_part = next(p for p in content if p["type"] == "input_audio")
        assert audio_part["input_audio"]["data"] == FAKE_B64
        assert audio_part["input_audio"]["format"] == "mp3"

    @pytest.mark.asyncio
    async def test_sends_authorization_header(self, respx_mock):
        """Request harus menyertakan header Authorization yang benar."""
        captured = {}

        def capture_request(request):
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=SUCCESS_RESPONSE)

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            side_effect=capture_request
        )

        from app.core.transcription_service import transcribe_audio

        await transcribe_audio(FAKE_B64, "ogg", openrouter_api_key=FAKE_KEY)
        assert captured["headers"].get("authorization") == f"Bearer {FAKE_KEY}"


class TestProcessWaMediaAudio:
    """Unit tests untuk process_wa_media() dengan media_type audio/ptt."""

    @pytest.mark.asyncio
    async def test_ptt_returns_voice_note_label(self, respx_mock, tmp_path, monkeypatch):
        """Voice note (PTT) harus diberi label [Voice note: ...]."""
        import uuid
        from app.api.wa_helpers import process_wa_media

        # get_workspace_dir dan get_settings diimport lokal di dalam fungsi,
        # jadi mock harus di modul asal (source module), bukan di wa_helpers
        monkeypatch.setattr(
            "app.core.sandbox.get_workspace_dir",
            lambda sid: tmp_path,
        )
        class FakeSettings:
            openrouter_api_key = FAKE_KEY
        monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        import structlog
        media_context, img_b64, img_mime = await process_wa_media(
            media_type="ptt",
            media_data=FAKE_B64,
            media_filename="voice.ogg",
            session_id=uuid.uuid4(),
            logger=structlog.get_logger(),
        )

        assert "Voice note" in media_context
        assert "Halo, ini transkrip audio." in media_context
        assert img_b64 is None
        assert img_mime is None

    @pytest.mark.asyncio
    async def test_audio_returns_audio_label(self, respx_mock, tmp_path, monkeypatch):
        """Audio file biasa harus diberi label [Audio: ...]."""
        import uuid
        from app.api.wa_helpers import process_wa_media

        monkeypatch.setattr(
            "app.core.sandbox.get_workspace_dir",
            lambda sid: tmp_path,
        )
        class FakeSettings:
            openrouter_api_key = FAKE_KEY
        monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        import structlog
        media_context, img_b64, img_mime = await process_wa_media(
            media_type="audio",
            media_data=FAKE_B64,
            media_filename="audio.ogg",
            session_id=uuid.uuid4(),
            logger=structlog.get_logger(),
        )

        assert "Audio" in media_context
        assert img_b64 is None

    @pytest.mark.asyncio
    async def test_audio_fallback_on_transcription_failure(self, respx_mock, tmp_path, monkeypatch):
        """Jika transkripsi gagal, harus return fallback tapi tidak raise exception."""
        import uuid
        from app.api.wa_helpers import process_wa_media
        from app.core.transcription_service import TRANSCRIPTION_FALLBACK

        monkeypatch.setattr(
            "app.core.sandbox.get_workspace_dir",
            lambda sid: tmp_path,
        )
        class FakeSettings:
            openrouter_api_key = FAKE_KEY
        monkeypatch.setattr("app.config.get_settings", lambda: FakeSettings())

        respx_mock.post("https://openrouter.ai/api/v1/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "server error"})
        )

        import structlog
        media_context, _, _ = await process_wa_media(
            media_type="ptt",
            media_data=FAKE_B64,
            media_filename="voice.ogg",
            session_id=uuid.uuid4(),
            logger=structlog.get_logger(),
        )

        assert TRANSCRIPTION_FALLBACK in media_context

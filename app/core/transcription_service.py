"""
Transcription service untuk pesan suara (voice note / audio file) dari WhatsApp.

Menggunakan model openai/gpt-audio-mini via OpenRouter dengan format
OpenAI-compatible audio input (input_audio content part).

Format request:
  POST https://openrouter.ai/api/v1/chat/completions
  {
    "model": "openai/gpt-audio-mini",
    "messages": [{
      "role": "user",
      "content": [
        { "type": "input_audio", "input_audio": { "data": "<base64>", "format": "ogg" } },
        { "type": "text", "text": "Transkripsikan audio ini ke teks. Kembalikan hanya teks transkrip." }
      ]
    }]
  }
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import structlog
import httpx

log = structlog.get_logger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
TRANSCRIPTION_MODEL = "openai/gpt-audio-mini"

# Prompt instruksi transkripsi — ringkas dan jelas
_TRANSCRIBE_PROMPT = (
    "Transkripsikan audio ini ke teks dengan tepat. "
    "Kembalikan HANYA teks transkrip, tanpa komentar atau penjelasan tambahan."
)

# Fallback text jika transkripsi gagal
TRANSCRIPTION_FALLBACK = "[Voice note: tidak dapat ditranskripsi]"

# Formats natively supported by OpenAI audio input
_OPENAI_SUPPORTED_FORMATS = {"mp3", "wav"}


async def _convert_to_mp3(audio_b64: str) -> str:
    """Convert base64 audio (any format) to base64 mp3 via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH — install ffmpeg in the container")
    audio_bytes = base64.b64decode(audio_b64)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", "pipe:0", "-f", "mp3", "-codec:a", "libmp3lame", "-q:a", "4", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()[-300:]}")
    return base64.b64encode(stdout).decode()


async def transcribe_audio(
    audio_b64: str,
    audio_format: str = "ogg",
    *,
    openrouter_api_key: str,
    timeout: float = 30.0,
) -> str:
    """
    Transkripsi audio base64 menggunakan openai/gpt-audio-mini via OpenRouter.

    Args:
        audio_b64: Data audio yang sudah di-encode ke base64.
        audio_format: Format audio ("ogg", "mp3", "wav", dll).
        openrouter_api_key: API key OpenRouter.
        timeout: Timeout HTTP request dalam detik.

    Returns:
        Teks hasil transkripsi, atau TRANSCRIPTION_FALLBACK jika gagal.
    """
    if not openrouter_api_key:
        log.warning("transcription_service.no_api_key")
        return TRANSCRIPTION_FALLBACK

    if not audio_b64:
        return TRANSCRIPTION_FALLBACK

    # OpenAI only accepts mp3/wav — convert everything else
    if audio_format not in _OPENAI_SUPPORTED_FORMATS:
        try:
            audio_b64 = await _convert_to_mp3(audio_b64)
            audio_format = "mp3"
        except Exception as exc:
            log.error("transcription_service.conversion_error", error=str(exc))
            return TRANSCRIPTION_FALLBACK

    payload = {
        "model": TRANSCRIPTION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64,
                            "format": audio_format,
                        },
                    },
                    {
                        "type": "text",
                        "text": _TRANSCRIBE_PROMPT,
                    },
                ],
            }
        ],
        "max_tokens": 1024,
    }

    headers = {
        "Authorization": f"Bearer {openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://managed-agents-project",
        "X-Title": "Managed Agents WhatsApp",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            transcript = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            ).strip()
            if transcript:
                log.info(
                    "transcription_service.success",
                    model=TRANSCRIPTION_MODEL,
                    length=len(transcript),
                )
                return transcript
            log.warning("transcription_service.empty_response", data=data)
            return TRANSCRIPTION_FALLBACK

    except httpx.HTTPStatusError as exc:
        log.error(
            "transcription_service.http_error",
            status=exc.response.status_code,
            body=exc.response.text[:500],
        )
    except Exception as exc:
        log.error("transcription_service.error", error=str(exc))

    return TRANSCRIPTION_FALLBACK

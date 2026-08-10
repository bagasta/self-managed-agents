"""
Embedding service using OpenRouter → openai/text-embedding-3-small.

Endpoint : https://openrouter.ai/api/v1/embeddings
Model    : openai/text-embedding-3-small
Dims     : 1536
Auth     : OPENROUTER_API_KEY (same key used for LLM calls)

langchain_openai.OpenAIEmbeddings is used because it handles retries,
batching, and the OpenAI-compatible request/response format.

The embedder instance is cached via lru_cache so the object is created
once per process (avoids repeated httpx client construction).
"""
from __future__ import annotations

import asyncio
from functools import lru_cache

import structlog

logger = structlog.get_logger(__name__)

EMBEDDING_DIM = 1536
EMBEDDING_MODEL = "openai/text-embedding-3-small"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Maximum chars to embed — avoids sending huge documents in one call.
# text-embedding-3-small supports up to 8191 tokens (~32 000 chars);
# we stay well under that.
_MAX_CHARS = 24_000


@lru_cache(maxsize=1)
def _get_embedder():
    from langchain_openai import OpenAIEmbeddings

    from app.config import get_settings
    settings = get_settings()

    if not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is not set — cannot generate embeddings.")

    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=settings.openrouter_api_key,
        base_url=OPENROUTER_BASE_URL,
    )


async def embed_text(text: str) -> list[float]:
    """
    Embed a single text string via OpenRouter.
    Runs the synchronous SDK call in a thread pool to avoid blocking the event loop.
    Returns list[float] of length EMBEDDING_DIM (1536).
    """
    loop = asyncio.get_event_loop()
    truncated = text[:_MAX_CHARS]

    def _sync() -> list[float]:
        embedder = _get_embedder()
        return embedder.embed_query(truncated)

    return await loop.run_in_executor(None, _sync)


async def warmup_embedding_model() -> None:
    """
    Verify connectivity to OpenRouter embeddings on startup.
    Logs a warning if the API key is missing or the call fails — never raises.
    """
    try:
        await embed_text("warmup")
        logger.info(
            "embedding.warmup.complete",
            model=EMBEDDING_MODEL,
            dim=EMBEDDING_DIM,
        )
    except Exception as exc:
        logger.warning("embedding.warmup.failed", error=str(exc))

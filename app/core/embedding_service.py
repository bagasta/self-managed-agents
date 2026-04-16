"""
Embedding service using fastembed (local, ONNX-based, no GPU/API key needed).

Model: paraphrase-multilingual-MiniLM-L12-v2
  - 384 dimensions
  - 50+ languages including Indonesian
  - ~130MB one-time download, cached at ~/.cache/fastembed/

The model is loaded once at startup (warmup_embedding_model) and reused
across all requests via lru_cache.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache

import structlog

logger = structlog.get_logger(__name__)

EMBEDDING_DIM = 384
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@lru_cache(maxsize=1)
def _get_model(model_name: str):
    """Load and cache the embedding model (called once, blocks until ready)."""
    from fastembed import TextEmbedding
    logger.info("embedding.model.loading", model=model_name)
    model = TextEmbedding(model_name=model_name)
    logger.info("embedding.model.ready", model=model_name)
    return model


async def embed_text(text: str, model_name: str = DEFAULT_MODEL) -> list[float]:
    """
    Embed a single piece of text. Returns a list[float] of length EMBEDDING_DIM.
    Runs the CPU-bound work in a thread pool so the event loop stays free.
    """
    loop = asyncio.get_event_loop()

    def _sync() -> list[float]:
        model = _get_model(model_name)
        # fastembed expects an iterable; truncate to avoid OOM on huge docs
        results = list(model.embed([text[:8192]]))
        return results[0].tolist()

    return await loop.run_in_executor(None, _sync)


async def warmup_embedding_model(model_name: str = DEFAULT_MODEL) -> None:
    """
    Pre-load the model on app startup so the first real request isn't slow.
    Downloads the model files if they aren't cached yet.
    """
    try:
        await embed_text("warmup", model_name=model_name)
        logger.info("embedding.warmup.complete", model=model_name, dim=EMBEDDING_DIM)
    except Exception as exc:
        logger.warning("embedding.warmup.failed", error=str(exc))

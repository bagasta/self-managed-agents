"""
Document store service — per-agent knowledge base.

On every create/update the document text is embedded and stored as a
pgvector vector(384) column. Vector search (cosine distance) is used
for RAG retrieval in agent_runner.

Fallback: if embedding fails (pgvector not installed, model not loaded),
documents still save without an embedding and vector search gracefully
returns an empty list.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

async def _generate_embedding(title: str, content: str) -> list[float] | None:
    """Embed title + content. Returns None on any error so upload never blocks."""
    try:
        from app.core.embedding_service import embed_text
        text = f"{title}\n{content}"
        return await embed_text(text)
    except Exception as exc:
        logger.warning("document.embedding_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def create_document(
    agent_id: uuid.UUID,
    title: str,
    content: str,
    source: str | None,
    doc_metadata: dict[str, Any],
    db: AsyncSession,
) -> Document:
    embedding = await _generate_embedding(title, content)
    doc = Document(
        agent_id=agent_id,
        title=title,
        content=content,
        source=source,
        doc_metadata=doc_metadata,
        embedding=embedding,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    logger.info("document.created", doc_id=str(doc.id), has_embedding=embedding is not None)
    return doc


async def get_document(
    agent_id: uuid.UUID, doc_id: uuid.UUID, db: AsyncSession
) -> Document | None:
    result = await db.execute(
        select(Document).where(Document.agent_id == agent_id, Document.id == doc_id)
    )
    return result.scalar_one_or_none()


async def list_documents(
    agent_id: uuid.UUID, db: AsyncSession, limit: int = 50, offset: int = 0
) -> tuple[list[Document], int]:
    total_result = await db.execute(
        select(func.count()).where(Document.agent_id == agent_id)
    )
    total = total_result.scalar_one()
    result = await db.execute(
        select(Document)
        .where(Document.agent_id == agent_id)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


async def update_document(
    agent_id: uuid.UUID,
    doc_id: uuid.UUID,
    updates: dict[str, Any],
    db: AsyncSession,
) -> Document | None:
    doc = await get_document(agent_id, doc_id, db)
    if not doc:
        return None

    content_changed = "content" in updates or "title" in updates
    for field, value in updates.items():
        if value is not None:
            setattr(doc, field, value)

    # Regenerate embedding if title or content changed
    if content_changed:
        doc.embedding = await _generate_embedding(doc.title, doc.content)

    await db.flush()
    await db.refresh(doc)
    return doc


async def delete_document(
    agent_id: uuid.UUID, doc_id: uuid.UUID, db: AsyncSession
) -> bool:
    doc = await get_document(agent_id, doc_id, db)
    if not doc:
        return False
    await db.delete(doc)
    await db.flush()
    return True


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search_documents_vector(
    agent_id: uuid.UUID,
    query_embedding: list[float],
    db: AsyncSession,
    max_results: int = 3,
) -> list[Document]:
    """
    Return up to max_results documents ordered by cosine distance to query_embedding.
    Only considers documents that have an embedding stored.
    """
    try:
        stmt = (
            select(Document)
            .where(
                Document.agent_id == agent_id,
                Document.embedding.isnot(None),
            )
            .order_by(Document.embedding.cosine_distance(query_embedding))
            .limit(max_results)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())
    except Exception as exc:
        logger.warning("document.vector_search_failed", error=str(exc))
        return []


async def search_documents_keyword(
    agent_id: uuid.UUID,
    query: str,
    db: AsyncSession,
    max_results: int = 3,
) -> list[Document]:
    """
    Keyword fallback: ILIKE search across title + content.
    Used when pgvector is not available or no embeddings exist.
    """
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        return []
    stmt = select(Document).where(Document.agent_id == agent_id)
    for word in words:
        pattern = f"%{word}%"
        stmt = stmt.where(
            Document.title.ilike(pattern) | Document.content.ilike(pattern)
        )
    stmt = stmt.order_by(Document.updated_at.desc()).limit(max_results)
    result = await db.execute(stmt)
    return list(result.scalars().all())

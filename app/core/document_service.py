"""
Document store service — per-agent knowledge base for RAG retrieval.

Search uses PostgreSQL ILIKE for simple keyword matching.
Upgrade path: replace _search_query with tsvector / pgvector for semantic search.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document


async def create_document(
    agent_id: uuid.UUID,
    title: str,
    content: str,
    source: str | None,
    doc_metadata: dict[str, Any],
    db: AsyncSession,
) -> Document:
    doc = Document(
        agent_id=agent_id,
        title=title,
        content=content,
        source=source,
        doc_metadata=doc_metadata,
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    return doc


async def get_document(agent_id: uuid.UUID, doc_id: uuid.UUID, db: AsyncSession) -> Document | None:
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
    for field, value in updates.items():
        if value is not None:
            setattr(doc, field, value)
    await db.flush()
    await db.refresh(doc)
    return doc


async def delete_document(agent_id: uuid.UUID, doc_id: uuid.UUID, db: AsyncSession) -> bool:
    doc = await get_document(agent_id, doc_id, db)
    if not doc:
        return False
    await db.delete(doc)
    await db.flush()
    return True


async def search_documents(
    agent_id: uuid.UUID,
    query: str,
    db: AsyncSession,
    max_results: int = 5,
) -> list[Document]:
    """
    Keyword search across title + content using ILIKE.
    Each word in the query must appear in the title OR content.
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

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.document_service import (
    create_document,
    delete_document,
    get_document,
    list_documents,
    update_document,
)
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.document import DocumentCreate, DocumentListResponse, DocumentResponse, DocumentUpdate

router = APIRouter(
    prefix="/v1/agents/{agent_id}/documents",
    tags=["documents"],
    dependencies=[Depends(verify_api_key)],
)


async def _get_agent_or_404(agent_id: uuid.UUID, db: AsyncSession) -> Agent:
    from sqlalchemy import select
    result = await db.execute(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def add_document(
    agent_id: uuid.UUID,
    body: DocumentCreate,
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """Add a document to the agent's knowledge base."""
    await _get_agent_or_404(agent_id, db)
    doc = await create_document(
        agent_id=agent_id,
        title=body.title,
        content=body.content,
        source=body.source,
        doc_metadata=body.doc_metadata,
        db=db,
    )
    await db.commit()
    await db.refresh(doc)
    return DocumentResponse.model_validate(doc)


@router.get("", response_model=DocumentListResponse)
async def list_agent_documents(
    agent_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    """List all documents in the agent's knowledge base."""
    await _get_agent_or_404(agent_id, db)
    docs, total = await list_documents(agent_id, db, limit=limit, offset=offset)
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in docs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{doc_id}", response_model=DocumentResponse)
async def get_agent_document(
    agent_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """Get a specific document by ID."""
    await _get_agent_or_404(agent_id, db)
    doc = await get_document(agent_id, doc_id, db)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentResponse.model_validate(doc)


@router.patch("/{doc_id}", response_model=DocumentResponse)
async def update_agent_document(
    agent_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: DocumentUpdate,
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """Update a document."""
    await _get_agent_or_404(agent_id, db)
    updates = body.model_dump(exclude_none=True)
    doc = await update_document(agent_id, doc_id, updates, db)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await db.commit()
    await db.refresh(doc)
    return DocumentResponse.model_validate(doc)


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_document(
    agent_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a document from the knowledge base."""
    await _get_agent_or_404(agent_id, db)
    deleted = await delete_document(agent_id, doc_id, db)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await db.commit()

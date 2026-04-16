import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.document_service import (
    create_document,
    delete_document,
    get_document,
    list_documents,
    search_documents_keyword,
    search_documents_vector,
    update_document,
)
from app.core.embedding_service import embed_text
from app.core.file_processor import SUPPORTED_EXTENSIONS, chunk_text, extract_text
from app.database import get_db
from app.deps import verify_api_key
from app.models.agent import Agent
from app.schemas.document import (
    DocumentCreate,
    DocumentListResponse,
    DocumentResponse,
    DocumentSearchRequest,
    DocumentSearchResponse,
    DocumentUpdate,
    DocumentUploadResponse,
)

settings = get_settings()

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


@router.post("/search", response_model=DocumentSearchResponse)
async def search_agent_documents(
    agent_id: uuid.UUID,
    body: DocumentSearchRequest,
    db: AsyncSession = Depends(get_db),
) -> DocumentSearchResponse:
    """Semantic search across agent's knowledge base using pgvector cosine similarity."""
    await _get_agent_or_404(agent_id, db)
    try:
        query_embedding = await embed_text(body.query)
        docs = await search_documents_vector(agent_id, query_embedding, db, max_results=body.max_results)
    except Exception:
        docs = await search_documents_keyword(agent_id, body.query, db, max_results=body.max_results)
    return DocumentSearchResponse(
        results=[DocumentResponse.model_validate(d) for d in docs],
        total=len(docs),
        query=body.query,
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


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file (PDF/TXT/DOCX/PPTX) to the knowledge base",
)
async def upload_document_file(
    agent_id: uuid.UUID,
    file: UploadFile = File(..., description="PDF, TXT, MD, DOCX, or PPTX file"),
    title: str | None = Form(
        None,
        description="Document title. Defaults to the filename.",
    ),
    source: str | None = Form(
        None,
        description="Optional source label (e.g. 'notion/handbook', 'confluence/dev-guide').",
    ),
    db: AsyncSession = Depends(get_db),
) -> DocumentUploadResponse:
    """
    Upload a file and add it to the agent's RAG knowledge base.

    The file is parsed and split into chunks (≤1200 chars each) so each
    chunk gets its own embedding for fine-grained retrieval.

    PDFs are processed with Mistral OCR (mistral-ocr-latest).
    DOCX and PPTX are parsed with python-docx / python-pptx.
    TXT / MD are read directly.

    Returns the list of document chunks created.
    """
    await _get_agent_or_404(agent_id, db)

    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if f".{ext}" not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported file extension '.{ext}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            ),
        )

    # Read file content
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty.",
        )

    # Extract text (PDF → Mistral OCR, DOCX/PPTX → lib, TXT → decode)
    try:
        full_text = await extract_text(
            content=raw_bytes,
            filename=filename,
            content_type=file.content_type,
            mistral_api_key=settings.mistral_api_key,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Text extraction failed: {exc}",
        )

    if not full_text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No text could be extracted from the file.",
        )

    doc_title = title or filename
    doc_source = source or filename

    # Split into chunks for better retrieval granularity
    chunks = chunk_text(full_text)

    created_docs = []
    total = len(chunks)
    for i, chunk_content in enumerate(chunks, 1):
        chunk_title = doc_title if total == 1 else f"{doc_title} (Part {i}/{total})"
        doc = await create_document(
            agent_id=agent_id,
            title=chunk_title,
            content=chunk_content,
            source=doc_source,
            doc_metadata={
                "original_filename": filename,
                "chunk_index": i,
                "total_chunks": total,
            },
            db=db,
        )
        created_docs.append(doc)

    await db.commit()
    for doc in created_docs:
        await db.refresh(doc)

    return DocumentUploadResponse(
        chunks=[DocumentResponse.model_validate(d) for d in created_docs],
        total_chunks=total,
        original_filename=filename,
        extracted_chars=len(full_text),
    )

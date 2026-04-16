import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    source: str | None = Field(None, max_length=500)
    doc_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    content: str | None = Field(None, min_length=1)
    source: str | None = None
    doc_metadata: dict[str, Any] | None = None


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    title: str
    content: str
    source: str | None
    doc_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int


class DocumentUploadResponse(BaseModel):
    """Returned after a file upload that may produce multiple chunks."""
    chunks: list[DocumentResponse]
    total_chunks: int
    original_filename: str
    extracted_chars: int


class DocumentSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    max_results: int = Field(3, ge=1, le=20)


class DocumentSearchResponse(BaseModel):
    results: list[DocumentResponse]
    total: int
    query: str

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class MessageCreate(BaseModel):
    message: str
    metadata: dict[str, Any] = {}


class StepSummary(BaseModel):
    step: int
    tool: str
    args: dict[str, Any]
    result: str


class MessageResponse(BaseModel):
    reply: str
    steps: list[StepSummary]
    run_id: uuid.UUID


class HistoryMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str | None
    tool_name: str | None
    tool_args: dict[str, Any] | None
    tool_result: str | None
    step_index: int
    run_id: uuid.UUID | None
    timestamp: datetime


class HistoryResponse(BaseModel):
    session_id: uuid.UUID
    messages: list[HistoryMessage]

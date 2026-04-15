import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    instructions: str = ""
    model: str = "anthropic/claude-sonnet-4-5"
    tools_config: dict[str, Any] = Field(default_factory=dict)
    sandbox_config: dict[str, Any] = Field(default_factory=dict)
    safety_policy: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    tools_config: dict[str, Any] | None = None
    sandbox_config: dict[str, Any] | None = None
    safety_policy: dict[str, Any] | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    instructions: str
    model: str
    tools_config: dict[str, Any]
    sandbox_config: dict[str, Any]
    safety_policy: dict[str, Any]
    version: int
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    total: int
    limit: int
    offset: int

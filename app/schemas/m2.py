import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1)


class MemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    value_data: str
    created_at: datetime
    updated_at: datetime


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1)
    content_md: str = Field(..., min_length=1)


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    description: str
    content_md: str
    created_at: datetime
    updated_at: datetime


class CustomToolCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255,
                      pattern=r"^[a-z][a-z0-9_]*$",
                      description="Function name (snake_case, starts with lowercase letter)")
    description: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1, description="Python code defining a function named `name`")


class CustomToolResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    description: str
    code: str
    created_at: datetime
    updated_at: datetime

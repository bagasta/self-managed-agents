"""Schemas for the opt-in workforce task control plane."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal["draft", "open", "in_progress", "blocked", "completed", "cancelled"]
StepStatus = Literal["pending", "queued", "in_progress", "blocked", "completed", "cancelled"]
Priority = Literal["low", "normal", "high", "urgent"]


class WorkforceTaskCreate(BaseModel):
    workspace_id: str | None = Field(None, min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=20_000)
    priority: Priority = "normal"
    context: dict[str, Any] = Field(default_factory=dict)
    assigned_agent_id: uuid.UUID | None = None
    customer_session_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)


class WorkforceTaskUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=20_000)
    status: TaskStatus | None = None
    priority: Priority | None = None
    context: dict[str, Any] | None = None
    result_summary: str | None = Field(None, max_length=20_000)
    assigned_agent_id: uuid.UUID | None = None


class WorkforceTaskStepCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    instructions: str | None = Field(None, max_length=20_000)
    input_context: dict[str, Any] = Field(default_factory=dict)
    assigned_agent_id: uuid.UUID | None = None
    delegated_by_agent_id: uuid.UUID | None = None


class WorkforceTaskStepUpdate(BaseModel):
    status: StepStatus | None = None
    output_summary: str | None = Field(None, max_length=20_000)
    run_id: uuid.UUID | None = None


class WorkforceTaskDispatch(BaseModel):
    manager_agent_id: uuid.UUID


class WorkforceTaskStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    sequence: int
    title: str
    instructions: str | None
    status: str
    input_context: dict[str, Any]
    output_summary: str | None
    delegated_by_agent_id: uuid.UUID | None
    assigned_agent_id: uuid.UUID | None
    run_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WorkforceTaskEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    step_id: uuid.UUID | None
    run_id: uuid.UUID | None
    actor_agent_id: uuid.UUID | None
    event_type: str
    visibility: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_", serialization_alias="metadata")
    created_at: datetime


class WorkforceTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: str
    title: str
    description: str | None
    status: str
    priority: str
    context: dict[str, Any]
    result_summary: str | None
    idempotency_key: str | None
    parent_task_id: uuid.UUID | None
    assigned_agent_id: uuid.UUID | None
    customer_session_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[WorkforceTaskStepResponse] = Field(default_factory=list)
    events: list[WorkforceTaskEventResponse] = Field(default_factory=list)


class WorkforceTaskListResponse(BaseModel):
    items: list[WorkforceTaskResponse]
    total: int
    limit: int
    offset: int


class WorkforceRosterMember(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    capabilities: list[str]
    model: str
    active_task_count: int


class WorkforceRosterResponse(BaseModel):
    workspace_id: str
    items: list[WorkforceRosterMember]

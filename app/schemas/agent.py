import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_TOKEN_QUOTA = 4_000_000
_DEFAULT_PERIOD_DAYS = 30


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    instructions: str = ""
    model: str = "anthropic/claude-sonnet-4-6"
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    tools_config: dict[str, Any] = Field(default_factory=dict)
    sandbox_config: dict[str, Any] = Field(default_factory=dict)
    safety_policy: dict[str, Any] = Field(default_factory=dict)
    escalation_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Konfigurasi human operator untuk eskalasi. "
            "Contoh: {\"channel_type\": \"whatsapp\", \"operator_phone\": \"+62811xxx\"}"
        ),
    )
    token_quota: int = Field(
        _DEFAULT_TOKEN_QUOTA,
        ge=1,
        description="Max tokens allowed per period (default 4,000,000)",
    )
    quota_period_days: int = Field(
        _DEFAULT_PERIOD_DAYS,
        ge=1,
        description="Subscription period in days before renewal is required (default 30)",
    )


class AgentUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    tools_config: dict[str, Any] | None = None
    sandbox_config: dict[str, Any] | None = None
    safety_policy: dict[str, Any] | None = None
    escalation_config: dict[str, Any] | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    instructions: str
    model: str
    temperature: float
    tools_config: dict[str, Any]
    sandbox_config: dict[str, Any]
    safety_policy: dict[str, Any]
    escalation_config: dict[str, Any]
    version: int
    is_deleted: bool

    # subscription / quota
    api_key: str
    token_quota: int
    tokens_used: int
    active_until: datetime
    quota_period_days: int

    created_at: datetime
    updated_at: datetime


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    total: int
    limit: int
    offset: int


class AgentRenewResponse(BaseModel):
    id: uuid.UUID
    api_key: str
    tokens_used: int
    token_quota: int
    active_until: datetime
    quota_period_days: int
    message: str

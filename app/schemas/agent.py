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
    operator_ids: list[str] = Field(
        default_factory=list,
        description="Daftar external_user_id (nomor WA/JID) yang punya akses operator.",
    )
    allowed_senders: list[str] | None = Field(
        None,
        description=(
            "Allowlist nomor pengirim. null = semua diizinkan. "
            "Isi dengan list nomor (e.g. ['628111', '628222']) untuk membatasi."
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
    channel_type: str | None = Field(
        None,
        description="Channel to connect at creation time. Supported: 'whatsapp'",
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
    operator_ids: list[str] | None = None
    allowed_senders: list[str] | None = None


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
    operator_ids: list[str]
    allowed_senders: list[str] | None
    version: int
    is_deleted: bool

    # subscription / quota
    api_key: str
    token_quota: int
    tokens_used: int
    active_until: datetime
    quota_period_days: int

    # whatsapp channel
    wa_device_id: str | None
    channel_type: str | None

    created_at: datetime
    updated_at: datetime

    # Populated only on create/reconnect response when channel_type == "whatsapp"
    qr_image: str | None = None


class AgentListResponse(BaseModel):
    items: list[AgentResponse]
    total: int
    limit: int
    offset: int


class AgentWhatsAppQRResponse(BaseModel):
    device_id: str
    qr_image: str  # base64 PNG
    status: str    # "waiting_qr" | "connected"


class AgentWhatsAppStatusResponse(BaseModel):
    device_id: str
    status: str
    phone_number: str


class AgentRenewResponse(BaseModel):
    id: uuid.UUID
    api_key: str
    tokens_used: int
    token_quota: int
    active_until: datetime
    quota_period_days: int
    message: str

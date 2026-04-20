import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    external_user_id: str | None = None
    metadata: dict[str, Any] = {}
    channel_type: str | None = Field(
        None,
        description="Channel asal user: whatsapp | telegram | slack | webhook | in-app",
    )
    channel_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Konfigurasi channel. Contoh WhatsApp: "
            "{\"user_phone\": \"+62812xxx\", \"api_key\": \"WABA_TOKEN\"}"
        ),
    )


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    external_user_id: str | None
    workspace_dir: str | None
    channel_type: str | None
    escalation_active: bool
    created_at: datetime
    updated_at: datetime

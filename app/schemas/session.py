import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SessionCreate(BaseModel):
    external_user_id: str | None = None
    metadata: dict[str, Any] = {}


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    external_user_id: str | None
    workspace_dir: str | None
    created_at: datetime
    updated_at: datetime

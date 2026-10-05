import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class UserApiKeyCreate(BaseModel):
    label: str | None = None
    owner_external_id: str | None = Field(None, min_length=1, max_length=64)


class UserApiKeyCreateResponse(BaseModel):
    id: uuid.UUID
    key: str
    label: str | None
    expires_at: datetime
    revoked: bool
    created_at: datetime


class UserApiKeyStatusResponse(BaseModel):
    id: uuid.UUID
    label: str | None
    expires_at: datetime
    revoked: bool
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserApiKeyRenewResponse(BaseModel):
    id: uuid.UUID
    label: str | None
    expires_at: datetime
    revoked: bool
    created_at: datetime
    message: str

    model_config = {"from_attributes": True}

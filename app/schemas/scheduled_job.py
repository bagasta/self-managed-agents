import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScheduledJobCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=255, description="Nama unik job dalam sesi ini")
    payload: str = Field(..., description="Pesan yang dikirim ke agent saat job berjalan")
    cron_expr: str | None = Field(
        None,
        description="Cron expression untuk recurring job. Contoh: '0 9 * * 1-5' (setiap hari kerja jam 9)",
    )
    run_once_at: datetime | None = Field(
        None,
        description="Jalankan sekali pada waktu ini (ISO 8601 UTC). Gunakan ini ATAU cron_expr.",
    )

    @model_validator(mode="after")
    def check_schedule(self) -> "ScheduledJobCreate":
        if not self.cron_expr and not self.run_once_at:
            raise ValueError("Harus mengisi salah satu: cron_expr atau run_once_at")
        if self.cron_expr and self.run_once_at:
            raise ValueError("Isi hanya salah satu: cron_expr atau run_once_at, tidak keduanya")
        return self


class ScheduledJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    label: str
    cron_expr: str | None
    run_once_at: datetime | None
    payload: str
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime

from __future__ import annotations

from datetime import datetime

from app.schemas.common import CamelModel


class StageView(CamelModel):
    stage: str
    status: str
    error: str | None = None


class JobStatusResponse(CamelModel):
    job_id: str
    job_type: str
    status: str
    current_stage: str | None
    error_code: str | None
    error_message: str | None
    track_id: str | None = None
    pipeline: list[StageView]
    created_at: datetime
    updated_at: datetime

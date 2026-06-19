from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import JobNotFound
from app.deps import get_current_user, get_sessionmaker
from app.domain.models.user import User
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.tracks import TracksRepository
from app.domain.schemas.jobs import JobStatusResponse, StageView

router = APIRouter(tags=["Задачи"])


@router.get("/jobs/{job_id}", response_model=JobStatusResponse, summary="Статус задачи")
async def get_job(
    job_id: UUID,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> JobStatusResponse:
    async with sessionmaker() as session:
        repo = JobsRepository(session)
        job = await repo.get_by_id(job_id)
        if job is None or job.user_id != current.id:
            raise JobNotFound()
        stages = await repo.list_stage_events(job_id)
        track = await TracksRepository(session).get_by_job_id(job_id)
        pipeline = [
            StageView(stage=s.stage.value, status=s.status, error=s.error)
            for s in sorted(stages, key=lambda s: s.created_at)
        ]
        return JobStatusResponse(
            job_id=str(job.id),
            job_type=job.job_type.value,
            status=job.status.value,
            current_stage=job.current_stage.value if job.current_stage else None,
            error_code=job.error_code,
            error_message=job.error_message,
            track_id=str(track.id) if track else None,
            pipeline=pipeline,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

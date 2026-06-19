from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select

from app.api.errors import JobNotFound
from app.deps import get_current_user, get_generation_service, get_sessionmaker
from app.domain.enums import AssetKind, JobType
from app.domain.models.asset import Asset
from app.domain.models.user import User
from app.domain.repositories.jobs import JobsRepository
from app.domain.schemas.songs import JobAcceptedResponse
from app.domain.schemas.videos import CreateVideoRequest, VideoResultResponse
from app.domain.services.generation_service import GenerationService

router = APIRouter(prefix="/videos", tags=["Видео"])


@router.post(
    "", response_model=JobAcceptedResponse, status_code=202, summary="Создать AI music video"
)
async def create_video(
    body: CreateVideoRequest,
    current: Annotated[User, Depends(get_current_user)],
    generation: Annotated[GenerationService, Depends(get_generation_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAcceptedResponse:
    payload = body.model_dump(exclude_none=True, by_alias=False)
    result = await generation.create_job(
        user_id=current.id,
        job_type=JobType.video,
        payload=payload,
        client_idempotency_key=idempotency_key,
    )
    return JobAcceptedResponse(
        job_id=str(result.job_id), status="queued", deduplicated=result.deduplicated
    )


@router.get("/{job_id}", response_model=VideoResultResponse, summary="Результат видео")
async def get_video(
    job_id: UUID,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> VideoResultResponse:
    async with sessionmaker() as session:
        job = await JobsRepository(session).get_by_id(job_id)
        if job is None or job.user_id != current.id or job.job_type != JobType.video:
            raise JobNotFound()
        stmt = (
            select(Asset)
            .where(Asset.user_id == current.id, Asset.kind == AssetKind.video)
            .where(Asset.meta["job_id"].astext == str(job_id))
            .limit(1)
        )
        asset = (await session.execute(stmt)).scalars().first()
        return VideoResultResponse(
            job_id=str(job.id),
            status=job.status.value,
            video_url=asset.url if asset else None,
            created_at=asset.created_at if asset else None,
        )

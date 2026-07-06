from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import JobNotFound, TrackNotFound, ValidationFailed
from app.deps import get_current_user, get_generation_service, get_sessionmaker
from app.domain.enums import AssetKind, JobType, VideoMode
from app.domain.models.asset import Asset
from app.domain.models.user import User
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.tracks import TracksRepository
from app.domain.schemas.songs import JobAcceptedResponse
from app.domain.schemas.videos import CreateVideoRequest, VideoResultResponse
from app.domain.services.generation_service import GenerationService

router = APIRouter(prefix="/videos", tags=["Видео"])


async def _resolve_track_audio(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: UUID,
    track_id: UUID,
    variant_id: UUID | None,
    mode: VideoMode,
) -> tuple[str, str | None]:
    """Резолвит «My track» → (audio_url, lyrics|None).

    Проверяет владение треком. Лирика (для lyrics_video) берётся из задачи-песни:
    track.job_id → Job.input_payload['_lyrics'] (song пишет её именно туда, не в Track.meta).
    """
    async with sessionmaker() as session:
        tracks = TracksRepository(session)
        track = await tracks.get(track_id)
        if track is None or track.user_id != user_id:
            raise TrackNotFound()
        variants = await tracks.list_variants(track_id)
        if not variants:
            raise ValidationFailed(
                details={"reason": "track_has_no_audio"}, http_status=422
            )
        variant = variants[0]
        if variant_id is not None:
            match = next((v for v in variants if v.id == variant_id), None)
            if match is None:
                raise ValidationFailed(
                    details={"reason": "unknown_variant"}, http_status=422
                )
            variant = match
        audio_url = variant.audio_url

        lyrics: str | None = None
        if mode == VideoMode.lyrics_video and track.job_id is not None:
            job = await JobsRepository(session).get_by_id(track.job_id)
            if job is not None:
                raw = (job.input_payload or {}).get("_lyrics")
                if isinstance(raw, str) and raw.strip():
                    lyrics = raw
    return audio_url, lyrics


@router.post(
    "", response_model=JobAcceptedResponse, status_code=202, summary="Создать AI music video"
)
async def create_video(
    body: CreateVideoRequest,
    current: Annotated[User, Depends(get_current_user)],
    generation: Annotated[GenerationService, Depends(get_generation_service)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> JobAcceptedResponse:
    payload = body.model_dump(mode="json", exclude_none=True, by_alias=False)

    # «My track»: до create_job резолвим audio_url (и лирику для lyrics_video).
    if body.track_id is not None:
        audio_url, lyrics = await _resolve_track_audio(
            sessionmaker=sessionmaker,
            user_id=current.id,
            track_id=body.track_id,
            variant_id=body.variant_id,
            mode=body.mode,
        )
        payload["audio_url"] = audio_url
        if lyrics and not payload.get("lyrics"):
            payload["lyrics"] = lyrics

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
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
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
        meta = (asset.meta or {}) if asset else {}
        return VideoResultResponse(
            job_id=str(job.id),
            status=job.status.value,
            video_url=asset.url if asset else None,
            mode=meta.get("mode"),
            aspect_ratio=meta.get("aspect_ratio"),
            style=meta.get("style"),
            created_at=asset.created_at if asset else None,
        )

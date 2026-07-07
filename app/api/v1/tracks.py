from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import TrackNotFound
from app.deps import get_current_user, get_sessionmaker
from app.domain.models.user import User
from app.domain.repositories.tracks import TracksRepository
from app.domain.schemas.tracks import TrackResponse, TrackVariantView

router = APIRouter(prefix="/tracks", tags=["Треки"])


@router.get("/{track_id}", response_model=TrackResponse, summary="Получить трек")
async def get_track(
    track_id: UUID,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> TrackResponse:
    async with sessionmaker() as session:
        repo = TracksRepository(session)
        track = await repo.get(track_id)
        if track is None or track.user_id != current.id:
            raise TrackNotFound()
        variants = await repo.list_variants(track_id)
        return TrackResponse(
            id=str(track.id),
            kind=track.kind.value,
            title=track.title,
            prompt=(track.meta or {}).get("prompt"),
            job_id=str(track.job_id) if track.job_id else None,
            created_at=track.created_at,
            variants=[
                TrackVariantView(
                    id=str(v.id),
                    variant_index=v.variant_index,
                    audio_url=v.audio_url,
                    duration_seconds=float(v.duration_seconds),
                    stems=v.stems,
                )
                for v in variants
            ],
        )

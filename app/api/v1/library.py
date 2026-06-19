from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.deps import get_current_user, get_sessionmaker
from app.domain.enums import AssetKind
from app.domain.models.asset import Asset
from app.domain.models.user import User
from app.domain.repositories.tracks import TracksRepository
from app.domain.repositories.voice import VoiceRepository
from app.schemas.common import CamelModel


class LibraryItem(CamelModel):
    id: str
    type: str
    title: str | None = None
    url: str | None = None
    created_at: str


class LibraryResponse(CamelModel):
    tracks: list[LibraryItem]
    videos: list[LibraryItem]
    voices: list[LibraryItem]


router = APIRouter(tags=["Библиотека"])


@router.get("/library", response_model=LibraryResponse, summary="Медиатека пользователя")
async def library(
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> LibraryResponse:
    async with sessionmaker() as session:
        tracks = await TracksRepository(session).list_for_user(user_id=current.id, limit=100)
        video_stmt = (
            select(Asset)
            .where(Asset.user_id == current.id, Asset.kind == AssetKind.video)
            .order_by(Asset.created_at.desc())
            .limit(100)
        )
        videos = list((await session.execute(video_stmt)).scalars().all())
        voices = await VoiceRepository(session).list_profiles(current.id)
        return LibraryResponse(
            tracks=[
                LibraryItem(
                    id=str(t.id), type=t.kind.value, title=t.title,
                    created_at=t.created_at.isoformat(),
                )
                for t in tracks
            ],
            videos=[
                LibraryItem(
                    id=str(v.id), type="video", url=v.url,
                    created_at=v.created_at.isoformat(),
                )
                for v in videos
            ],
            voices=[
                LibraryItem(
                    id=str(p.id), type="voice", title=p.name,
                    created_at=p.created_at.isoformat(),
                )
                for p in voices
            ],
        )

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import TrackKind
from app.domain.models.track import Track, TrackVariant


class TracksRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_job_id(
        self, job_id: UUID, *, include_deleted: bool = False
    ) -> Track | None:
        """Резолв трека по job_id.

        `include_deleted=False` (user-read, jobs.py) — фильтрует soft-deleted:
        не отдаём наружу ссылку на удалённый трек. `include_deleted=True`
        (finalize-дедуп cover.py/song.py) — НЕ фильтрует: иначе после soft-delete
        финализатор решит, что трека нет, и создаст дубликат по тому же job_id.
        """
        stmt = select(Track).where(Track.job_id == job_id)
        if not include_deleted:
            stmt = stmt.where(Track.deleted_at.is_(None))
        return (await self._session.execute(stmt)).scalars().first()

    async def get(self, track_id: UUID) -> Track | None:
        """User-read/ownership резолв: soft-deleted трек трактуется как отсутствующий."""
        stmt = select(Track).where(
            Track.id == track_id, Track.deleted_at.is_(None)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def create(
        self,
        *,
        user_id: UUID,
        job_id: UUID | None,
        kind: TrackKind,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Track:
        track = Track(
            user_id=user_id, job_id=job_id, kind=kind, title=title, meta=meta
        )
        self._session.add(track)
        await self._session.flush()
        return track

    async def add_variant(
        self,
        *,
        track_id: UUID,
        variant_index: int,
        audio_url: str,
        duration_seconds: float | Decimal,
        stems: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> TrackVariant:
        variant = TrackVariant(
            track_id=track_id,
            variant_index=variant_index,
            audio_url=audio_url,
            duration_seconds=Decimal(str(duration_seconds)),
            stems=stems,
            meta=meta,
        )
        self._session.add(variant)
        await self._session.flush()
        return variant

    async def list_variants(self, track_id: UUID) -> list[TrackVariant]:
        stmt = (
            select(TrackVariant)
            .where(TrackVariant.track_id == track_id)
            .order_by(TrackVariant.variant_index)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_for_user(
        self, *, user_id: UUID, limit: int = 50, offset: int = 0,
        kind: TrackKind | None = None,
    ) -> list[Track]:
        stmt = select(Track).where(
            Track.user_id == user_id, Track.deleted_at.is_(None)
        )
        if kind is not None:
            stmt = stmt.where(Track.kind == kind)
        stmt = stmt.order_by(Track.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

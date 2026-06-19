from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.lyrics_draft import LyricsDraft


class LyricsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        content: str,
        prompt: str | None,
        language: str,
        genre: str | None,
        mood: str | None,
        source: str = "generated",
        meta: dict[str, Any] | None = None,
    ) -> LyricsDraft:
        draft = LyricsDraft(
            user_id=user_id,
            content=content,
            prompt=prompt,
            language=language,
            genre=genre,
            mood=mood,
            source=source,
            meta=meta,
        )
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def get(self, draft_id: UUID) -> LyricsDraft | None:
        return await self._session.get(LyricsDraft, draft_id)

    async def list_for_user(
        self, *, user_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[LyricsDraft]:
        stmt = (
            select(LyricsDraft)
            .where(LyricsDraft.user_id == user_id)
            .order_by(LyricsDraft.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

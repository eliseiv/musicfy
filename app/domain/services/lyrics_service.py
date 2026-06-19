from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import LyricsDraftNotFound
from app.domain.models.lyrics_draft import LyricsDraft
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.lyrics import LyricsRepository


class LyricsService:
    """Синхронная генерация и редактирование текста песни (LLM-пайплайн)."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], fal: FalProvider
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fal = fal

    async def generate(
        self,
        *,
        user_id: UUID,
        prompt: str,
        language: str = "en",
        genre: str | None = None,
        mood: str | None = None,
    ) -> LyricsDraft:
        content = await self._fal.generate_lyrics(
            prompt=prompt, language=language, genre=genre, mood=mood
        )
        async with self._sessionmaker() as session:
            async with session.begin():
                draft = await LyricsRepository(session).create(
                    user_id=user_id,
                    content=content,
                    prompt=prompt,
                    language=language,
                    genre=genre,
                    mood=mood,
                    source="generated",
                )
            session.expunge(draft)
        return draft

    async def get(self, *, user_id: UUID, draft_id: UUID) -> LyricsDraft:
        async with self._sessionmaker() as session:
            draft = await LyricsRepository(session).get(draft_id)
            if draft is None or draft.user_id != user_id:
                raise LyricsDraftNotFound()
            session.expunge(draft)
            return draft

    async def update_content(
        self, *, user_id: UUID, draft_id: UUID, content: str
    ) -> LyricsDraft:
        async with self._sessionmaker() as session:
            async with session.begin():
                repo = LyricsRepository(session)
                draft = await repo.get(draft_id)
                if draft is None or draft.user_id != user_id:
                    raise LyricsDraftNotFound()
                draft.content = content
                draft.source = "edited"
                await session.flush()
            session.expunge(draft)
            return draft

    async def list_for_user(self, *, user_id: UUID, limit: int, offset: int):
        async with self._sessionmaker() as session:
            return await LyricsRepository(session).list_for_user(
                user_id=user_id, limit=limit, offset=offset
            )

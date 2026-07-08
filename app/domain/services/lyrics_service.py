from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import LyricsDraftNotFound
from app.domain.models.lyrics_draft import LyricsDraft
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.lyrics import LyricsRepository
from app.domain.services.credits import CoinWalletService


class LyricsService:
    """Синхронная генерация и редактирование текста песни (LLM-пайплайн)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fal: FalProvider,
        credits: CoinWalletService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fal = fal
        self._credits = credits

    async def generate(
        self,
        *,
        user_id: UUID,
        prompt: str,
        language: str = "en",
        genre: str | None = None,
        mood: str | None = None,
        idempotency_key: str | None = None,
    ) -> LyricsDraft:
        op_id = idempotency_key or str(uuid4())
        # Списание ДО fal (fail-fast 402 без траты на провайдера); саговый refund
        # при любом сбое после charge (ADR-010).
        charged = await self._credits.charge(
            user_id=user_id,
            job_type="lyrics",
            idempotency_key=f"charge:lyrics:{op_id}",
            ref_type="lyrics",
            ref_id=str(op_id),
        )
        try:
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
        except Exception:
            await self._credits.refund(
                user_id=user_id,
                units=charged,
                idempotency_key=f"refund:lyrics:{op_id}",
                ref_type="lyrics",
                ref_id=str(op_id),
            )
            raise
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

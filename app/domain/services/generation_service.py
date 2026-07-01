from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import FalProviderError, FalTimeout
from app.config import Settings
from app.domain.enums import JobType
from app.domain.repositories.jobs import JobsRepository
from app.domain.services.pipelines.runner import PipelineRunner

logger = logging.getLogger(__name__)


@dataclass
class CreateJobResult:
    job_id: UUID
    deduplicated: bool


class GenerationService:
    """Фасад создания задач генерации. Резерв кредитов подключается в Фазе 3."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        runner: PipelineRunner,
        settings: Settings,
        *,
        credits=None,
        moderation=None,
        analytics=None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runner = runner
        self._settings = settings
        self._credits = credits
        self._moderation = moderation
        self._analytics = analytics

    def _provider_model(self, job_type: JobType) -> str | None:
        return {
            JobType.song: self._settings.FAL_SONG_MODEL,
            JobType.cover: self._settings.FAL_DEMUCS_MODEL,
            JobType.video: self._settings.FAL_VIDEO_MODEL,
            JobType.voice_clone: self._settings.FAL_VOICE_CLONE_MODEL,
        }.get(job_type)

    async def create_job(
        self,
        *,
        user_id: UUID,
        job_type: JobType,
        payload: dict[str, Any],
        store_stems: bool = False,
        client_idempotency_key: str | None = None,
    ) -> CreateJobResult:
        # Модерация текстового ввода (prompt / lyrics / title).
        if self._moderation is not None:
            reason = self._moderation.screen_text(
                payload.get("prompt"),
                payload.get("lyrics_prompt"),
                payload.get("custom_lyrics"),
                payload.get("title"),
            )
            if reason is not None:
                from app.api.errors import ModerationBlocked
                from app.domain.enums import ModerationStatus

                await self._moderation.record_case(
                    user_id=user_id,
                    status=ModerationStatus.blocked,
                    reason=reason,
                    excerpt=payload.get("prompt") or payload.get("lyrics_prompt"),
                )
                raise ModerationBlocked(details={"reason": reason})

        # Идемпотентность по ключу клиента.
        if client_idempotency_key:
            async with self._sessionmaker() as session:
                existing = await JobsRepository(session).get_by_idempotency_key(
                    user_id=user_id, key=client_idempotency_key
                )
                if existing is not None:
                    return CreateJobResult(job_id=existing.id, deduplicated=True)

        # Резерв монет по цене типа генерации из прайс-листа (CoinWalletService).
        # Бесплатный тип (цена 0) → reserve вернёт 0, резерв не выполняется.
        reserved = 0
        if self._credits is not None:
            reserved = await self._credits.reserve(
                user_id=user_id, job_type=job_type.value
            )

        async with self._sessionmaker() as session:
            async with session.begin():
                repo = JobsRepository(session)
                job = await repo.create(
                    user_id=user_id,
                    job_type=job_type,
                    input_payload=payload,
                    provider_model=self._provider_model(job_type),
                    credit_category=None,
                    reserved_credits=reserved,
                    store_stems=store_stems,
                    client_idempotency_key=client_idempotency_key,
                )
                job_id = job.id
            session.expunge(job)

        try:
            await self._runner.start(job)
        except (FalProviderError, FalTimeout) as exc:
            logger.warning("pipeline start failed for job=%s: %s", job_id, exc)
            if self._credits is not None and reserved:
                try:
                    await self._credits.release(job=job)
                except Exception:
                    logger.exception("release after start failure failed")
            async with self._sessionmaker() as session:
                async with session.begin():
                    await JobsRepository(session).mark_failed(
                        job_id=job_id,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
            raise

        if self._analytics is not None:
            await self._analytics.track(
                user_id=user_id,
                name="generation_started",
                properties={"job_type": job_type.value, "job_id": str(job_id)},
            )

        return CreateJobResult(job_id=job_id, deduplicated=False)

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain.enums import JobStage, JobType
from app.domain.models.job import Job
from app.domain.providers.fal.base import FalProvider
from app.domain.services.pipelines.base import BasePipeline, CreditGate
from app.domain.services.pipelines.cover import CoverPipeline
from app.domain.services.pipelines.song import SongPipeline
from app.domain.services.pipelines.video import VideoPipeline
from app.domain.services.pipelines.voice_clone import VoiceClonePipeline

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Диспетчер пайплайнов по job_type. Используется webhook'ом и poller'ом."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fal: FalProvider,
        settings: Settings,
        *,
        credits: CreditGate | None = None,
        notifier=None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._pipelines: dict[JobType, BasePipeline] = {
            JobType.song: SongPipeline(sessionmaker, fal, settings, credits=credits),
            JobType.cover: CoverPipeline(sessionmaker, fal, settings, credits=credits),
            JobType.voice_clone: VoiceClonePipeline(
                sessionmaker, fal, settings, credits=credits
            ),
            JobType.video: VideoPipeline(
                sessionmaker, fal, settings, credits=credits, notifier=notifier
            ),
        }

    def for_type(self, job_type: JobType) -> BasePipeline | None:
        return self._pipelines.get(job_type)

    async def start(self, job: Job) -> None:
        pipeline = self.for_type(job.job_type)
        if pipeline is None:
            raise RuntimeError(f"No pipeline for job_type={job.job_type}")
        await pipeline.start(job)

    async def _load(self, job_id: UUID) -> Job | None:
        async with self._sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is not None:
                session.expunge(job)
            return job

    async def advance(
        self,
        *,
        job_id: UUID,
        completed_stage: JobStage,
        media_url: str | None,
        duration_seconds: float | None,
        stems: dict[str, Any] | None,
        event_id: str,
    ) -> None:
        job = await self._load(job_id)
        if job is None:
            logger.warning("advance: job %s not found", job_id)
            return
        pipeline = self.for_type(job.job_type)
        if pipeline is None:
            return
        await pipeline.advance(
            job=job,
            completed_stage=completed_stage,
            media_url=media_url,
            duration_seconds=duration_seconds,
            stems=stems,
            event_id=event_id,
        )

    async def fail(
        self, *, job_id: UUID, failed_stage: JobStage, error_code: str, error_message: str
    ) -> None:
        job = await self._load(job_id)
        if job is None:
            return
        pipeline = self.for_type(job.job_type)
        if pipeline is None:
            return
        await pipeline.fail(
            job=job,
            failed_stage=failed_stage,
            error_code=error_code,
            error_message=error_message,
        )

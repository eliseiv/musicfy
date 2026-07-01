from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import FalProviderError, FalTimeout
from app.config import Settings
from app.domain.enums import AssetKind, JobStage, JobStatus
from app.domain.models.job import Job
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.assets import AssetsRepository
from app.domain.repositories.jobs import JobsRepository
from app.domain.services.pipelines.base import BasePipeline, CreditGate

logger = logging.getLogger(__name__)


class VideoPipeline(BasePipeline):
    """AI music video (Avatar Performance): kling lipsync audio→video. Самая долгая."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fal: FalProvider,
        settings: Settings,
        *,
        credits: CreditGate | None = None,
        notifier=None,
    ) -> None:
        super().__init__(sessionmaker, fal, settings, credits=credits)
        self._notifier = notifier

    async def start(self, job: Job) -> None:
        await self._mark_status(job.id, JobStatus.running)
        await self._record_stage(job.id, JobStage.prepare_prompt, "succeeded")
        payload = job.input_payload or {}
        audio_url = payload.get("audio_url")
        source_video_url = payload.get("source_video_url")
        if not audio_url or not source_video_url:
            await self._record_stage(
                job.id, JobStage.source_prep, "failed", error="missing audio/source video"
            )
            await self._mark_failed(
                job.id, "INVALID_INPUT", "audio_url and source_video_url required"
            )
            return
        await self._record_stage(job.id, JobStage.source_prep, "succeeded")
        await self._record_stage(job.id, JobStage.lipsync, "running")
        try:
            submit = await self._fal.submit_lipsync_video(
                video_url=source_video_url,
                audio_url=audio_url,
                webhook_url=self._webhook_url(),
                idempotency_key=f"{job.id}:lipsync",
            )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(job.id, JobStage.lipsync, "failed", error=str(exc))
            raise
        await self._set_current_stage(
            job.id, JobStage.lipsync, submit.request_id, submit=submit
        )

    async def advance(
        self,
        *,
        job: Job,
        completed_stage: JobStage,
        media_url: str | None,
        duration_seconds: float | None,
        stems: dict[str, Any] | None,
        event_id: str,
    ) -> None:
        if job.current_stage is not None and job.current_stage != completed_stage:
            return
        await self._record_stage(job.id, completed_stage, "succeeded")
        if not media_url:
            await self._record_stage(
                job.id, JobStage.finalize, "failed", error="no video url"
            )
            await self._mark_failed(job.id, "PROVIDER_FAILED", "no video url")
            return
        await self._finalize(job.id, media_url, duration_seconds)

    async def _finalize(
        self, job_id: UUID, video_url: str, duration: float | None
    ) -> None:
        await self._mark_status(job_id, JobStatus.post_processing)
        await self._record_stage(job_id, JobStage.upload_cdn, "succeeded")
        captured = await self._capture_credits(job_id)
        await self._record_stage(job_id, JobStage.finalize, "running")
        user_id = None
        async with self._sessionmaker() as session:
            async with session.begin():
                job = await session.get(Job, job_id)
                if job is None:
                    return
                user_id = job.user_id
                await AssetsRepository(session).create(
                    user_id=job.user_id,
                    kind=AssetKind.video,
                    url=video_url,
                    duration_seconds=duration,
                    meta={"job_id": str(job_id), "mode": (job.input_payload or {}).get("mode")},
                )
                await JobsRepository(session).mark_succeeded(
                    job_id=job_id, captured_credits=captured
                )
        await self._record_stage(job_id, JobStage.finalize, "succeeded")
        if self._notifier is not None and user_id is not None:
            try:
                await self._notifier.notify_job_done(
                    user_id=user_id, title="Your music video is ready 🎬",
                    body="Tap to watch your AI music video.",
                    payload={"job_id": str(job_id), "type": "video"},
                )
            except Exception:
                logger.exception("push notify failed for job=%s", job_id)

    async def fail(
        self, *, job: Job, failed_stage: JobStage, error_code: str, error_message: str
    ) -> None:
        await self._record_stage(job.id, failed_stage, "failed", error=error_message)
        await self._mark_failed(job.id, error_code, error_message)

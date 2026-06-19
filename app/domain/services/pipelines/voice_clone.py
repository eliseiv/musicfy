from __future__ import annotations

import logging
from uuid import UUID

from app.api.errors import FalProviderError, FalTimeout
from app.domain.enums import JobStage, JobStatus, VoiceProfileStatus
from app.domain.models.job import Job
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.voice import VoiceRepository
from app.domain.services.pipelines.base import BasePipeline

logger = logging.getLogger(__name__)


class VoiceClonePipeline(BasePipeline):
    """Клонирование голоса (sync). consent_check → quality_check → voice_clone → finalize."""

    async def start(self, job: Job) -> None:
        await self._mark_status(job.id, JobStatus.running)
        payload = job.input_payload or {}
        profile_id = payload.get("voice_profile_id")
        consent_id = payload.get("consent_id")
        sample_url = payload.get("sample_asset_url")

        # consent_check
        async with self._sessionmaker() as session:
            repo = VoiceRepository(session)
            consent = await repo.get_consent(UUID(consent_id)) if consent_id else None
            ok = (
                consent is not None
                and consent.user_id == job.user_id
                and consent.accepted
            )
        if not ok:
            await self._record_stage(
                job.id, JobStage.consent_check, "failed", error="consent missing"
            )
            await self._fail_profile(profile_id)
            await self._mark_failed(job.id, "CONSENT_REQUIRED", "voice consent required")
            return
        await self._record_stage(job.id, JobStage.consent_check, "succeeded")

        # quality_check
        if not sample_url:
            await self._record_stage(
                job.id, JobStage.quality_check, "failed", error="no sample"
            )
            await self._fail_profile(profile_id)
            await self._mark_failed(job.id, "INVALID_INPUT", "no voice sample")
            return
        await self._record_stage(job.id, JobStage.quality_check, "succeeded")

        # voice_clone (sync)
        await self._record_stage(job.id, JobStage.voice_clone, "running")
        try:
            voice_id = await self._fal.voice_clone(audio_url=sample_url)
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(job.id, JobStage.voice_clone, "failed", error=str(exc))
            await self._fail_profile(profile_id)
            await self._mark_failed(job.id, exc.code, exc.message)
            return
        await self._record_stage(job.id, JobStage.voice_clone, "succeeded")

        # finalize
        await self._mark_status(job.id, JobStatus.post_processing)
        async with self._sessionmaker() as session:
            async with session.begin():
                if profile_id:
                    await VoiceRepository(session).update_profile(
                        profile_id=UUID(profile_id),
                        provider_voice_id=voice_id,
                        status=VoiceProfileStatus.ready,
                    )
                await JobsRepository(session).mark_succeeded(job_id=job.id, captured_credits=0)
        await self._record_stage(job.id, JobStage.finalize, "succeeded")

    async def _fail_profile(self, profile_id: str | None) -> None:
        if not profile_id:
            return
        async with self._sessionmaker() as session:
            async with session.begin():
                await VoiceRepository(session).update_profile(
                    profile_id=UUID(profile_id),
                    provider_voice_id=None,
                    status=VoiceProfileStatus.failed,
                )

    async def advance(self, **kwargs) -> None:  # voice_clone — sync, без webhook
        return

    async def fail(
        self, *, job: Job, failed_stage: JobStage, error_code: str, error_message: str
    ) -> None:
        await self._mark_failed(job.id, error_code, error_message)

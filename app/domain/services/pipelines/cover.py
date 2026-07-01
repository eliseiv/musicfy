from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.api.errors import FalProviderError, FalTimeout
from app.domain.enums import JobStage, JobStatus, TrackKind
from app.domain.models.job import Job
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.tracks import TracksRepository
from app.domain.services.audio_duration import probe_duration_seconds
from app.domain.services.pipelines.base import BasePipeline

logger = logging.getLogger(__name__)

ASYNC_STAGES = (JobStage.stem_separation, JobStage.voice_conversion)


class CoverPipeline(BasePipeline):
    """AI cover: demucs (stem separation) → voice-changer → mix с инструменталом."""

    async def start(self, job: Job) -> None:
        await self._mark_status(job.id, JobStatus.running)
        await self._record_stage(job.id, JobStage.prepare_prompt, "succeeded")
        payload = job.input_payload or {}
        source_url = payload.get("source_audio_url")
        if not source_url:
            await self._record_stage(
                job.id, JobStage.stem_separation, "failed", error="no source_audio_url"
            )
            await self._mark_failed(job.id, "INVALID_INPUT", "no source_audio_url")
            return
        await self._submit_stems(job, source_url)

    async def _submit_stems(self, job: Job, source_url: str) -> None:
        await self._record_stage(job.id, JobStage.stem_separation, "running")
        try:
            submit = await self._fal.submit_stem_separation(
                audio_url=source_url,
                webhook_url=self._webhook_url(),
                idempotency_key=f"{job.id}:stems",
            )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(
                job.id, JobStage.stem_separation, "failed", error=str(exc)
            )
            raise
        await self._set_current_stage(
            job.id, JobStage.stem_separation, submit.request_id, submit=submit
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
        runtime = await self._get_runtime(job.id)
        runtime[completed_stage.value] = {
            "media_url": media_url, "duration_seconds": duration_seconds,
            "stems": stems, "event_id": event_id,
        }
        await self._persist_runtime(job.id, runtime)

        if completed_stage == JobStage.stem_separation:
            vocal_url = _pick_stem(stems, ("vocals", "vocal"))
            instrumental = _pick_stem(
                stems, ("accompaniment", "instrumental", "other", "backing")
            )
            await self._update_payload(
                job.id,
                {"_vocal_stem": vocal_url, "_instrumental_stem": instrumental},
            )
            if not vocal_url:
                await self._record_stage(job.id, JobStage.voice_conversion, "skipped")
                await self._finalize(job.id, await self._get_runtime(job.id))
                return
            await self._submit_voice_conversion(job, vocal_url)
            return
        # voice_conversion completed → finalize
        await self._finalize(job.id, await self._get_runtime(job.id))

    async def _submit_voice_conversion(self, job: Job, vocal_url: str) -> None:
        target_voice = (job.input_payload or {}).get("target_voice") or "default"
        await self._record_stage(job.id, JobStage.voice_conversion, "running")
        try:
            submit = await self._fal.submit_voice_changer(
                audio_url=vocal_url,
                target_voice=target_voice,
                webhook_url=self._webhook_url(),
                idempotency_key=f"{job.id}:vc",
            )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(
                job.id, JobStage.voice_conversion, "failed", error=str(exc)
            )
            await self._finalize(job.id, await self._get_runtime(job.id))
            return
        await self._set_current_stage(
            job.id, JobStage.voice_conversion, submit.request_id, submit=submit
        )

    async def fail(
        self, *, job: Job, failed_stage: JobStage, error_code: str, error_message: str
    ) -> None:
        await self._record_stage(job.id, failed_stage, "failed", error=error_message)
        await self._mark_failed(job.id, error_code, error_message)

    async def _finalize(self, job_id: UUID, runtime: dict[str, Any]) -> None:
        await self._mark_status(job_id, JobStatus.post_processing)
        existing = await self._list_recorded_stages(job_id)
        for st in ASYNC_STAGES:
            if st not in existing:
                await self._record_stage(job_id, st, "skipped")

        job = await self.load_job(job_id)
        payload = (job.input_payload if job else {}) or {}
        converted_vocal = runtime.get(JobStage.voice_conversion.value, {}).get("media_url")
        instrumental = payload.get("_instrumental_stem") or payload.get("source_audio_url")

        audio_url, duration, stems = await self._mix(
            job_id, converted_vocal, instrumental, runtime
        )
        await self._record_stage(job_id, JobStage.upload_cdn, "succeeded")

        if not audio_url:
            await self._record_stage(
                job_id, JobStage.finalize, "failed", error="no cover audio"
            )
            await self._mark_failed(job_id, "PROVIDER_FAILED", "no cover audio")
            return
        if not duration or duration <= 0:
            probed = await probe_duration_seconds(audio_url)
            if probed:
                duration = probed

        captured = await self._capture_credits(job_id)
        await self._record_stage(job_id, JobStage.finalize, "running")
        async with self._sessionmaker() as session:
            async with session.begin():
                job = await session.get(Job, job_id)
                if job is None:
                    return
                tracks = TracksRepository(session)
                if await tracks.get_by_job_id(job_id) is None:
                    track = await tracks.create(
                        user_id=job.user_id, job_id=job_id, kind=TrackKind.cover,
                        title=(job.input_payload or {}).get("title"),
                        meta={"runtime": runtime, "quality_flag": "mvp_review"},
                    )
                    await tracks.add_variant(
                        track_id=track.id, variant_index=0, audio_url=audio_url,
                        duration_seconds=duration or 0,
                        stems=stems if job.store_stems else None,
                    )
                await JobsRepository(session).mark_succeeded(
                    job_id=job_id, captured_credits=captured
                )
        await self._record_stage(job_id, JobStage.finalize, "succeeded")

    async def _mix(
        self, job_id: UUID, vocal_url: str | None, instrumental_url: str | None,
        runtime: dict[str, Any],
    ) -> tuple[str | None, float | None, dict[str, Any] | None]:
        from app.domain.services.audio_mixer import ffmpeg_available, mix_music_and_vocal

        if not vocal_url:
            return None, None, None
        if not instrumental_url or not ffmpeg_available():
            # Деградированный путь: отдаём преобразованный вокал, инструментал в stems.
            await self._record_stage(
                job_id, JobStage.mix_master, "skipped", error="no instrumental or ffmpeg"
            )
            return vocal_url, None, {"vocal": vocal_url, "instrumental": instrumental_url}
        await self._record_stage(job_id, JobStage.mix_master, "running")
        try:
            mix_url, mix_duration = await mix_music_and_vocal(
                music_url=instrumental_url, vocal_url=vocal_url,
                upload_fn=self._fal.upload_to_storage,
            )
        except Exception as exc:
            await self._record_stage(
                job_id, JobStage.mix_master, "failed", error=str(exc)[:200]
            )
            return vocal_url, None, {"vocal": vocal_url, "instrumental": instrumental_url}
        if not mix_url:
            await self._record_stage(job_id, JobStage.mix_master, "failed")
            return vocal_url, None, {"vocal": vocal_url, "instrumental": instrumental_url}
        await self._record_stage(job_id, JobStage.mix_master, "succeeded")
        return mix_url, mix_duration, {"vocal": vocal_url, "instrumental": instrumental_url}


def _pick_stem(stems: dict[str, Any] | None, keys: tuple[str, ...]) -> str | None:
    if not stems:
        return None
    for k in keys:
        v = stems.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    return None

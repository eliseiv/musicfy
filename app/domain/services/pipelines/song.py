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
from app.domain.services.track_title import derive_track_title

logger = logging.getLogger(__name__)

FALLBACK_MUSIC_MODEL = "fal-ai/stable-audio"
FALLBACK_VOCAL_MODEL = "fal-ai/ace-step"

ASYNC_STAGES = (JobStage.music_generation, JobStage.vocal_tts)


class SongPipeline(BasePipeline):
    """text-to-song / lyrics-to-song на fal-ai/minimax-music/v2.6."""

    async def start(self, job: Job) -> None:
        await self._mark_status(job.id, JobStatus.running)
        await self._record_stage(job.id, JobStage.prepare_prompt, "succeeded")
        lyrics = await self._resolve_lyrics(job)
        await self._submit_music(job, lyrics=lyrics)

    async def _resolve_lyrics(self, job: Job) -> str | None:
        payload = job.input_payload or {}
        custom = payload.get("custom_lyrics")
        if custom:
            await self._record_stage(job.id, JobStage.lyrics, "skipped")
            await self._update_payload(job.id, {"_lyrics": custom})
            return custom
        theme = payload.get("lyrics_prompt")
        if not theme:
            await self._record_stage(job.id, JobStage.lyrics, "skipped")
            return None
        await self._record_stage(job.id, JobStage.lyrics, "running")
        try:
            lyrics = await self._fal.generate_lyrics(
                prompt=theme,
                language=payload.get("language") or "en",
                genre=payload.get("genre"),
                mood=payload.get("mood"),
            )
        except (FalProviderError, FalTimeout) as exc:
            logger.warning("lyrics gen failed for job=%s: %s", job.id, exc)
            await self._record_stage(job.id, JobStage.lyrics, "failed", error=str(exc))
            return None
        if not lyrics or len(lyrics) < 3:
            await self._record_stage(job.id, JobStage.lyrics, "skipped")
            return None
        await self._update_payload(job.id, {"_lyrics": lyrics})
        await self._record_stage(job.id, JobStage.lyrics, "succeeded")
        return lyrics

    async def _submit_music(self, job: Job, *, lyrics: str | None) -> None:
        await self._record_stage(job.id, JobStage.music_generation, "running")
        prompt = _compose_song_prompt(job.input_payload)
        payload = job.input_payload or {}
        try:
            submit = await self._fal.submit_song(
                prompt=prompt,
                duration_seconds=payload.get("desired_duration_seconds"),
                lyrics=lyrics,
                reference_audio_url=payload.get("reference_audio_url"),
                webhook_url=self._webhook_url(),
                idempotency_key=f"{job.id}:music",
            )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(
                job.id, JobStage.music_generation, "failed", error=str(exc)
            )
            raise
        await self._set_current_stage(
            job.id, JobStage.music_generation, submit.request_id, submit=submit
        )

    # ---- webhook / poller ----

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
            logger.warning(
                "Song webhook stage=%s but current=%s (job=%s); ignoring",
                completed_stage.value, job.current_stage.value, job.id,
            )
            return

        await self._record_stage(job.id, completed_stage, "succeeded")
        runtime = await self._get_runtime(job.id)
        runtime[completed_stage.value] = {
            "media_url": media_url,
            "duration_seconds": duration_seconds,
            "stems": stems,
            "event_id": event_id,
        }
        await self._persist_runtime(job.id, runtime)

        payload = job.input_payload or {}
        has_voice = bool(payload.get("voice_url"))
        lyrics = payload.get("_lyrics")
        if completed_stage == JobStage.music_generation and has_voice and lyrics:
            await self._submit_vocal_tts(job, lyrics=lyrics)
            return
        await self._finalize(job.id, runtime)

    async def _submit_vocal_tts(self, job: Job, *, lyrics: str) -> None:
        payload = job.input_payload or {}
        voice_url = payload.get("voice_url")
        await self._record_stage(job.id, JobStage.vocal_tts, "running")
        cloned = payload.get("_cloned_voice_id")
        if not cloned:
            try:
                cloned = await self._fal.voice_clone(audio_url=voice_url)
            except (FalProviderError, FalTimeout) as exc:
                logger.warning("voice_clone failed job=%s: %s", job.id, exc)
                await self._record_stage(
                    job.id, JobStage.vocal_tts, "failed", error=f"voice_clone: {exc}"
                )
                await self._finalize(job.id, await self._get_runtime(job.id))
                return
            await self._update_payload(job.id, {"_cloned_voice_id": cloned})
        try:
            submit = await self._fal.submit_speech(
                text=lyrics,
                voice_id=cloned,
                webhook_url=self._webhook_url(),
                idempotency_key=f"{job.id}:tts",
            )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(
                job.id, JobStage.vocal_tts, "failed", error=str(exc)
            )
            await self._finalize(job.id, await self._get_runtime(job.id))
            return
        await self._set_current_stage(
            job.id, JobStage.vocal_tts, submit.request_id, submit=submit
        )

    async def fail(
        self, *, job: Job, failed_stage: JobStage, error_code: str, error_message: str
    ) -> None:
        if failed_stage == JobStage.music_generation and error_code == "PROVIDER_FAILED":
            if await self._try_music_fallback(job, error_message):
                return
        await self._record_stage(
            job.id, failed_stage, "failed", error=error_message
        )
        await self._mark_failed(job.id, error_code, error_message)

    async def _try_music_fallback(self, job: Job, prev_error: str) -> bool:
        payload = dict(job.input_payload or {})
        if payload.get("_music_fallback_used"):
            return False
        if job.provider_model in (FALLBACK_MUSIC_MODEL, FALLBACK_VOCAL_MODEL):
            return False
        lyrics = payload.get("_lyrics")
        try:
            if lyrics:
                submit = await self._fal.submit_ace_step_vocal(
                    tags=_fallback_tags(payload),
                    lyrics=lyrics,
                    webhook_url=self._webhook_url(),
                    idempotency_key=f"{job.id}:music-fb-vocal",
                )
                new_model = FALLBACK_VOCAL_MODEL
            else:
                seconds = max(10, min(47, int(payload.get("desired_duration_seconds") or 30)))
                submit = await self._fal.submit_stable_audio(
                    prompt=_compose_song_prompt(payload),
                    seconds_total=seconds,
                    webhook_url=self._webhook_url(),
                    idempotency_key=f"{job.id}:music-fb",
                )
                new_model = FALLBACK_MUSIC_MODEL
        except (FalProviderError, FalTimeout) as exc:
            logger.warning("music fallback submit failed job=%s: %s", job.id, exc)
            return False
        await self._update_payload(
            job.id,
            {"_music_fallback_used": True, "_music_fallback_prev_error": prev_error[:200]},
        )
        await self._set_current_stage(
            job.id, JobStage.music_generation, submit.request_id,
            provider_model=new_model, submit=submit,
        )
        await self._record_stage(job.id, JobStage.music_generation, "running")
        return True

    async def _finalize(self, job_id: UUID, runtime: dict[str, Any]) -> None:
        await self._mark_status(job_id, JobStatus.post_processing)
        existing = await self._list_recorded_stages(job_id)
        for st in ASYNC_STAGES:
            if st not in existing:
                await self._record_stage(job_id, st, "skipped")

        runtime = await self._maybe_mix(job_id, runtime)
        await self._record_stage(job_id, JobStage.upload_cdn, "succeeded")

        audio_url, duration, stems = _pick_output(runtime)
        if not audio_url:
            await self._record_stage(
                job_id, JobStage.finalize, "failed", error="no audio after pipeline"
            )
            await self._mark_failed(job_id, "PROVIDER_FAILED", "no audio after pipeline")
            return

        if not duration or duration <= 0:
            probed = await probe_duration_seconds(audio_url)
            if probed and probed > 0:
                duration = probed

        captured = await self._capture_credits(job_id)

        await self._record_stage(job_id, JobStage.finalize, "running")
        async with self._sessionmaker() as session:
            async with session.begin():
                job = await session.get(Job, job_id)
                if job is None:
                    return
                tracks = TracksRepository(session)
                existing_track = await tracks.get_by_job_id(job_id)
                if existing_track is None:
                    track = await tracks.create(
                        user_id=job.user_id,
                        job_id=job_id,
                        kind=TrackKind.song,
                        title=derive_track_title("song", job.input_payload),
                        meta={
                            "runtime": runtime,
                            "prompt": (job.input_payload or {}).get("prompt"),
                        },
                    )
                    await tracks.add_variant(
                        track_id=track.id,
                        variant_index=0,
                        audio_url=audio_url,
                        duration_seconds=duration or 0,
                        stems=stems if job.store_stems else None,
                    )
                await JobsRepository(session).mark_succeeded(
                    job_id=job_id, captured_credits=captured
                )
        await self._record_stage(job_id, JobStage.finalize, "succeeded")

    async def _maybe_mix(self, job_id: UUID, runtime: dict[str, Any]) -> dict[str, Any]:
        from app.domain.services.audio_mixer import ffmpeg_available, mix_music_and_vocal

        music_url = runtime.get(JobStage.music_generation.value, {}).get("media_url")
        vocal_url = runtime.get(JobStage.vocal_tts.value, {}).get("media_url")
        if not music_url or not vocal_url:
            await self._record_stage(job_id, JobStage.mix_master, "skipped")
            return runtime
        if not ffmpeg_available():
            await self._record_stage(
                job_id, JobStage.mix_master, "skipped", error="ffmpeg not in PATH"
            )
            mg = runtime[JobStage.music_generation.value]
            stems = dict(mg.get("stems") or {})
            stems["vocal"] = vocal_url
            mg["stems"] = stems
            return runtime
        await self._record_stage(job_id, JobStage.mix_master, "running")
        try:
            mix_url, mix_duration = await mix_music_and_vocal(
                music_url=music_url, vocal_url=vocal_url,
                upload_fn=self._fal.upload_to_storage,
            )
        except Exception as exc:
            await self._record_stage(
                job_id, JobStage.mix_master, "failed", error=str(exc)[:200]
            )
            return runtime
        if not mix_url:
            await self._record_stage(job_id, JobStage.mix_master, "failed")
            return runtime
        runtime["mix_master"] = {
            "media_url": mix_url,
            "duration_seconds": mix_duration,
            "stems": {"vocal": vocal_url, "music": music_url},
        }
        await self._record_stage(job_id, JobStage.mix_master, "succeeded")
        return runtime


def _compose_song_prompt(payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    parts: list[str] = []
    if payload.get("prompt"):
        parts.append(str(payload["prompt"]))
    if payload.get("genre"):
        parts.append(f"genre: {payload['genre']}")
    if payload.get("mood"):
        parts.append(f"mood: {payload['mood']}")
    if payload.get("tempo_bpm"):
        parts.append(f"{payload['tempo_bpm']} BPM")
    if payload.get("vocal_type"):
        parts.append(f"vocals: {payload['vocal_type']}")
    if payload.get("language"):
        parts.append(f"language: {payload['language']}")
    if payload.get("negative_hints"):
        parts.append(f"avoid: {payload['negative_hints']}")
    return " | ".join(parts) or "an original song"


def _fallback_tags(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("genre", "mood", "vocal_type"):
        if payload.get(key):
            parts.append(str(payload[key]))
    if payload.get("tempo_bpm"):
        parts.append(f"{payload['tempo_bpm']} bpm")
    parts.append("vocal")
    return ", ".join(parts)


def _pick_output(
    runtime: dict[str, Any],
) -> tuple[str | None, float | None, dict[str, Any] | None]:
    for key in ("mix_master", JobStage.music_generation.value):
        r = runtime.get(key)
        if r and r.get("media_url"):
            return r.get("media_url"), r.get("duration_seconds"), r.get("stems")
    return None, None, None

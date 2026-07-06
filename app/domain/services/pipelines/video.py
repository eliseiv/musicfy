from __future__ import annotations

import logging
import random
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import FalProviderError, FalTimeout
from app.config import Settings
from app.domain.enums import (
    AssetKind,
    JobStage,
    JobStatus,
    PresetKind,
    VideoAspect,
    VideoMode,
)
from app.domain.models.job import Job
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.assets import AssetsRepository
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.presets import PresetsRepository
from app.domain.services.moderation_service import ModerationService
from app.domain.services.pipelines.base import BasePipeline, CreditGate

logger = logging.getLogger(__name__)

# Async-стадии video (fal submit → webhook/poller → advance). mux_audio / lyrics_render —
# синхронные ffmpeg-стадии, здесь не перечисляются.
ASYNC_STAGES = (JobStage.lipsync, JobStage.visual_gen)

DEFAULT_VISUAL_PROMPT = "an abstract cinematic music visualizer, flowing colors, high detail"
DEFAULT_LYRICS_BG_PROMPT = (
    "a soft abstract animated background for a lyrics video, gentle flowing gradients, "
    "subtle bokeh, cinematic ambient mood, no text"
)


class VideoPipeline(BasePipeline):
    """AI music video на 3 режима (ADR-007): avatar / visual_clip / lyrics_video."""

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

    # ---- dispatch ----

    async def start(self, job: Job) -> None:
        await self._mark_status(job.id, JobStatus.running)
        payload = job.input_payload or {}
        try:
            mode = VideoMode(payload.get("mode") or VideoMode.avatar_performance.value)
        except ValueError:
            await self._record_stage(
                job.id, JobStage.prepare_prompt, "failed", error="unknown mode"
            )
            await self._mark_failed(job.id, "INVALID_INPUT", "unknown video mode")
            return

        audio_url = payload.get("audio_url")
        if not audio_url:
            await self._record_stage(
                job.id, JobStage.source_prep, "failed", error="missing audio"
            )
            await self._mark_failed(job.id, "INVALID_INPUT", "audio_url required")
            return

        if mode == VideoMode.avatar_performance:
            await self._start_avatar(job, payload, audio_url)
        elif mode == VideoMode.visual_clip:
            await self._start_visual(job, payload, audio_url)
        else:
            await self._start_lyrics(job, payload, audio_url)

    # ---- avatar_performance ----

    async def _start_avatar(
        self, job: Job, payload: dict[str, Any], audio_url: str
    ) -> None:
        await self._record_stage(job.id, JobStage.prepare_prompt, "succeeded")
        source_video_url = payload.get("source_video_url")
        reference_image_url = payload.get("reference_image_url")
        if not source_video_url and not reference_image_url:
            await self._record_stage(
                job.id, JobStage.source_prep, "failed", error="missing avatar source"
            )
            await self._mark_failed(
                job.id, "INVALID_INPUT", "source_video_url or reference_image_url required"
            )
            return
        await self._record_stage(job.id, JobStage.source_prep, "succeeded")
        await self._record_stage(job.id, JobStage.lipsync, "running")
        try:
            if source_video_url:
                submit = await self._fal.submit_lipsync_video(
                    video_url=source_video_url,
                    audio_url=audio_url,
                    webhook_url=self._webhook_url(),
                    idempotency_key=f"{job.id}:lipsync",
                )
            elif reference_image_url:
                submit = await self._fal.submit_avatar_image_video(
                    image_url=reference_image_url,
                    audio_url=audio_url,
                    webhook_url=self._webhook_url(),
                    idempotency_key=f"{job.id}:avatar",
                )
            else:  # недостижимо (проверено выше), защитно
                await self._mark_failed(
                    job.id, "INVALID_INPUT", "avatar source missing"
                )
                return
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(job.id, JobStage.lipsync, "failed", error=str(exc))
            raise
        # Обе ветки переиспользуют JobStage.lipsync как current_stage (совпадает с
        # completed_stage в advance → идемпотентный guard работает без отдельной стадии).
        await self._set_current_stage(
            job.id, JobStage.lipsync, submit.request_id, submit=submit
        )

    # ---- visual_clip ----

    async def _start_visual(
        self, job: Job, payload: dict[str, Any], audio_url: str
    ) -> None:
        prompt = await self._resolve_prompt(job, payload)
        await self._record_stage(job.id, JobStage.prepare_prompt, "succeeded")
        reference_image_url = payload.get("reference_image_url")
        aspect_ratio = payload.get("aspect_ratio") or VideoAspect.vertical_9_16.value
        await self._record_stage(job.id, JobStage.visual_gen, "running")
        try:
            if reference_image_url:
                submit = await self._fal.submit_image_to_video(
                    prompt=prompt,
                    image_url=reference_image_url,
                    aspect_ratio=aspect_ratio,
                    webhook_url=self._webhook_url(),
                    idempotency_key=f"{job.id}:visual",
                )
            else:
                submit = await self._fal.submit_text_to_video(
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    webhook_url=self._webhook_url(),
                    idempotency_key=f"{job.id}:visual",
                )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(job.id, JobStage.visual_gen, "failed", error=str(exc))
            raise
        await self._set_current_stage(
            job.id, JobStage.visual_gen, submit.request_id, submit=submit
        )

    # ---- lyrics_video (АСИНХРОННЫЙ, симметричен visual_clip — ADR-007 §3/§3a) ----

    async def _start_lyrics(
        self, job: Job, payload: dict[str, Any], audio_url: str
    ) -> None:
        # ИНВАРИАНТ §3a: start() выполняет ТОЛЬКО быстрый fal-submit t2v-фона. Никакого
        # скачивания аудио / probe / ffmpeg / upload на request-пути — вся тяжёлая работа
        # (бёрн-ин лирики + мукс) переносится в advance() (фон webhook/поллера).
        prompt = self._lyrics_bg_prompt(payload)
        await self._record_stage(job.id, JobStage.prepare_prompt, "succeeded")
        # Дешёвая подготовка: лирика уже в payload['lyrics'] (резолвится в create_video).
        # Пустая лирика → деградация (quality_flag) в advance, а не отказ запроса (ADR §3).
        await self._record_stage(job.id, JobStage.source_prep, "succeeded")
        aspect_ratio = payload.get("aspect_ratio") or VideoAspect.vertical_9_16.value

        await self._record_stage(job.id, JobStage.visual_gen, "running")
        try:
            # lyrics-bg модель (FAL_VIDEO_LYRICS_BG_MODEL), а НЕ visual t2v: держит инвариант
            # job.provider_model == реально вызванной модели (ADR-007 §3a, config.py:148).
            submit = await self._fal.submit_lyrics_background(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                webhook_url=self._webhook_url(),
                idempotency_key=f"{job.id}:lyrics_bg",
            )
        except (FalProviderError, FalTimeout) as exc:
            await self._record_stage(
                job.id, JobStage.visual_gen, "failed", error=str(exc)
            )
            raise
        await self._set_current_stage(
            job.id, JobStage.visual_gen, submit.request_id, submit=submit
        )

    def _lyrics_bg_prompt(self, payload: dict[str, Any]) -> str:
        """Промпт t2v-фона под бёрн-ин лирики: явный prompt или дефолт по стилю (дёшево)."""
        prompt = (payload.get("prompt") or "").strip()
        if prompt:
            return prompt
        style = (payload.get("style") or "").strip()
        if style:
            return f"{DEFAULT_LYRICS_BG_PROMPT}, {style} style"
        return DEFAULT_LYRICS_BG_PROMPT

    # ---- surprise-me / prompt ----

    async def _resolve_prompt(self, job: Job, payload: dict[str, Any]) -> str:
        prompt = (payload.get("prompt") or "").strip()
        if prompt:
            return prompt
        if payload.get("surprise_me"):
            picked = await self._pick_surprise_prompt()
            if picked:
                await self._update_payload(job.id, {"prompt": picked})
                return picked
        return DEFAULT_VISUAL_PROMPT

    async def _pick_surprise_prompt(self) -> str | None:
        async with self._sessionmaker() as session:
            presets = await PresetsRepository(session).list_by_kind(PresetKind.prompt)
        candidates = [p.prompt_text for p in presets if p.prompt_text]
        random.shuffle(candidates)
        for text in candidates:
            # Серверный подбор всё равно проходит модерацию (ADR §5 / Surprise me).
            if ModerationService.screen_text(text) is None:
                return text
        return None

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
            return
        await self._record_stage(job.id, completed_stage, "succeeded")
        if not media_url:
            await self._record_stage(
                job.id, JobStage.finalize, "failed", error="no video url"
            )
            await self._mark_failed(job.id, "PROVIDER_FAILED", "no video url")
            return

        if completed_stage == JobStage.visual_gen:
            # visual_clip и lyrics_video завершают async-стадию на visual_gen; различаем
            # их по input_payload['mode'] (ADR-007 §3): lyrics → render + мукс, visual → мукс.
            payload = job.input_payload or {}
            audio_url = payload.get("audio_url")
            if (payload.get("mode") or "") == VideoMode.lyrics_video.value:
                # lyrics_video: media_url = сгенерированный fal t2v-фон. Тяжёлый ffmpeg
                # (бёрн-ин лирики + мукс аудио) исполняется здесь, вне request-пути (§3a).
                await self._render_lyrics(
                    job.id, media_url, audio_url, payload, duration_seconds
                )
                return
            # visual_clip: вшиваем аудио трека в короткий клип (loop под длину аудио).
            final_url, final_duration, quality_flag = await self._mux_audio(
                job.id, media_url, audio_url
            )
            await self._finalize(
                job.id,
                final_url,
                final_duration if final_duration is not None else duration_seconds,
                quality_flag=quality_flag,
            )
            return

        # avatar (lipsync/avatar-image): аудио уже вшито моделью — мукс не нужен.
        await self._finalize(job.id, media_url, duration_seconds)

    async def _render_lyrics(
        self,
        job_id: UUID,
        background_url: str,
        audio_url: str | None,
        payload: dict[str, Any],
        fallback_duration: float | None,
    ) -> None:
        """lyrics_render в advance(): бёрн-ин лирики поверх fal-фона + мукс аудио + finalize.

        Деградация ffmpeg-сбоя для lyrics — `_mark_failed` (release кредитов), НЕ финализация
        битым видео (ADR-007 §3, отличие от visual_clip mux → немое видео).
        """
        from app.domain.services.video_mux import (
            ffmpeg_available,
            render_lyrics_video,
            split_lyric_lines,
        )

        if not audio_url:
            await self._record_stage(
                job_id, JobStage.lyrics_render, "failed", error="missing audio"
            )
            await self._mark_failed(job_id, "INVALID_INPUT", "audio_url required")
            return
        if not ffmpeg_available():
            await self._record_stage(
                job_id, JobStage.lyrics_render, "skipped", error="ffmpeg not in PATH"
            )
            await self._mark_failed(
                job_id, "PROVIDER_FAILED", "lyrics render requires ffmpeg"
            )
            return

        lines = split_lyric_lines(payload.get("lyrics"))
        aspect_ratio = payload.get("aspect_ratio") or VideoAspect.vertical_9_16.value
        await self._record_stage(job_id, JobStage.lyrics_render, "running")
        try:
            # Один probe аудио — внутри render_lyrics_video (без дублирования в start, §3).
            video_url, out_duration = await render_lyrics_video(
                background_url=background_url,
                audio_url=audio_url,
                lyrics_lines=lines,
                aspect_ratio=aspect_ratio,
                upload_fn=self._fal.upload_to_storage,
            )
        except Exception as exc:
            await self._record_stage(
                job_id, JobStage.lyrics_render, "failed", error=str(exc)[:200]
            )
            await self._mark_failed(job_id, "PROVIDER_FAILED", "lyrics render failed")
            return
        if not video_url:
            await self._record_stage(job_id, JobStage.lyrics_render, "failed")
            await self._mark_failed(
                job_id, "PROVIDER_FAILED", "lyrics render produced no video"
            )
            return
        await self._record_stage(job_id, JobStage.lyrics_render, "succeeded")
        quality_flag = "lyrics_even_timing" if lines else "lyrics_no_text"
        await self._finalize(
            job_id, video_url, out_duration or fallback_duration, quality_flag=quality_flag
        )

    async def _mux_audio(
        self, job_id: UUID, video_url: str, audio_url: str | None
    ) -> tuple[str, float | None, str | None]:
        from app.domain.services.video_mux import ffmpeg_available, mux_audio_into_video

        if not audio_url:
            return video_url, None, "muted_no_audio"
        if not ffmpeg_available():
            await self._record_stage(
                job_id, JobStage.mux_audio, "skipped", error="ffmpeg not in PATH"
            )
            return video_url, None, "muted_no_ffmpeg"
        await self._record_stage(job_id, JobStage.mux_audio, "running")
        try:
            muxed_url, muxed_duration = await mux_audio_into_video(
                video_url=video_url,
                audio_url=audio_url,
                upload_fn=self._fal.upload_to_storage,
            )
        except Exception as exc:
            await self._record_stage(
                job_id, JobStage.mux_audio, "failed", error=str(exc)[:200]
            )
            return video_url, None, "mux_failed"
        if not muxed_url:
            await self._record_stage(job_id, JobStage.mux_audio, "failed")
            return video_url, None, "mux_failed"
        await self._record_stage(job_id, JobStage.mux_audio, "succeeded")
        return muxed_url, muxed_duration, None

    # ---- finalize ----

    async def _finalize(
        self,
        job_id: UUID,
        video_url: str,
        duration: float | None,
        *,
        quality_flag: str | None = None,
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
                payload = job.input_payload or {}
                meta: dict[str, Any] = {
                    "job_id": str(job_id),
                    "mode": payload.get("mode"),
                    "style": payload.get("style"),
                    "aspect_ratio": payload.get("aspect_ratio"),
                }
                if quality_flag:
                    meta["quality_flag"] = quality_flag
                await AssetsRepository(session).create(
                    user_id=job.user_id,
                    kind=AssetKind.video,
                    url=video_url,
                    duration_seconds=duration,
                    meta=meta,
                )
                await JobsRepository(session).mark_succeeded(
                    job_id=job_id, captured_credits=captured
                )
        await self._record_stage(job_id, JobStage.finalize, "succeeded")
        if self._notifier is not None and user_id is not None:
            try:
                await self._notifier.notify_job_done(
                    user_id=user_id,
                    title="Your music video is ready 🎬",
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

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import FalProviderError, FalTimeout, ValidationFailed
from app.config import Settings
from app.domain.enums import JobType, VideoMode, VoiceProfileStatus
from app.domain.repositories.jobs import JobsRepository
from app.domain.repositories.preset_voices import PresetVoicesRepository
from app.domain.repositories.voice import VoiceRepository
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

    async def _resolve_target_voice(
        self, *, user_id: UUID, payload: dict[str, Any]
    ) -> None:
        """Валидирует и резолвит `cover.targetVoice` (JobType.cover).

        Валиден, если значение: пустое | UUID собственного ready-профиля |
        активный `preset_voices.key`. Для собственного клона переписывает
        `payload["target_voice"]` на `profile.provider_voice_id`, для ключа пресета —
        на `preset.provider_voice` (в fal всегда уходит провайдерский voice-id, не
        внутренний UUID и не публичный key). Иначе → 422 unknown_voice.
        """
        raw = payload.get("target_voice")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return
        value = str(raw).strip()

        # Случай: UUID собственного ready-профиля (My Clones) — переписываем
        # payload на провайдерский voice-id (ADR-006 §3): в fal должен уйти
        # реальный ElevenLabs voice-id, а не внутренний DB-UUID профиля.
        profile_uuid: UUID | None = None
        try:
            profile_uuid = UUID(value)
        except (ValueError, AttributeError):
            profile_uuid = None
        if profile_uuid is not None:
            async with self._sessionmaker() as session:
                profile = await VoiceRepository(session).get_profile(profile_uuid)
            if (
                profile is not None
                and profile.user_id == user_id
                and profile.status == VoiceProfileStatus.ready
            ):
                # Defensive: ready-профиль без provider_voice_id слать в fal нельзя.
                if not (
                    profile.provider_voice_id and profile.provider_voice_id.strip()
                ):
                    raise ValidationFailed(
                        details={"reason": "unknown_voice"}, http_status=422
                    )
                payload["target_voice"] = profile.provider_voice_id
                return
            raise ValidationFailed(
                details={"reason": "unknown_voice"}, http_status=422
            )

        # Случай: ключ активного пресета — переписываем на provider_voice.
        async with self._sessionmaker() as session:
            preset = await PresetVoicesRepository(session).get_by_key(value)
        if preset is not None and preset.active:
            payload["target_voice"] = preset.provider_voice
            return
        raise ValidationFailed(details={"reason": "unknown_voice"}, http_status=422)

    def _provider_model(
        self, job_type: JobType, payload: dict[str, Any] | None = None
    ) -> str | None:
        # video — модель зависит от режима (+наличия source/reference), поэтому payload-aware.
        # Инвариант: значение обязано совпадать с реально вызванной моделью (его опрашивает
        # FalPoller). lyrics_video (ADR-007 §3a, async) → FAL_VIDEO_LYRICS_BG_MODEL: поллер
        # ведёт t2v-фон, рендер лирики выполняется в advance().
        if job_type == JobType.video:
            data = payload or {}
            raw_mode = data.get("mode") or VideoMode.avatar_performance.value
            try:
                mode = VideoMode(raw_mode)
            except ValueError:
                mode = VideoMode.avatar_performance
            return self._settings.video_provider_model(
                mode,
                has_reference_image=bool(data.get("reference_image_url")),
                has_source_video=bool(data.get("source_video_url")),
            )
        return {
            JobType.song: self._settings.FAL_SONG_MODEL,
            JobType.cover: self._settings.FAL_DEMUCS_MODEL,
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
                payload.get("lyrics"),
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

        # Валидация + резолв целевого голоса кавера (до резерва и сохранения job).
        if job_type == JobType.cover:
            await self._resolve_target_voice(user_id=user_id, payload=payload)

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
                    provider_model=self._provider_model(job_type, payload),
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

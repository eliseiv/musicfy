from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import ValidationFailed, VoiceProfileNotFound
from app.deps import get_current_user, get_generation_service, get_sessionmaker
from app.domain.enums import JobType, VoiceConsentKind
from app.domain.models.user import User
from app.domain.repositories.voice import VoiceRepository
from app.domain.schemas.voices import (
    ConsentRequest,
    ConsentResponse,
    CreateVoiceRequest,
    RenameVoiceRequest,
    VoiceProfileResponse,
)
from app.domain.services.generation_service import GenerationService

router = APIRouter(prefix="/voices", tags=["Голоса"])


@router.post("/consent", response_model=ConsentResponse, summary="Записать согласие на голос")
async def create_consent(
    body: ConsentRequest,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> ConsentResponse:
    try:
        kind = VoiceConsentKind(body.kind)
    except ValueError as exc:
        raise ValidationFailed(details={"field": "kind"}) from exc
    if not body.accepted:
        raise ValidationFailed(details={"reason": "consent_not_accepted"})
    async with sessionmaker() as session:
        async with session.begin():
            consent = await VoiceRepository(session).create_consent(
                user_id=current.id, kind=kind, accepted=body.accepted, statement=body.statement
            )
        return ConsentResponse(
            id=str(consent.id), kind=consent.kind.value, accepted=consent.accepted
        )


@router.post("", response_model=VoiceProfileResponse, status_code=201, summary="Клонировать голос")
async def create_voice(
    body: CreateVoiceRequest,
    current: Annotated[User, Depends(get_current_user)],
    generation: Annotated[GenerationService, Depends(get_generation_service)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> VoiceProfileResponse:
    # Профиль создаётся pending, затем sync-пайплайн клонирования обновит статус.
    async with sessionmaker() as session:
        async with session.begin():
            profile = await VoiceRepository(session).create_profile(
                user_id=current.id, name=body.name, consent_id=UUID(body.consent_id),
                sample_asset_url=body.sample_asset_url,
            )
            profile_id = profile.id
        session.expunge(profile)

    result = await generation.create_job(
        user_id=current.id,
        job_type=JobType.voice_clone,
        payload={
            "voice_profile_id": str(profile_id),
            "consent_id": body.consent_id,
            "sample_asset_url": body.sample_asset_url,
            "name": body.name,
        },
    )

    async with sessionmaker() as session:
        profile = await VoiceRepository(session).get_profile(profile_id)
        return VoiceProfileResponse(
            id=str(profile.id), name=profile.name, status=profile.status.value,
            provider_voice_id=profile.provider_voice_id,
            preview_url=profile.sample_asset_url,
            sample_duration_seconds=profile.sample_duration_seconds,
            job_id=str(result.job_id),
            created_at=profile.created_at,
        )


@router.get("", response_model=list[VoiceProfileResponse], summary="Библиотека голосов")
async def list_voices(
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[object, Depends(get_sessionmaker)],
) -> list[VoiceProfileResponse]:
    async with sessionmaker() as session:
        profiles = await VoiceRepository(session).list_profiles(current.id)
        return [
            VoiceProfileResponse(
                id=str(p.id), name=p.name, status=p.status.value,
                provider_voice_id=p.provider_voice_id,
                preview_url=p.sample_asset_url,
                sample_duration_seconds=p.sample_duration_seconds,
                created_at=p.created_at,
            )
            for p in profiles
        ]


@router.patch(
    "/{voice_id}",
    response_model=VoiceProfileResponse,
    summary="Переименовать голос",
    responses={
        400: {"description": "Пустое имя"},
        404: {"description": "Голос не найден или уже удалён"},
    },
)
async def rename_voice(
    voice_id: UUID,
    body: RenameVoiceRequest,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> VoiceProfileResponse:
    """Переименование профиля голоса (ADR-012).

    Owner-check через `VoiceRepository.get_active_profile` (фильтрует soft-deleted).
    Разрешено для любого не-удалённого профиля (в т.ч. pending/failed). Меняет только
    `name` (trimmed); status/provider_voice_id/consent_id/`deleted_at` не трогает.
    Идемпотентно. Повтор/чужой/удалён → 404. В ответе `job_id=null`.
    """
    async with sessionmaker() as session:
        async with session.begin():
            repo = VoiceRepository(session)
            profile = await repo.get_active_profile(voice_id)
            if profile is None or profile.user_id != current.id:
                raise VoiceProfileNotFound()
            profile.name = body.name
            return VoiceProfileResponse(
                id=str(profile.id),
                name=profile.name,
                status=profile.status.value,
                provider_voice_id=profile.provider_voice_id,
                preview_url=profile.sample_asset_url,
                sample_duration_seconds=profile.sample_duration_seconds,
                job_id=None,
                created_at=profile.created_at,
            )


@router.delete(
    "/{voice_id}",
    status_code=204,
    summary="Удалить голос",
    responses={404: {"description": "Голос не найден или уже удалён"}},
)
async def delete_voice(
    voice_id: UUID,
    current: Annotated[User, Depends(get_current_user)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> Response:
    """Soft-delete профиля голоса (ADR-011).

    Скрывает профиль из листингов/резолвов. Consent, sample-asset и голос у
    провайдера НЕ трогаем; монеты не возвращаем. Повтор/чужой → 404.
    """
    async with sessionmaker() as session:
        async with session.begin():
            repo = VoiceRepository(session)
            profile = await repo.get_active_profile(voice_id)
            if profile is None or profile.user_id != current.id:
                raise VoiceProfileNotFound()
            profile.deleted_at = datetime.now(UTC)
    return Response(status_code=204)

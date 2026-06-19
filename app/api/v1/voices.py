from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import ValidationFailed
from app.deps import get_current_user, get_generation_service, get_sessionmaker
from app.domain.enums import JobType, VoiceConsentKind
from app.domain.models.user import User
from app.domain.repositories.voice import VoiceRepository
from app.domain.schemas.voices import (
    ConsentRequest,
    ConsentResponse,
    CreateVoiceRequest,
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
            provider_voice_id=profile.provider_voice_id, job_id=str(result.job_id),
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
                provider_voice_id=p.provider_voice_id, created_at=p.created_at,
            )
            for p in profiles
        ]

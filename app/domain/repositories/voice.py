from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import VoiceConsentKind, VoiceProfileStatus
from app.domain.models.voice import VoiceConsent, VoiceProfile


class VoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_consent(
        self, *, user_id: UUID, kind: VoiceConsentKind, accepted: bool, statement: str | None
    ) -> VoiceConsent:
        consent = VoiceConsent(
            user_id=user_id, kind=kind, accepted=accepted, statement=statement
        )
        self._session.add(consent)
        await self._session.flush()
        return consent

    async def get_consent(self, consent_id: UUID) -> VoiceConsent | None:
        return await self._session.get(VoiceConsent, consent_id)

    async def latest_accepted_consent(self, user_id: UUID) -> VoiceConsent | None:
        stmt = (
            select(VoiceConsent)
            .where(VoiceConsent.user_id == user_id, VoiceConsent.accepted.is_(True))
            .order_by(VoiceConsent.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create_profile(
        self,
        *,
        user_id: UUID,
        name: str | None,
        consent_id: UUID | None,
        sample_asset_url: str | None,
        status: VoiceProfileStatus = VoiceProfileStatus.pending,
    ) -> VoiceProfile:
        profile = VoiceProfile(
            user_id=user_id, name=name, consent_id=consent_id,
            sample_asset_url=sample_asset_url, status=status,
        )
        self._session.add(profile)
        await self._session.flush()
        return profile

    async def get_profile(self, profile_id: UUID) -> VoiceProfile | None:
        return await self._session.get(VoiceProfile, profile_id)

    async def update_profile(
        self, *, profile_id: UUID, provider_voice_id: str | None,
        status: VoiceProfileStatus, meta: dict[str, Any] | None = None,
    ) -> None:
        profile = await self._session.get(VoiceProfile, profile_id)
        if profile is not None:
            profile.provider_voice_id = provider_voice_id
            profile.status = status
            if meta is not None:
                profile.meta = meta

    async def list_profiles(self, user_id: UUID) -> list[VoiceProfile]:
        stmt = (
            select(VoiceProfile)
            .where(VoiceProfile.user_id == user_id)
            .order_by(VoiceProfile.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

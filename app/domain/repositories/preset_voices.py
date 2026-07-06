from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.preset_voice import PresetVoice


class PresetVoicesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[PresetVoice]:
        stmt = (
            select(PresetVoice)
            .where(PresetVoice.active.is_(True))
            .order_by(PresetVoice.sort_order, PresetVoice.title)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_by_key(self, key: str) -> PresetVoice | None:
        stmt = select(PresetVoice).where(PresetVoice.key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

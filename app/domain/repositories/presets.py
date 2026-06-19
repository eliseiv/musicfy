from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import PresetKind
from app.domain.models.prompt_preset import PromptPreset


class PresetsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_kind(self, kind: PresetKind) -> list[PromptPreset]:
        stmt = (
            select(PromptPreset)
            .where(PromptPreset.kind == kind, PromptPreset.active.is_(True))
            .order_by(PromptPreset.sort_order, PromptPreset.title)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_by_key(self, *, kind: PresetKind, key: str) -> PromptPreset | None:
        stmt = select(PromptPreset).where(
            PromptPreset.kind == kind, PromptPreset.key == key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

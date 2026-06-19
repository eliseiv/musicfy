from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AssetKind
from app.domain.models.asset import Asset


class AssetsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        kind: AssetKind,
        url: str,
        mime: str | None = None,
        duration_seconds: float | None = None,
        size_bytes: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Asset:
        asset = Asset(
            user_id=user_id,
            kind=kind,
            url=url,
            mime=mime,
            duration_seconds=Decimal(str(duration_seconds)) if duration_seconds else None,
            size_bytes=size_bytes,
            meta=meta,
        )
        self._session.add(asset)
        await self._session.flush()
        return asset

    async def get(self, asset_id: UUID) -> Asset | None:
        return await self._session.get(Asset, asset_id)

    async def list_for_user(
        self, *, user_id: UUID, kind: AssetKind | None = None, limit: int = 50, offset: int = 0
    ) -> list[Asset]:
        stmt = select(Asset).where(Asset.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(Asset.kind == kind)
        stmt = stmt.order_by(Asset.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

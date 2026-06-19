from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import UploadRejected
from app.domain.enums import AssetKind
from app.domain.models.asset import Asset
from app.domain.providers.fal.base import FalProvider
from app.domain.repositories.assets import AssetsRepository


class AssetService:
    """Загрузка пользовательских медиа в fal storage и регистрация Asset."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], fal: FalProvider
    ) -> None:
        self._sessionmaker = sessionmaker
        self._fal = fal

    async def upload(
        self,
        *,
        user_id: UUID,
        content: bytes,
        filename: str,
        content_type: str,
        kind: AssetKind,
        max_bytes: int,
        allowed_content_types: set[str],
    ) -> Asset:
        if not content:
            raise UploadRejected(details={"reason": "empty"})
        if len(content) > max_bytes:
            raise UploadRejected(details={"reason": "too_large", "max_bytes": max_bytes})
        if allowed_content_types and content_type not in allowed_content_types:
            raise UploadRejected(
                details={"reason": "unsupported_content_type", "content_type": content_type}
            )
        url = await self._fal.upload_to_storage(
            content=content, filename=filename, content_type=content_type
        )
        async with self._sessionmaker() as session:
            async with session.begin():
                asset = await AssetsRepository(session).create(
                    user_id=user_id,
                    kind=kind,
                    url=url,
                    mime=content_type,
                    size_bytes=len(content),
                )
            session.expunge(asset)
        return asset

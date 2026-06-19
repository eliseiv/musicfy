from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.session import Session


class SessionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, user_id: UUID, token_hash: str, expires_at: datetime
    ) -> Session:
        row = Session(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        stmt = select(Session).where(Session.token_hash == token_hash)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def revoke(self, *, token_hash: str, now: datetime) -> None:
        stmt = (
            update(Session)
            .where(Session.token_hash == token_hash, Session.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await self._session.execute(stmt)

    async def reassign_user(self, *, from_user_id: UUID, to_user_id: UUID) -> None:
        """Перенос активных сессий guest-пользователя на постоянного (merge)."""
        stmt = (
            update(Session)
            .where(Session.user_id == from_user_id)
            .values(user_id=to_user_id)
        )
        await self._session.execute(stmt)

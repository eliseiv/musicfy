from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuthProvider
from app.domain.models.auth_identity import AuthIdentity
from app.domain.models.user import User


class UsersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def create(self, *, is_guest: bool, display_name: str | None = None) -> User:
        user = User(is_guest=is_guest, display_name=display_name)
        self._session.add(user)
        await self._session.flush()
        return user

    async def set_apple_account(
        self, user: User, *, apple_sub: str, display_name: str | None
    ) -> None:
        user.is_guest = False
        user.apple_sub = apple_sub
        if display_name and not user.display_name:
            user.display_name = display_name
        await self._session.flush()

    # --- identities ---

    async def find_identity(
        self, *, provider: AuthProvider, subject: str
    ) -> AuthIdentity | None:
        stmt = select(AuthIdentity).where(
            AuthIdentity.provider == provider,
            AuthIdentity.subject == subject,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add_identity(
        self,
        *,
        user_id: UUID,
        provider: AuthProvider,
        subject: str,
        meta: dict[str, Any] | None = None,
    ) -> AuthIdentity:
        identity = AuthIdentity(
            user_id=user_id, provider=provider, subject=subject, meta=meta
        )
        self._session.add(identity)
        await self._session.flush()
        return identity

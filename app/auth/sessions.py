from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import InvalidSession
from app.auth.apple import AppleIdentityVerifier
from app.domain.enums import AuthProvider
from app.domain.models.user import User
from app.domain.repositories.sessions import SessionsRepository
from app.domain.repositories.users import UsersRepository

logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class SessionResult:
    user_id: UUID
    is_guest: bool
    display_name: str | None
    token: str
    expires_at: datetime


class AuthService:
    """Guest/device-сессии и Sign in with Apple поверх opaque-токенов."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        apple_verifier: AppleIdentityVerifier,
        session_ttl_seconds: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._apple = apple_verifier
        self._ttl = session_ttl_seconds

    def _new_token(self, now: datetime) -> tuple[str, str, datetime]:
        token = secrets.token_urlsafe(32)
        return token, _hash_token(token), now + timedelta(seconds=self._ttl)

    async def create_guest(self, *, device_id: str | None = None) -> SessionResult:
        """Гостевая сессия. Повторный вход с тем же `device_id` — тот же пользователь.

        Идемпотентность по устройству обязательна: клиент зовёт этот endpoint при каждом
        холодном старте, и на втором вызове identity `(guest, device_id)` уже существует.
        Раньше метод безусловно создавал нового пользователя и падал на
        `uq_auth_identities_provider_subject` → 500, то есть приложение после переустановки
        не могло войти вообще, а вместе с сессией терялись монеты и история устройства.

        Пользователь, уже промоутнутый в Apple-аккаунт (`is_guest=False`), по `device_id`
        НЕ выдаётся: иначе знание идентификатора устройства заменяло бы Sign in with Apple.
        Для него заводится новый гость с синтетическим subject, а слияние выполнит
        `sign_in_with_apple` (он мигрирует данные текущего гостя в постоянный аккаунт).
        """
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            async with session.begin():
                user = await self._resolve_guest(session, device_id)
                token, token_hash, expires_at = self._new_token(now)
                await SessionsRepository(session).create(
                    user_id=user.id, token_hash=token_hash, expires_at=expires_at
                )
                is_guest, display_name = user.is_guest, user.display_name
            return SessionResult(
                user_id=user.id,
                is_guest=is_guest,
                display_name=display_name,
                token=token,
                expires_at=expires_at,
            )

    async def _resolve_guest(self, session: AsyncSession, device_id: str | None) -> User:
        """Находит пользователя устройства либо заводит нового (внутри транзакции вызывающего).

        Гонка двух холодных стартов с одним `device_id` реальна (retry клиента, два запроса
        подряд): оба не найдут identity и оба вставят её. Поэтому вставка сделана
        idempotent-ой на уровне БД (`ON CONFLICT DO NOTHING`), а при проигрыше гонки
        пользователь перечитывается по выигравшей identity — вместо 500 клиент получает
        сессию того же гостя.
        """
        users = UsersRepository(session)

        if device_id:
            existing = await users.find_identity(
                provider=AuthProvider.guest, subject=device_id
            )
            if existing is not None:
                owner = await users.get_by_id(existing.user_id)
                if owner is not None and owner.is_guest:
                    return owner
                # Устройство принадлежит промоутнутому аккаунту (или строка пользователя
                # исчезла) — device_id занят, новому гостю даём синтетический subject.
                return await self._create_guest_user(users, subject=None)

        return await self._create_guest_user(users, subject=device_id)

    @staticmethod
    async def _create_guest_user(
        users: UsersRepository, *, subject: str | None
    ) -> User:
        """Создаёт гостя и его identity. `subject=None` → синтетический `guest:{user_id}`."""
        user = await users.create(is_guest=True)
        claimed = await users.add_identity_if_absent(
            user_id=user.id,
            provider=AuthProvider.guest,
            subject=subject or f"guest:{user.id}",
        )
        if claimed or subject is None:
            return user
        # Гонку выиграл параллельный запрос: отдаём пользователя победителя, а созданная
        # строка остаётся без identity — она не адресуема ни по одному входу.
        winner_identity = await users.find_identity(
            provider=AuthProvider.guest, subject=subject
        )
        if winner_identity is not None:
            winner = await users.get_by_id(winner_identity.user_id)
            if winner is not None and winner.is_guest:
                return winner
        return user

    async def sign_in_with_apple(
        self,
        *,
        identity_token: str,
        nonce: str | None,
        display_name: str | None,
        current_user_id: UUID | None,
    ) -> SessionResult:
        claims = await self._apple.verify(identity_token, nonce=nonce)
        apple_sub = str(claims["sub"])
        now = datetime.now(UTC)

        async with self._sessionmaker() as session:
            async with session.begin():
                users = UsersRepository(session)
                existing_identity = await users.find_identity(
                    provider=AuthProvider.apple, subject=apple_sub
                )

                if existing_identity is not None:
                    # Аккаунт Apple уже есть. Если текущий — guest, мигрируем его
                    # данные в постоянный аккаунт (без потери прогресса).
                    target_user = await users.get_by_id(existing_identity.user_id)
                    assert target_user is not None
                    if current_user_id and current_user_id != target_user.id:
                        await self._merge_guest_into(
                            session, from_user_id=current_user_id, to_user_id=target_user.id
                        )
                    user = target_user
                else:
                    # Новый Apple-аккаунт. Промоутим текущего guest или создаём нового.
                    user = None
                    if current_user_id:
                        candidate = await users.get_by_id(current_user_id)
                        if candidate is not None and candidate.is_guest:
                            user = candidate
                    if user is None:
                        user = await users.create(is_guest=False)
                    await users.set_apple_account(
                        user, apple_sub=apple_sub, display_name=display_name
                    )
                    await users.add_identity(
                        user_id=user.id,
                        provider=AuthProvider.apple,
                        subject=apple_sub,
                        meta={"email": claims.get("email")} if claims.get("email") else None,
                    )

                token, token_hash, expires_at = self._new_token(now)
                await SessionsRepository(session).create(
                    user_id=user.id, token_hash=token_hash, expires_at=expires_at
                )

            return SessionResult(
                user_id=user.id,
                is_guest=user.is_guest,
                display_name=user.display_name,
                token=token,
                expires_at=expires_at,
            )

    async def _merge_guest_into(
        self, session: AsyncSession, *, from_user_id: UUID, to_user_id: UUID
    ) -> None:
        """Переносит данные guest-пользователя на постоянный аккаунт.

        В Фазе 1 переносятся сессии. Доменные таблицы (credits/jobs/library/voice)
        регистрируют свои reassign-операции в MERGE_REASSIGNERS по мере добавления.
        """
        await SessionsRepository(session).reassign_user(
            from_user_id=from_user_id, to_user_id=to_user_id
        )
        for reassign in MERGE_REASSIGNERS:
            await reassign(session, from_user_id, to_user_id)
        logger.info("Merged guest %s into %s", from_user_id, to_user_id)

    async def resolve(self, token: str) -> User:
        token_hash = _hash_token(token)
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            repo = SessionsRepository(session)
            row = await repo.get_by_token_hash(token_hash)
            if row is None or row.revoked_at is not None or row.expires_at <= now:
                raise InvalidSession()
            user = await UsersRepository(session).get_by_id(row.user_id)
            if user is None:
                raise InvalidSession()
            session.expunge(user)
            return user

    async def logout(self, token: str) -> None:
        token_hash = _hash_token(token)
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            async with session.begin():
                await SessionsRepository(session).revoke(token_hash=token_hash, now=now)


# Расширяемая точка для merge данных при guest→apple. Каждая доменная область
# (credits, jobs, library, voice) регистрирует здесь корутину
# `async def reassign(session, from_user_id, to_user_id) -> None`.
MERGE_REASSIGNERS: list = []

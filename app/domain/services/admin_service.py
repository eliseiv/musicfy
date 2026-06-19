from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import UserNotFound
from app.domain.enums import (
    BillingProvider,
    CreditCategory,
    CreditLedgerKind,
    CreditSource,
    SubscriptionStatus,
)
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.users import UsersRepository


class AdminService:
    """Ручное начисление кредитов и подписки (админ/саппорт)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def _ensure_user(self, session: AsyncSession, user_id: UUID) -> None:
        if await UsersRepository(session).get_by_id(user_id) is None:
            raise UserNotFound(details={"user_id": str(user_id)})

    async def grant_credits(
        self,
        *,
        user_id: UUID,
        category: CreditCategory,
        amount: int,
        reason: str | None = None,
    ) -> int:
        """Начисляет покупные (non-expiring) кредиты категории. Возвращает новый баланс."""
        async with self._sessionmaker() as session:
            async with session.begin():
                await self._ensure_user(session, user_id)
                repo = CreditsRepository(session)
                bal = await repo.ensure_balance(user_id=user_id, category=category)
                bal.available += amount
                await repo.append_ledger(
                    user_id=user_id,
                    kind=CreditLedgerKind.credit_promo,
                    amount=amount,
                    category=category,
                    source=CreditSource.promo,
                    reason=reason or "admin_grant",
                    ref_type="admin",
                )
                return bal.available

    async def grant_subscription(
        self,
        *,
        user_id: UUID,
        grants: dict[CreditCategory, int],
        period_days: int | None,
        label: str = "admin_grant",
    ) -> None:
        """Активирует подписку и грантит периодные entitlements по категориям."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=period_days) if period_days else None
        async with self._sessionmaker() as session:
            async with session.begin():
                await self._ensure_user(session, user_id)
                repo = CreditsRepository(session)
                await repo.upsert_subscription(
                    user_id=user_id,
                    values={
                        "status": SubscriptionStatus.active,
                        "provider": BillingProvider.apple,
                        "product_external_id": label,
                        "original_transaction_id": None,
                        "expires_at": expires_at,
                    },
                )
                for category, granted in grants.items():
                    await repo.upsert_entitlement(
                        user_id=user_id,
                        category=category,
                        granted=int(granted),
                        period_start=now,
                        period_end=expires_at,
                        source_product_external_id=label,
                    )
                    await repo.append_ledger(
                        user_id=user_id,
                        kind=CreditLedgerKind.credit_subscription_grant,
                        amount=int(granted),
                        category=category,
                        source=CreditSource.subscription,
                        reason=label,
                        ref_type="admin",
                    )

    async def revoke_subscription(self, *, user_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            async with session.begin():
                await self._ensure_user(session, user_id)
                repo = CreditsRepository(session)
                sub = await repo.get_subscription_for_update(user_id)
                if sub is not None:
                    sub.status = SubscriptionStatus.canceled
                # Подписочные лимиты сгорают немедленно (истекаем период).
                for ent in await repo.list_entitlements(user_id):
                    ent.period_end = now

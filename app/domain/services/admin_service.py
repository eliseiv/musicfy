from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import UserNotFound
from app.domain.enums import (
    BillingProvider,
    CreditLedgerKind,
    CreditSource,
    SubscriptionStatus,
)
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.pricing import PricingRepository
from app.domain.repositories.users import UsersRepository


class AdminService:
    """Ручное начисление монет, подписки и правка прайс-листа (админ/саппорт)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def _ensure_user(self, session: AsyncSession, user_id: UUID) -> None:
        if await UsersRepository(session).get_by_id(user_id) is None:
            raise UserNotFound(details={"user_id": str(user_id)})

    async def grant_credits(
        self,
        *,
        user_id: UUID,
        coins: int,
        reason: str | None = None,
    ) -> int:
        """Начисляет монеты в кошелёк (non-expiring). Возвращает новый доступный баланс."""
        async with self._sessionmaker() as session:
            async with session.begin():
                await self._ensure_user(session, user_id)
                repo = CreditsRepository(session)
                wallet = await repo.ensure_wallet(user_id)
                wallet.coins_available += coins
                await repo.append_ledger(
                    user_id=user_id,
                    kind=CreditLedgerKind.credit_promo,
                    amount=coins,
                    source=CreditSource.promo,
                    reason=reason or "admin_grant",
                    ref_type="admin",
                )
                return int(wallet.coins_available)

    async def grant_subscription(
        self,
        *,
        user_id: UUID,
        coins: int,
        period_days: int | None,
        label: str = "admin_grant",
    ) -> None:
        """Активирует подписку и начисляет монеты в кошелёк."""
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
                if coins > 0:
                    wallet = await repo.ensure_wallet(user_id)
                    wallet.coins_available += coins
                    await repo.append_ledger(
                        user_id=user_id,
                        kind=CreditLedgerKind.credit_subscription_grant,
                        amount=coins,
                        source=CreditSource.subscription,
                        reason=label,
                        ref_type="admin",
                    )

    async def revoke_subscription(self, *, user_id: UUID) -> None:
        """Отзывает подписку. Монеты не сгорают (non-expiring), меняется только статус."""
        async with self._sessionmaker() as session:
            async with session.begin():
                await self._ensure_user(session, user_id)
                repo = CreditsRepository(session)
                sub = await repo.get_subscription_for_update(user_id)
                if sub is not None:
                    sub.status = SubscriptionStatus.canceled

    async def set_price(
        self, *, job_type: str, price_coins: int, active: bool
    ) -> tuple[str, int, bool]:
        """Создаёт/обновляет цену типа генерации. Возвращает актуальные значения."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = await PricingRepository(session).upsert_price(
                    job_type=job_type, price_coins=price_coins, active=active
                )
                return row.job_type, int(row.price_coins), bool(row.active)

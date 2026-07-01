from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import (
    BillingProvider,
    CreditLedgerKind,
    CreditSource,
    ProductKind,
    SubscriptionStatus,
)
from app.domain.models.billing import Product
from app.domain.providers.billing.apple import AppleStoreKitVerifier
from app.domain.repositories.credits import CreditsRepository
from app.domain.repositories.products import ProductsRepository, PurchasesRepository

logger = logging.getLogger(__name__)

SUBSCRIPTION_KINDS = {ProductKind.subscription}


class BillingService:
    """Применяет StoreKit-транзакции: единый грант монет в кошелёк (паки + подписки)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        verifier: AppleStoreKitVerifier,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._verifier = verifier

    async def verify_and_apply_transaction(
        self, *, user_id: UUID, signed_transaction: str
    ) -> dict:
        tx = self._verifier.decode_transaction(signed_transaction)
        if not tx["transaction_id"] or not tx["product_id"]:
            return {"status": "ignored", "reason": "incomplete_transaction"}
        return await self._apply(user_id=user_id, tx=tx)

    async def apply_notification(self, *, signed_payload: str) -> dict:
        note = self._verifier.decode_notification(signed_payload)
        tx = note.get("transaction")
        if not tx:
            return {"status": "ignored", "reason": "no_transaction"}
        otid = tx.get("original_transaction_id") or tx.get("transaction_id")
        async with self._sessionmaker() as session:
            user_id = await PurchasesRepository(session).find_user_by_original_transaction(otid)
        if user_id is None:
            logger.warning("billing notification: unknown original_transaction=%s", otid)
            return {"status": "ignored", "reason": "unknown_user"}
        note_type = (note.get("notification_type") or "").upper()
        if note_type in ("REFUND", "REVOKE", "EXPIRED"):
            return await self._handle_revocation(user_id=user_id, tx=tx, note_type=note_type)
        return await self._apply(user_id=user_id, tx=tx)

    async def _apply(self, *, user_id: UUID, tx: dict) -> dict:
        async with self._sessionmaker() as session:
            async with session.begin():
                product = await ProductsRepository(session).get_by_external_id(
                    tx["product_id"]
                )
                if product is None:
                    return {"status": "ignored", "reason": "unknown_product"}

                purchases = PurchasesRepository(session)
                newly = await purchases.record(
                    user_id=user_id,
                    product_external_id=product.external_product_id,
                    transaction_id=tx["transaction_id"],
                    original_transaction_id=tx.get("original_transaction_id"),
                    raw=tx.get("raw"),
                )
                credits = CreditsRepository(session)
                is_subscription = product.kind in SUBSCRIPTION_KINDS
                if is_subscription:
                    await self._update_subscription_state(credits, user_id, product, tx)
                await self._grant_coins(
                    credits,
                    user_id=user_id,
                    product=product,
                    transaction_id=tx["transaction_id"],
                    is_subscription=is_subscription,
                )
        return {"status": "ok", "deduplicated": not newly}

    async def _update_subscription_state(
        self, credits: CreditsRepository, user_id: UUID, product: Product, tx: dict
    ) -> None:
        """Апдейт статуса/срока подписки (idempotent upsert, без начисления монет)."""
        expires_at = _ms_to_dt(tx.get("expires_date_ms"))
        await credits.upsert_subscription(
            user_id=user_id,
            values={
                "status": SubscriptionStatus.active,
                "provider": BillingProvider.apple,
                "product_external_id": product.external_product_id,
                "original_transaction_id": tx.get("original_transaction_id"),
                "expires_at": expires_at,
            },
        )

    async def _grant_coins(
        self,
        credits: CreditsRepository,
        *,
        user_id: UUID,
        product: Product,
        transaction_id: str,
        is_subscription: bool,
    ) -> None:
        """Единый монетный грант (пак и подписка), идемпотентный по транзакции.

        Инкремент `coins_available` выполняется ТОЛЬКО при первой ledger-записи
        (`newly is True`) — иначе повторный verify/notification дал бы двойное начисление.
        """
        coins = int((product.grants or {}).get("coins") or 0)
        if coins <= 0:
            return
        kind = (
            CreditLedgerKind.credit_subscription_grant
            if is_subscription
            else CreditLedgerKind.credit_purchase
        )
        source = (
            CreditSource.subscription if is_subscription else CreditSource.purchase
        )
        newly = await credits.append_ledger(
            user_id=user_id,
            kind=kind,
            amount=coins,
            source=source,
            reason="subscription_grant" if is_subscription else "pack_purchase",
            ref_type="transaction",
            ref_id=transaction_id,
            idempotency_key=f"purchase:{transaction_id}",
        )
        if not newly:
            return
        wallet = await credits.ensure_wallet(user_id)
        wallet.coins_available += coins

    async def _handle_revocation(self, *, user_id: UUID, tx: dict, note_type: str) -> dict:
        async with self._sessionmaker() as session:
            async with session.begin():
                credits = CreditsRepository(session)
                sub = await credits.get_subscription_for_update(user_id)
                if sub is not None:
                    sub.status = (
                        SubscriptionStatus.expired
                        if note_type == "EXPIRED" else SubscriptionStatus.canceled
                    )
        return {"status": "ok", "action": note_type.lower()}


def _ms_to_dt(ms: object) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (ValueError, TypeError):
        return None

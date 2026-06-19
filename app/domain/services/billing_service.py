from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import (
    BillingProvider,
    CreditCategory,
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
PACK_KINDS = {
    ProductKind.song_pack,
    ProductKind.cover_pack,
    ProductKind.video_pack,
    ProductKind.mixed_pack,
}


class BillingService:
    """Применяет StoreKit-транзакции: гранты подписки и покупные кредиты."""

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
                if product.kind in SUBSCRIPTION_KINDS:
                    await self._grant_subscription(credits, user_id, product, tx)
                elif newly and product.kind in PACK_KINDS:
                    await self._grant_pack(credits, user_id, product, tx["transaction_id"])
        return {"status": "ok", "deduplicated": not newly}

    async def _grant_subscription(
        self, credits: CreditsRepository, user_id: UUID, product: Product, tx: dict
    ) -> None:
        now = datetime.now(UTC)
        expires_at = _ms_to_dt(tx.get("expires_date_ms"))
        period_end = expires_at
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
        for category_str, granted in (product.grants or {}).items():
            try:
                category = CreditCategory(category_str)
            except ValueError:
                continue
            await credits.upsert_entitlement(
                user_id=user_id,
                category=category,
                granted=int(granted),
                period_start=now,
                period_end=period_end,
                source_product_external_id=product.external_product_id,
            )
            await credits.append_ledger(
                user_id=user_id, kind=CreditLedgerKind.credit_subscription_grant,
                amount=int(granted), category=category, source=CreditSource.subscription,
                reason="subscription_grant", ref_type="product",
                ref_id=product.external_product_id,
            )

    async def _grant_pack(
        self, credits: CreditsRepository, user_id: UUID, product: Product, transaction_id: str
    ) -> None:
        for category_str, amount in (product.grants or {}).items():
            try:
                category = CreditCategory(category_str)
            except ValueError:
                continue
            bal = await credits.ensure_balance(user_id=user_id, category=category)
            bal.available += int(amount)
            await credits.append_ledger(
                user_id=user_id, kind=CreditLedgerKind.credit_purchase,
                amount=int(amount), category=category, source=CreditSource.purchase,
                reason="pack_purchase", ref_type="transaction", ref_id=transaction_id,
                idempotency_key=f"purchase:{transaction_id}:{category.value}",
            )

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

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
    StoreKitEnvironment,
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

    @staticmethod
    def _environment(tx: dict) -> StoreKitEnvironment:
        """Верифицированное окружение транзакции; неизвестное → Production (строжайший дедуп)."""
        try:
            return StoreKitEnvironment(str(tx.get("environment") or ""))
        except ValueError:
            return StoreKitEnvironment.production

    @classmethod
    def _dedup_key(cls, user_id: UUID, tx: dict) -> str:
        """Единственный источник истины дедупа покупки (ADR-013 D1/D2).

        Один и тот же ключ уникализирует строку `purchases` (`uq_purchases_dedup_key`) и
        запись в `credit_ledger` (`purchase:{dedup_key}`) — слои не могут разойтись.

        * `Production:{tx}` / `Sandbox:{tx}` — глобально: Apple гарантирует уникальность
          `transactionId` внутри окружения, поэтому чужой чек повторно не применить (replay).
          Раздельные namespace не дают sandbox-ID затенить боевой.
        * `Xcode:{user}:{tx}:{purchase_date_ms}` — ID Xcode StoreKit Test не уникальны
          (счётчик, обнуляемый при *Delete All Transactions*). `purchaseDate` даёт энтропию:
          повторная покупка после сброса — новый ключ (монеты начисляются), повтор того же
          payload — тот же ключ (идемпотентность сохранена).
        """
        env = cls._environment(tx)
        transaction_id = tx["transaction_id"]
        if env is StoreKitEnvironment.xcode:
            purchase_date_ms = _as_int(tx.get("purchase_date_ms"))
            return f"{env.value}:{user_id}:{transaction_id}:{purchase_date_ms}"
        return f"{env.value}:{transaction_id}"

    async def _apply(self, *, user_id: UUID, tx: dict) -> dict:
        environment = self._environment(tx)
        dedup_key = self._dedup_key(user_id, tx)
        async with self._sessionmaker() as session:
            async with session.begin():
                product = await ProductsRepository(session).get_by_external_id(
                    tx["product_id"]
                )
                if product is None:
                    return {"status": "ignored", "reason": "unknown_product"}

                purchases = PurchasesRepository(session)
                is_subscription = product.kind in SUBSCRIPTION_KINDS
                newly = await purchases.record(
                    user_id=user_id,
                    product_external_id=product.external_product_id,
                    transaction_id=tx["transaction_id"],
                    original_transaction_id=tx.get("original_transaction_id"),
                    dedup_key=dedup_key,
                    environment=environment.value,
                    purchase_date=_ms_to_dt(tx.get("purchase_date_ms")),
                    raw=tx.get("raw"),
                )
                credits = CreditsRepository(session)

                if not newly:
                    # Ключ занят. Если владелец — другой пользователь, это применение чужого
                    # чека: монет не будет, и ответ обязан это показать (не "ok").
                    owner = await purchases.find_owner_by_dedup_key(dedup_key)
                    if owner != user_id:
                        logger.warning(
                            "billing: transaction claimed by another user: "
                            "environment=%s transaction=%s",
                            environment.value,
                            tx["transaction_id"],
                        )
                        return {
                            "status": "rejected",
                            "reason": "transaction_already_claimed",
                            "deduplicated": False,
                        }
                    # Свой повтор (restore / повторный verify): эффект уже применён ранее.
                    # Состояние подписки обновляем — idempotent upsert, без начисления монет.
                    if is_subscription:
                        await self._update_subscription_state(credits, user_id, product, tx)
                    return {"status": "ok", "deduplicated": True}

                if is_subscription:
                    await self._update_subscription_state(credits, user_id, product, tx)
                # Монеты — ТОЛЬКО при реально вставленной строке purchases (ADR-013 D2).
                await self._grant_coins(
                    credits,
                    user_id=user_id,
                    product=product,
                    transaction_id=tx["transaction_id"],
                    dedup_key=dedup_key,
                    is_subscription=is_subscription,
                )
        return {"status": "ok", "deduplicated": False}

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
        dedup_key: str,
        is_subscription: bool,
    ) -> None:
        """Единый монетный грант (пак и подписка), идемпотентный по дедуп-ключу покупки.

        Вызывается только когда строка `purchases` реально вставлена. Идемпотентность по
        ledger-ключу `purchase:{dedup_key}` остаётся как defence-in-depth (оба insert'а — в
        одной транзакции, поэтому состояния «покупка есть, гранта нет» не бывает).
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
            idempotency_key=f"purchase:{dedup_key}",
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


def _as_int(value: object) -> int:
    """Материал дедуп-ключа: нечисловой/отсутствующий `purchaseDate` → 0 (ключ детерминирован)."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0

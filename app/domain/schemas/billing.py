from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel


class BalanceResponse(CamelModel):
    """Единый кошелёк монет пользователя."""

    coins_available: int
    coins_reserved: int


class PriceView(CamelModel):
    job_type: str
    price_coins: int


class PricingResponse(CamelModel):
    """Активный прайс-лист платных типов генерации."""

    prices: list[PriceView]


class ProductView(CamelModel):
    product_id: str
    kind: str
    title: str
    grants: dict
    period_days: int | None


class VerifyPurchaseRequest(CamelModel):
    """Верификация покупки StoreKit 2."""

    signed_transaction: str = Field(
        min_length=1, description="JWS signedTransactionInfo из StoreKit 2 (Transaction)."
    )


class RestoreRequest(CamelModel):
    """Restore purchases: массив подписанных транзакций из StoreKit."""

    signed_transactions: list[str] = Field(
        default_factory=list, description="Список JWS-транзакций для восстановления."
    )


class ApplyResultResponse(CamelModel):
    """Результат применения StoreKit-транзакции.

    `status`:
      * `ok` — транзакция применена этому пользователю (`deduplicated=false` — монеты начислены
        сейчас; `true` — были начислены ранее этим же чеком, повтор идемпотентен). Особый случай
        `reason=subscription_transferred`: подписка переехала с прежнего (брошенного при
        переустановке) аккаунта — entitlement активен, монеты не переначислялись (ADR-017);
      * `rejected` — транзакция НЕ применена (`reason`), монет нет. В частности
        `transaction_already_claimed`: коин-пак уже погашен другим аккаунтом (replay-защита,
        ADR-013; для подписок с ADR-017 это переносится, а не отклоняется);
      * `ignored` — payload без эффекта (`reason`: `unknown_product` / `incomplete_transaction`).
    """

    status: str
    deduplicated: bool = False
    reason: str | None = None


class LedgerEntryView(CamelModel):
    kind: str
    category: str | None
    source: str | None
    amount: int
    reason: str | None
    created_at: datetime

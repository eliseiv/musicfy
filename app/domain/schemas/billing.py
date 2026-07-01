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
    status: str
    deduplicated: bool = False


class LedgerEntryView(CamelModel):
    kind: str
    category: str | None
    source: str | None
    amount: int
    reason: str | None
    created_at: datetime

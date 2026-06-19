from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CamelModel


class CategoryBalance(CamelModel):
    category: str
    subscription_remaining: int
    subscription_granted: int
    period_end: datetime | None
    purchased_available: int


class BalanceResponse(CamelModel):
    balances: list[CategoryBalance]


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

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    BillingProvider,
    CreditCategory,
    CreditLedgerKind,
    CreditSource,
    ProductKind,
    SubscriptionStatus,
)
from app.models.base import Base, TimestampMixin


class Product(Base, TimestampMixin):
    """Каталог продуктов App Store: подписки + паки генераций."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("external_product_id", name="uq_products_external_product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    external_product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[ProductKind] = mapped_column(
        SAEnum(ProductKind, name="product_kind", native_enum=True), nullable=False
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    # Сколько монет начисляется покупкой: {"coins": N}. Для подписок — монеты за период.
    grants: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Для подписок: длительность периода в днях (7 / 365).
    period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), default=True
    )


class SubscriptionState(Base, TimestampMixin):
    __tablename__ = "subscription_state"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_subscription_state_user_id"),
        Index("ix_subscription_state_status_expires", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(SubscriptionStatus, name="subscription_status", native_enum=True),
        nullable=False, server_default=text("'none'"),
    )
    provider: Mapped[BillingProvider | None] = mapped_column(
        SAEnum(BillingProvider, name="billing_provider", native_enum=True), nullable=True
    )
    product_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CoinWallet(Base, TimestampMixin):
    """Единый кошелёк монет пользователя (одна строка на юзера, монеты non-expiring)."""

    __tablename__ = "coin_wallets"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_coin_wallets_user_id"),
        CheckConstraint("coins_available >= 0", name="ck_coin_wallets_available_nonneg"),
        CheckConstraint("coins_reserved >= 0", name="ck_coin_wallets_reserved_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    coins_available: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )
    coins_reserved: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )


class GenerationPrice(Base, TimestampMixin):
    """Прайс-лист: цена типа генерации в монетах. Меняется admin-эндпоинтом."""

    __tablename__ = "generation_prices"
    __table_args__ = (
        UniqueConstraint("job_type", name="uq_generation_prices_job_type"),
        CheckConstraint("price_coins >= 0", name="ck_generation_prices_price_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    # Значение из JobType (song / cover / video). String, а не enum — прайс-лист
    # независим от credit-категорий и расширяется без миграции enum.
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    price_coins: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )


class CreditLedgerEntry(Base):
    """Аудит-журнал кредитных операций."""

    __tablename__ = "credit_ledger"
    __table_args__ = (
        Index("ix_credit_ledger_user_id_created_at", "user_id", "created_at"),
        UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[CreditCategory | None] = mapped_column(
        SAEnum(CreditCategory, name="credit_category", native_enum=True, create_type=False),
        nullable=True,
    )
    source: Mapped[CreditSource | None] = mapped_column(
        SAEnum(CreditSource, name="credit_source", native_enum=True), nullable=True
    )
    kind: Mapped[CreditLedgerKind] = mapped_column(
        SAEnum(CreditLedgerKind, name="credit_ledger_kind", native_enum=True), nullable=False
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Purchase(Base, TimestampMixin):
    """StoreKit-транзакция (для restore и cross-device sync)."""

    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_purchases_transaction_id"),
        Index("ix_purchases_user_id", "user_id"),
        Index("ix_purchases_original_transaction_id", "original_transaction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    original_transaction_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'applied'")
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

"""coin wallet billing: coin_wallets + generation_prices, drop entitlements/credit_balances

Revision ID: 0009_coin_wallet
Revises: 0008_moderation_analytics
Create Date: 2026-07-01

Переход на единый кошелёк монет (ADR-005 / docs/billing-coins-redesign.md §6).
Реальных платящих пользователей нет → reset без конвертации.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_coin_wallet"
down_revision: str | None = "0008_moderation_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Стартовый прайс-лист (утверждён владельцем, Q-BILL-2). Бесплатные типы (lyrics,
# voice_clone) в справочник не заносятся.
PRICES = [
    ("song", 10),
    ("cover", 5),
    ("video", 30),
]


def _ts():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "coin_wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("coins_available", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("coins_reserved", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_coin_wallets_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_coin_wallets_user_id"),
        sa.CheckConstraint("coins_available >= 0", name="ck_coin_wallets_available_nonneg"),
        sa.CheckConstraint("coins_reserved >= 0", name="ck_coin_wallets_reserved_nonneg"),
    )

    op.create_table(
        "generation_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("price_coins", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_ts(),
        sa.UniqueConstraint("job_type", name="uq_generation_prices_job_type"),
        sa.CheckConstraint("price_coins >= 0", name="ck_generation_prices_price_nonneg"),
    )

    seed = sa.text(
        "INSERT INTO generation_prices (job_type, price_coins, active) "
        "VALUES (:job_type, :price, true)"
    )
    bind = op.get_bind()
    for job_type, price in PRICES:
        bind.execute(seed, {"job_type": job_type, "price": price})

    # Reset старой мультивалютной модели: балансы не конвертируются.
    op.drop_table("credit_balances")
    op.drop_table("entitlements")


def downgrade() -> None:
    # Симметрично 0004: воссоздаём entitlements/credit_balances (пустыми).
    credit_category = postgresql.ENUM(name="credit_category", create_type=False)

    op.create_table(
        "entitlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", credit_category, nullable=False),
        sa.Column("granted", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("used", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_product_external_id", sa.String(255), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_entitlements_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "category", name="uq_entitlements_user_id_category"),
    )

    op.create_table(
        "credit_balances",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", credit_category, nullable=False),
        sa.Column("available", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_credit_balances_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "category", name="uq_credit_balances_user_id_category"),
    )

    op.drop_table("generation_prices")
    op.drop_table("coin_wallets")

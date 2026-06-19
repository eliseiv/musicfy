"""billing: products, subscription_state, entitlements, credit_balances,
credit_ledger, purchases

Revision ID: 0004_billing
Revises: 0003_seed_presets
Create Date: 2026-06-18

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_billing"
down_revision: str | None = "0003_seed_presets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRODUCT_KIND = ("subscription", "song_pack", "cover_pack", "video_pack", "mixed_pack")
SUBSCRIPTION_STATUS = ("none", "active", "canceled", "expired")
BILLING_PROVIDER = ("apple",)
CREDIT_SOURCE = ("subscription", "purchase", "promo")
CREDIT_LEDGER_KIND = (
    "credit_subscription_grant", "credit_purchase", "credit_promo",
    "debit_reserve", "debit_capture", "credit_release", "credit_refund",
    "debit_expire", "debit_adjustment", "credit_adjustment",
)


def _ts():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def upgrade() -> None:
    bind = op.get_bind()
    product_kind = postgresql.ENUM(*PRODUCT_KIND, name="product_kind", create_type=False)
    subscription_status = postgresql.ENUM(*SUBSCRIPTION_STATUS, name="subscription_status", create_type=False)
    billing_provider = postgresql.ENUM(*BILLING_PROVIDER, name="billing_provider", create_type=False)
    credit_source = postgresql.ENUM(*CREDIT_SOURCE, name="credit_source", create_type=False)
    credit_ledger_kind = postgresql.ENUM(*CREDIT_LEDGER_KIND, name="credit_ledger_kind", create_type=False)
    for e in (product_kind, subscription_status, billing_provider, credit_source, credit_ledger_kind):
        e.create(bind, checkfirst=True)
    # credit_category уже создан в 0002 — переиспользуем.
    credit_category = postgresql.ENUM(name="credit_category", create_type=False)

    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("external_product_id", sa.String(255), nullable=False),
        sa.Column("kind", product_kind, nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("grants", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_ts(),
        sa.UniqueConstraint("external_product_id", name="uq_products_external_product_id"),
    )

    op.create_table(
        "subscription_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", subscription_status, server_default=sa.text("'none'"), nullable=False),
        sa.Column("provider", billing_provider, nullable=True),
        sa.Column("product_external_id", sa.String(255), nullable=True),
        sa.Column("original_transaction_id", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_subscription_state_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_subscription_state_user_id"),
    )
    op.create_index("ix_subscription_state_status_expires", "subscription_state", ["status", "expires_at"])

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

    op.create_table(
        "credit_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", credit_category, nullable=True),
        sa.Column("source", credit_source, nullable=True),
        sa.Column("kind", credit_ledger_kind, nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("ref_type", sa.String(32), nullable=True),
        sa.Column("ref_id", sa.String(128), nullable=True),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_credit_ledger_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_ledger_idempotency_key"),
    )
    op.create_index("ix_credit_ledger_user_id_created_at", "credit_ledger", ["user_id", "created_at"])

    op.create_table(
        "purchases",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_external_id", sa.String(255), nullable=False),
        sa.Column("transaction_id", sa.String(255), nullable=False),
        sa.Column("original_transaction_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), server_default=sa.text("'applied'"), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_purchases_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("transaction_id", name="uq_purchases_transaction_id"),
    )
    op.create_index("ix_purchases_user_id", "purchases", ["user_id"])
    op.create_index("ix_purchases_original_transaction_id", "purchases", ["original_transaction_id"])

    # Добавляем 'apple' в webhook_provider (создан в 0001 как ('fal','apple')) — уже есть.


def downgrade() -> None:
    op.drop_table("purchases")
    op.drop_table("credit_ledger")
    op.drop_table("credit_balances")
    op.drop_table("entitlements")
    op.drop_table("subscription_state")
    op.drop_table("products")
    for name in ("credit_ledger_kind", "credit_source", "billing_provider", "subscription_status", "product_kind"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)

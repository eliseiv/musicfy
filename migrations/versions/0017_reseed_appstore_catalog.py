"""reseed product catalog to verbatim App Store Connect ids (ADR-015)

Revision ID: 0017_reseed_appstore_catalog
Revises: 0016_purchase_dedup_key
Create Date: 2026-07-14

Замена каталога продуктов биллинга на вербатим-каталог App Store Connect (ADR-015).
StoreKit матчит покупку по `product_id` побайтово: если `external_product_id` в БД не
байт-в-байт равен `product_id` из App Store, `BillingService.apply` получает
`product is None` → `ignored/unknown_product` и монеты не начисляются.

upgrade: идемпотентный upsert 7 новых продуктов (active=true) через
`ON CONFLICT ON CONSTRAINT uq_products_external_product_id DO UPDATE ...` + деактивация
(active=false, НЕ DELETE) 6 старых. Строки НЕ удаляются, чтобы FK из purchases /
subscription_state и резолв уже совершённых покупок (get_by_external_id без фильтра active)
продолжали работать.

downgrade: симметрично и FK-safe — реактивация 6 старых (active=true) + деактивация 7
новых (active=false, НЕ DELETE: за окно жизни ревизии по новому продукту могли пройти
покупки со ссылкой по FK).
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_reseed_appstore_catalog"
down_revision: str | None = "0016_purchase_dedup_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (external_product_id, kind, title, grants, period_days) — вербатим-каталог (ADR-015 §1).
# Строки — источник истины ADR §1; любое расхождение с App Store Connect воспроизводит
# исходный баг unknown_product.
NEW_PRODUCTS = [
    ("100_tokens_9.99", "coin_pack", "100 Tokens", {"coins": 100}, None),
    ("250_tokens_19.99", "coin_pack", "250 Tokens", {"coins": 250}, None),
    ("500_tokens_34.99", "coin_pack", "500 Tokens", {"coins": 500}, None),
    ("1000_tokens_59.99", "coin_pack", "1000 Tokens", {"coins": 1000}, None),
    ("2000_tokens_99.99", "coin_pack", "2000 Tokens", {"coins": 2000}, None),
    ("week_6.99_not_trial", "subscription", "Weekly", {"coins": 100}, 7),
    ("yearly_49.99_not_trial", "subscription", "Yearly", {"coins": 1000}, 365),
]

# Прежний монетный каталог (ADR-015 §2), засеянный 0011. Деактивируется, но НЕ удаляется.
LEGACY_PRODUCT_IDS = [
    "com.musicfy.coins.small",
    "com.musicfy.coins.medium",
    "com.musicfy.coins.large",
    "com.musicfy.coins.xl",
    "com.musicfy.sub.weekly",
    "com.musicfy.sub.yearly",
]

_UPSERT_NEW = sa.text(
    "INSERT INTO products (external_product_id, kind, title, grants, period_days, active) "
    "VALUES (:pid, CAST(:kind AS product_kind), :title, CAST(:grants AS jsonb), :period, true) "
    "ON CONFLICT ON CONSTRAINT uq_products_external_product_id DO UPDATE SET "
    "kind = EXCLUDED.kind, "
    "title = EXCLUDED.title, "
    "grants = EXCLUDED.grants, "
    "period_days = EXCLUDED.period_days, "
    "active = true, "
    "updated_at = now()"
)

_SET_ACTIVE_BY_IDS = sa.text(
    "UPDATE products SET active = :active, updated_at = now() "
    "WHERE external_product_id = ANY(:ids)"
)


def upgrade() -> None:
    bind = op.get_bind()
    # 1. Upsert 7 новых продуктов (active=true), идемпотентно.
    for pid, kind, title, grants, period in NEW_PRODUCTS:
        bind.execute(
            _UPSERT_NEW,
            {
                "pid": pid,
                "kind": kind,
                "title": title,
                "grants": json.dumps(grants),
                "period": period,
            },
        )
    # 2. Деактивация 6 старых (active=false, НЕ DELETE) — история и продления сохраняются.
    bind.execute(_SET_ACTIVE_BY_IDS, {"active": False, "ids": LEGACY_PRODUCT_IDS})


def downgrade() -> None:
    bind = op.get_bind()
    # 1. Реактивация 6 старых.
    bind.execute(_SET_ACTIVE_BY_IDS, {"active": True, "ids": LEGACY_PRODUCT_IDS})
    # 2. Деактивация 7 новых (active=false, НЕ DELETE: FK-safe, ADR-015 §Миграция/downgrade).
    new_ids = [pid for pid, *_ in NEW_PRODUCTS]
    bind.execute(_SET_ACTIVE_BY_IDS, {"active": False, "ids": new_ids})

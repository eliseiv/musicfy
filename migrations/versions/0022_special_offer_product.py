"""спецоффер offer_week_3.99_nottrial в каталог продуктов

Revision ID: 0022_special_offer_product
Revises: 0021_products_lower_unique
Create Date: 2026-09-09

Недельная подписка по сниженной цене ($3.99 против $6.99) с тем же грантом — 700 монет за
7 дней. Цена живёт в App Store Connect, у нас хранится только размер начисления, поэтому
разница со `week_6.99_not_trial` в каталоге не отражается: это два отдельных продукта с
одинаковым `grants`.

Написание `external_product_id` — **вербатим из App Store Connect**: `nottrial` слитно, в
отличие от `week_6.99_not_trial`. Резолв регистронезависим (ADR-021), но не
«подчёркивание-независим»: любое расхождение символов даёт `unknown_product` и потерю монет
(ADR-015).

upsert через `ON CONFLICT ON CONSTRAINT ... DO UPDATE` — миграция идемпотентна и не падает,
если продукт уже был заведён вручную.

downgrade деактивирует продукт (`active=false`), но НЕ удаляет строку: за окно жизни ревизии
по нему могли пройти покупки со ссылкой из `purchases` / `subscription_state`, а продления и
restore обязаны резолвиться и по неактивному продукту.

Каталог общий для всех инстансов (ADR-018): запись появится и там, где спецоффера в App Store
нет. Это безвредно — продукт, который нельзя купить, просто не встретится ни в одном чеке;
лишняя позиция видна лишь в `GET /v1/billing/products`.
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_special_offer_product"
down_revision: str | None = "0021_products_lower_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (external_product_id, kind, title, grants, period_days)
OFFER_PRODUCT = (
    "offer_week_3.99_nottrial",
    "subscription",
    "Weekly Offer",
    {"coins": 700},
    7,
)

_UPSERT = sa.text(
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

_SET_ACTIVE = sa.text(
    "UPDATE products SET active = :active, updated_at = now() "
    "WHERE external_product_id = :pid"
)


def upgrade() -> None:
    pid, kind, title, grants, period = OFFER_PRODUCT
    op.get_bind().execute(
        _UPSERT,
        {
            "pid": pid,
            "kind": kind,
            "title": title,
            "grants": json.dumps(grants),
            "period": period,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(_SET_ACTIVE, {"active": False, "pid": OFFER_PRODUCT[0]})

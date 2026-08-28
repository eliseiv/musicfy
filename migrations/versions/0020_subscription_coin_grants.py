"""proportional coin grants for subscription products (ADR-019)

Revision ID: 0020_subscription_coin_grants
Revises: 0019_adapty_webhook_events
Create Date: 2026-08-28

Приведение грантов подписок к тарифной сетке коин-паков (~10 монет за $1, как в
`100_tokens_9.99` … `2000_tokens_99.99`):

| продукт                  | было | стало |
|--------------------------|------|-------|
| `week_6.99_not_trial`    |  100 |   700 |
| `yearly_49.99_not_trial` | 1000 |  5000 |

Каталог `products` — ОБЩИЙ источник истины для обоих платёжных контуров, поэтому правка
меняет размер начисления и на StoreKit-пути (`BillingService._grant_coins`), и на
Adapty-пути (`AdaptyWebhookService._coins_for`). Это намеренно: две цены за один и тот же
продукт в зависимости от контура покупки — расхождение, которое пришлось бы объяснять
пользователю.

Уже совершённые покупки не пересчитываются: `credit_ledger` — журнал фактов, а гранты
идемпотентны по ключу покупки, поэтому новая ставка действует только для новых транзакций.

Идентификаторы продуктов НЕ трогаются: каталог уже вербатим-совпадает с App Store Connect
(0017 / ADR-015), и Adapty матчит `vendor_product_id` тем же побайтовым сравнением.
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_subscription_coin_grants"
down_revision: str | None = "0019_adapty_webhook_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (external_product_id, grants) — новая сетка.
NEW_GRANTS = [
    ("week_6.99_not_trial", {"coins": 700}),
    ("yearly_49.99_not_trial", {"coins": 5000}),
]

# Значения, засеянные 0017 (для downgrade).
OLD_GRANTS = [
    ("week_6.99_not_trial", {"coins": 100}),
    ("yearly_49.99_not_trial", {"coins": 1000}),
]

_UPDATE = sa.text(
    "UPDATE products SET grants = CAST(:grants AS jsonb), updated_at = now() "
    "WHERE external_product_id = :pid"
)


def _apply(rows: list[tuple[str, dict]]) -> None:
    bind = op.get_bind()
    for pid, grants in rows:
        bind.execute(_UPDATE, {"pid": pid, "grants": json.dumps(grants)})


def upgrade() -> None:
    _apply(NEW_GRANTS)


def downgrade() -> None:
    _apply(OLD_GRANTS)

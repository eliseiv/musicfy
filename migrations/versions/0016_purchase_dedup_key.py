"""Дедуп StoreKit-покупок по environment-scoped ключу (ADR-013).

Revision ID: 0016_purchase_dedup_key
Revises: 0015_soft_delete
Create Date: 2026-07-13

Дедуп покупки перестаёт быть глобальным по «голому» `transaction_id` и считается по
`dedup_key` (ADR-013 D1/D2): `Production:{tx}` / `Sandbox:{tx}` — глобально (replay-защита
боевых чеков сохранена побайтово), `Xcode:{user}:{tx}:{purchase_date_ms}` — на пользователя
(ID Xcode StoreKit Test не уникальны и обнуляются при *Delete All Transactions*).

Шаги 1-6 обязаны выполниться одной транзакцией (Alembic оборачивает upgrade в неё).
Шаг 6 — бэкфилл `credit_ledger.idempotency_key` — КРИТИЧЕН: без него уже применённый боевой
чек остался бы с ключом `purchase:{tx}`, тогда как новый код ищет `purchase:Production:{tx}`,
и restore после деплоя начислил бы монеты ПОВТОРНО.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_purchase_dedup_key"
down_revision: str | None = "0015_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Окружение транзакции. Существующие строки применялись как боевые → дефолт корректен.
    op.add_column(
        "purchases",
        sa.Column(
            "environment",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'Production'"),
        ),
    )
    # 2. Дата покупки: аудит + материал дедуп-ключа для Xcode.
    op.add_column(
        "purchases",
        sa.Column("purchase_date", sa.DateTime(timezone=True), nullable=True),
    )

    # 3. Дедуп-ключ: добавляем nullable → бэкфилл → NOT NULL.
    op.add_column("purchases", sa.Column("dedup_key", sa.String(255), nullable=True))
    op.execute(
        "UPDATE purchases SET dedup_key = 'Production:' || transaction_id "
        "WHERE dedup_key IS NULL"
    )
    op.alter_column("purchases", "dedup_key", nullable=False)

    # 4. Новый дедуп-инвариант.
    op.create_unique_constraint("uq_purchases_dedup_key", "purchases", ["dedup_key"])

    # 5. Старый глобальный UNIQUE снимается; transaction_id остаётся под неуникальным
    #    индексом — по нему ходят restore/саппорт.
    op.drop_constraint("uq_purchases_transaction_id", "purchases", type_="unique")
    op.create_index("ix_purchases_transaction_id", "purchases", ["transaction_id"])

    # 6. КРИТИЧНО: ключи леджера переводятся в новый формат (см. docstring).
    #    Условие `idempotency_key = 'purchase:' || ref_id` бьёт ровно по старому формату —
    #    строки других потоков (lyrics:*, manual:*) и уже сконвертированные не затрагиваются.
    op.execute(
        "UPDATE credit_ledger SET idempotency_key = 'purchase:Production:' || ref_id "
        "WHERE ref_type = 'transaction' "
        "AND ref_id IS NOT NULL "
        "AND idempotency_key = 'purchase:' || ref_id"
    )


def downgrade() -> None:
    # Обратный бэкфилл ключей леджера.
    op.execute(
        "UPDATE credit_ledger SET idempotency_key = 'purchase:' || ref_id "
        "WHERE ref_type = 'transaction' "
        "AND ref_id IS NOT NULL "
        "AND idempotency_key = 'purchase:Production:' || ref_id"
    )

    op.drop_index("ix_purchases_transaction_id", table_name="purchases")
    op.drop_constraint("uq_purchases_dedup_key", "purchases", type_="unique")
    op.drop_column("purchases", "dedup_key")
    op.drop_column("purchases", "purchase_date")
    op.drop_column("purchases", "environment")

    # Восстановление глобального UNIQUE — best-effort (ср. TD-001): падает, если в таблице
    # есть строки с одинаковым transaction_id в разных окружениях (то, ради чего и делался
    # ADR-013). Такие строки перед downgrade нужно снять вручную.
    op.create_unique_constraint(
        "uq_purchases_transaction_id", "purchases", ["transaction_id"]
    )

"""add 'coin_pack' value to product_kind enum

Revision ID: 0010_add_coin_pack_enum
Revises: 0009_coin_wallet
Create Date: 2026-07-01

Только расширение enum, БЕЗ использования значения (docs/billing-coins-redesign.md §6, Правка 3).
PostgreSQL (PG12+) не позволяет использовать новое значение enum в той же транзакции, где оно
добавлено. Поэтому ADD VALUE вынесен в отдельную миграцию и выполняется в autocommit_block,
чтобы значение было закоммичено до его использования в 0011.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_add_coin_pack_enum"
down_revision: str | None = "0009_coin_wallet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # autocommit_block: ADD VALUE коммитится немедленно, вне общей транзакции миграций.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE product_kind ADD VALUE IF NOT EXISTS 'coin_pack'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значения enum — no-op (задокументировано).
    pass

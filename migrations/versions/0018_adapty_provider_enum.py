"""add 'adapty' value to billing_provider enum (ADR-019)

Revision ID: 0018_adapty_provider_enum
Revises: 0017_reseed_appstore_catalog
Create Date: 2026-08-28

Только расширение enum, БЕЗ использования значения. PostgreSQL (PG12+) не позволяет
использовать новое значение enum в той же транзакции, где оно добавлено, поэтому ADD VALUE
вынесен в отдельную миграцию и выполняется в autocommit_block — чтобы значение было
закоммичено до первой записи `subscription_state.provider = 'adapty'` вебхуком Adapty.
Образец — 0010 (`coin_pack` в `product_kind`).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_adapty_provider_enum"
down_revision: str | None = "0017_reseed_appstore_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # autocommit_block: ADD VALUE коммитится немедленно, вне общей транзакции миграций.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE billing_provider ADD VALUE IF NOT EXISTS 'adapty'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значения enum — no-op (задокументировано).
    pass

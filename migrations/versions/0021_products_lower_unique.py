"""уникальный индекс по lower(external_product_id) (ADR-021)

Revision ID: 0021_products_lower_unique
Revises: 0020_subscription_coin_grants
Create Date: 2026-09-07

Резолв продукта стал регистронезависимым (`ProductsRepository.get_by_external_id`), потому
что один и тот же по смыслу продукт заведён в App Store Connect в разном регистре у разных
приложений (`100_tokens_9.99` / `100_Tokens_9.99`), а каталог сеется общей миграцией.

Регистронезависимый поиск обязан оставаться ОДНОЗНАЧНЫМ: если в каталоге окажутся две
строки, различающиеся только регистром, `scalar_one_or_none()` поднимет
`MultipleResultsFound` прямо на пути начисления монет. Уникальный функциональный индекс
делает такое состояние невозможным на уровне БД, а не соглашением.

Существующий `uq_products_external_product_id` НЕ снимается: он остаётся точным ключом для
`ON CONFLICT ON CONSTRAINT` в сид-миграциях каталога (0017 и далее).

upgrade падает, если коллизия уже есть в данных — это намеренно: молча схлопнуть два
продукта в один нельзя, оператор обязан решить, какой из них верный.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_products_lower_unique"
down_revision: str | None = "0020_subscription_coin_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_products_lower_external_product_id"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON products (lower(external_product_id))"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")

"""reseed product catalog with coin packs + coin subscriptions

Revision ID: 0011_reseed_coin_products
Revises: 0010_add_coin_pack_enum
Create Date: 2026-07-01

Пересид каталога под монетную модель (docs/billing-coins-redesign.md §4/§6, Правка 3).
Использование значения enum 'coin_pack' допустимо здесь — оно закоммичено миграцией 0010.
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_reseed_coin_products"
down_revision: str | None = "0010_add_coin_pack_enum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (external_product_id, kind, title, grants, period_days) — каталог монет (§4).
COIN_PRODUCTS = [
    ("com.musicfy.coins.small", "coin_pack", "100 Coins", {"coins": 100}, None),
    ("com.musicfy.coins.medium", "coin_pack", "550 Coins", {"coins": 550}, None),
    ("com.musicfy.coins.large", "coin_pack", "1200 Coins", {"coins": 1200}, None),
    ("com.musicfy.coins.xl", "coin_pack", "3000 Coins", {"coins": 3000}, None),
    ("com.musicfy.sub.weekly", "subscription", "Weekly", {"coins": 150}, 7),
    ("com.musicfy.sub.yearly", "subscription", "Yearly", {"coins": 8000}, 365),
]

# Прежний per-category каталог (для downgrade, соответствует 0005_seed_products).
LEGACY_PRODUCTS = [
    ("com.musicfy.sub.weekly", "subscription", "Weekly", {"song": 30, "cover": 10, "video": 3}, 7),
    ("com.musicfy.sub.yearly", "subscription", "Yearly", {"song": 1000, "cover": 300, "video": 120}, 365),
    ("com.musicfy.pack.song", "song_pack", "Song Pack", {"song": 20}, None),
    ("com.musicfy.pack.cover", "cover_pack", "Cover Pack", {"cover": 10}, None),
    ("com.musicfy.pack.video", "video_pack", "Video Pack", {"video": 5}, None),
    ("com.musicfy.pack.mixed", "mixed_pack", "Mixed Pack", {"song": 10, "cover": 5, "video": 2}, None),
]


def _seed(products: list) -> None:
    stmt = sa.text(
        "INSERT INTO products (external_product_id, kind, title, grants, period_days) "
        "VALUES (:pid, CAST(:kind AS product_kind), :title, CAST(:grants AS jsonb), :period)"
    )
    bind = op.get_bind()
    for pid, kind, title, grants, period in products:
        bind.execute(
            stmt,
            {"pid": pid, "kind": kind, "title": title, "grants": json.dumps(grants), "period": period},
        )


def upgrade() -> None:
    op.execute("DELETE FROM products")
    _seed(COIN_PRODUCTS)


def downgrade() -> None:
    op.execute("DELETE FROM products")
    _seed(LEGACY_PRODUCTS)

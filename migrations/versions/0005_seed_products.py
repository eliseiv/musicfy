"""seed products: subscriptions + generation packs

Revision ID: 0005_seed_products
Revises: 0004_billing
Create Date: 2026-06-18

"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_seed_products"
down_revision: str | None = "0004_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (external_product_id, kind, title, grants, period_days)
PRODUCTS = [
    ("com.musicfy.sub.weekly", "subscription", "Weekly",
     {"song": 30, "cover": 10, "video": 3}, 7),
    ("com.musicfy.sub.yearly", "subscription", "Yearly",
     {"song": 1000, "cover": 300, "video": 120}, 365),
    ("com.musicfy.pack.song", "song_pack", "Song Pack", {"song": 20}, None),
    ("com.musicfy.pack.cover", "cover_pack", "Cover Pack", {"cover": 10}, None),
    ("com.musicfy.pack.video", "video_pack", "Video Pack", {"video": 5}, None),
    ("com.musicfy.pack.mixed", "mixed_pack", "Mixed Pack",
     {"song": 10, "cover": 5, "video": 2}, None),
]


def upgrade() -> None:
    stmt = sa.text(
        "INSERT INTO products (external_product_id, kind, title, grants, period_days) "
        "VALUES (:pid, CAST(:kind AS product_kind), :title, CAST(:grants AS jsonb), :period)"
    )
    bind = op.get_bind()
    for pid, kind, title, grants, period in PRODUCTS:
        bind.execute(
            stmt,
            {"pid": pid, "kind": kind, "title": title, "grants": json.dumps(grants), "period": period},
        )


def downgrade() -> None:
    op.execute("DELETE FROM products")

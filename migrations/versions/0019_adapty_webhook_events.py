"""adapty_webhook_events + subscription_state.will_renew (ADR-019)

Revision ID: 0019_adapty_webhook_events
Revises: 0018_adapty_provider_enum
Create Date: 2026-08-28

Схема под вебхук подписок Adapty.

1. Таблица `adapty_webhook_events` — единственная точка дедупа ДОСТАВКИ события.
   `event_id` (значение `profile_event_id` из payload) объявлен PRIMARY KEY: именно он
   обеспечивает `INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id` в сервисе.
   `payload` хранится ЦЕЛИКОМ: Adapty не подписывает тело и переприсылает события, поэтому
   для разбора инцидентов нужен исходный объект, а не только digest (карточных данных в
   payload нет — секрет живёт в заголовке, а не в теле). FK на `users(id) ON DELETE CASCADE`
   и индекс по `user_id` — под диагностические выборки «что приходило по пользователю».

   Отдельная таблица, а не `processed_webhooks` (fal/apple): та хранит лишь `payload_digest`
   и не имеет владельца события, чего для разбора платёжных инцидентов недостаточно.

2. Колонка `subscription_state.will_renew boolean NULL` — «продлится ли автоматически».
   NULL значит «неизвестно»: StoreKit-путь это поле не заполняет, и обратная засыпка была бы
   выдумыванием данных. Adapty присылает отмену автопродления отдельным событием, при котором
   доступ сохраняется до конца оплаченного периода; без флага клиент не отличит «активна и
   продлится» от «активна, но последняя».

downgrade симметричен: снимает колонку и удаляет таблицу вместе с индексом. Значение enum
`billing_provider.adapty` (0018) не откатывается — PostgreSQL не умеет удалять значения enum.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_adapty_webhook_events"
down_revision: str | None = "0018_adapty_provider_enum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adapty_webhook_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_adapty_webhook_events_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_adapty_webhook_events"),
    )
    op.create_index(
        "ix_adapty_webhook_events_user_id",
        "adapty_webhook_events",
        ["user_id"],
    )
    op.add_column(
        "subscription_state",
        sa.Column("will_renew", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscription_state", "will_renew")
    op.drop_index("ix_adapty_webhook_events_user_id", table_name="adapty_webhook_events")
    op.drop_table("adapty_webhook_events")

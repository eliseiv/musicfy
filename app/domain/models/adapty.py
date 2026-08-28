from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdaptyWebhookEvent(Base):
    """Журнал доставленных событий Adapty — единственная точка дедупа доставки (ADR-019).

    `event_id` (значение `profile_event_id` из payload) — PRIMARY KEY: он обеспечивает
    `INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id` в
    `AdaptyWebhookService._apply`. Пустой RETURNING → повторная доставка → ни одной мутации.

    Отдельная таблица, а не `processed_webhooks` (fal/apple): Adapty не подписывает payload
    и переприсылает события бесконечно, поэтому для разбора инцидентов нужен ПОЛНЫЙ payload
    и владелец события, а не только digest. Карточных данных в payload нет, поэтому тело
    сохраняется целиком.
    """

    __tablename__ = "adapty_webhook_events"
    __table_args__ = (
        Index("ix_adapty_webhook_events_user_id", "user_id"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

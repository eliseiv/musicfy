from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import WebhookProvider
from app.models.base import Base


class ProcessedWebhook(Base):
    """Идемпотентность webhook'ов: (provider, event_id) — первичный ключ.

    Двухфазная обработка: outcome 'received' (заклеймили) → 'applied' (применили).
    """

    __tablename__ = "processed_webhooks"
    __table_args__ = (
        Index("ix_processed_webhooks_received_at", "received_at"),
    )

    provider: Mapped[WebhookProvider] = mapped_column(
        SAEnum(WebhookProvider, name="webhook_provider", native_enum=True),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'received'")
    )
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

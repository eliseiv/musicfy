from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PresetVoice(Base, TimestampMixin):
    """Каталог пресет-голосов (AI Voices) для вкладки Create Cover.

    `provider_voice` — внутренний идентификатор голоса модели fal voice-changer;
    наружу не отдаётся (см. `PresetVoiceView`). Клиент оперирует стабильным `key`.
    """

    __tablename__ = "preset_voices"
    __table_args__ = (
        Index("uq_preset_voices_key", "key", unique=True),
        Index("ix_preset_voices_active_sort", "active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_voice: Mapped[str] = mapped_column(String(255), nullable=False)
    preview_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sample_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(16), nullable=True)
    style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

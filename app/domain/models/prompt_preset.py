from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import PresetKind
from app.models.base import Base, TimestampMixin


class PromptPreset(Base, TimestampMixin):
    """Каталог genre / mood / prompt presets для формы генерации."""

    __tablename__ = "prompt_presets"
    __table_args__ = (
        Index("uq_prompt_presets_kind_key", "kind", "key", unique=True),
        Index("ix_prompt_presets_kind_active_sort", "kind", "active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    kind: Mapped[PresetKind] = mapped_column(
        SAEnum(PresetKind, name="preset_kind", native_enum=True), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

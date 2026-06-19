from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class LyricsDraft(Base, TimestampMixin):
    """Редактируемый текст песни (генерируется LLM, может правиться пользователем)."""

    __tablename__ = "lyrics_drafts"
    __table_args__ = (
        Index("ix_lyrics_drafts_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'en'"))
    genre: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mood: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # source: 'generated' | 'edited'
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'generated'")
    )
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

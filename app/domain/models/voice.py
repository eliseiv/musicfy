from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
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

from app.domain.enums import VoiceConsentKind, VoiceProfileStatus
from app.models.base import Base, TimestampMixin


class VoiceConsent(Base, TimestampMixin):
    """Артефакт согласия на использование голоса."""

    __tablename__ = "voice_consents"
    __table_args__ = (
        Index("ix_voice_consents_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[VoiceConsentKind] = mapped_column(
        SAEnum(VoiceConsentKind, name="voice_consent_kind", native_enum=True), nullable=False
    )
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)


class VoiceProfile(Base, TimestampMixin):
    """Профиль клонированного голоса (хранит provider voice id)."""

    __tablename__ = "voice_profiles"
    __table_args__ = (
        Index("ix_voice_profiles_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"), default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_voice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[VoiceProfileStatus] = mapped_column(
        SAEnum(VoiceProfileStatus, name="voice_profile_status", native_enum=True),
        nullable=False, server_default=text("'pending'"),
    )
    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("voice_consents.id", ondelete="SET NULL"), nullable=True
    )
    sample_asset_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sample_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

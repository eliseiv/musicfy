"""Реестр ORM-моделей домена musicfy.

Импортируется в migrations/env.py, чтобы все таблицы зарегистрировались в
Base.metadata для автогенерации миграций. Модели добавляются по мере реализации
фаз; список ниже отражает целевую схему V1.
"""
from app.domain.models.asset import Asset  # noqa: F401,E402
from app.domain.models.auth_identity import AuthIdentity  # noqa: F401,E402

# Фаза 3 (billing / credits):
from app.domain.models.billing import (  # noqa: F401,E402
    CoinWallet,
    CreditLedgerEntry,
    GenerationPrice,
    Product,
    Purchase,
    SubscriptionState,
)

# Фаза 6 (video / push):
from app.domain.models.device import DevicePushToken  # noqa: F401,E402

# Фаза 2 (song + lyrics + media + presets):
from app.domain.models.job import Job, JobStageLog  # noqa: F401,E402
from app.domain.models.lyrics_draft import LyricsDraft  # noqa: F401,E402

# Фаза 7 (moderation / analytics):
from app.domain.models.moderation import ModerationCase  # noqa: F401,E402
from app.domain.models.preset_voice import PresetVoice  # noqa: F401,E402
from app.domain.models.prompt_preset import PromptPreset  # noqa: F401,E402
from app.domain.models.session import Session  # noqa: F401,E402
from app.domain.models.track import Track, TrackVariant  # noqa: F401,E402
from app.domain.models.usage_event import UsageEvent  # noqa: F401,E402

# Модели подключаются по фазам.
# Фаза 1 (auth):
from app.domain.models.user import User  # noqa: F401,E402

# Фаза 5 (voice):
from app.domain.models.voice import VoiceConsent, VoiceProfile  # noqa: F401,E402
from app.domain.models.webhook import ProcessedWebhook  # noqa: F401,E402
from app.models.base import Base

__all__ = ["Base"]

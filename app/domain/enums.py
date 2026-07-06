from __future__ import annotations

from enum import Enum


class JobType(str, Enum):
    """Тип задачи генерации. Определяет, какой pipeline её обрабатывает."""

    song = "song"
    lyrics = "lyrics"
    cover = "cover"
    voice_clone = "voice_clone"
    video = "video"


class JobStatus(str, Enum):
    """Статусы задач. На них завязан iOS UI."""

    created = "created"
    queued = "queued"
    running = "running"
    post_processing = "post_processing"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class JobStage(str, Enum):
    """Все стадии всех пайплайнов. Записываются в job_stage_log."""

    # общие
    prepare_prompt = "prepare_prompt"
    upload_cdn = "upload_cdn"
    finalize = "finalize"
    # song
    lyrics = "lyrics"
    music_generation = "music_generation"
    vocal_tts = "vocal_tts"
    mix_master = "mix_master"
    # cover
    stem_separation = "stem_separation"
    voice_conversion = "voice_conversion"
    # voice clone
    consent_check = "consent_check"
    quality_check = "quality_check"
    voice_clone = "voice_clone"
    # video
    source_prep = "source_prep"
    lipsync = "lipsync"
    visual_gen = "visual_gen"
    mux_audio = "mux_audio"
    lyrics_render = "lyrics_render"


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class CreditCategory(str, Enum):
    """Категория генерации. Лимиты подписки и паки раздельны по категориям."""

    song = "song"
    cover = "cover"
    video = "video"


class CreditSource(str, Enum):
    """Источник кредита: подписка (периодный, сгорает) или покупка (non-expiring)."""

    subscription = "subscription"
    purchase = "purchase"
    promo = "promo"


class CreditLedgerKind(str, Enum):
    credit_subscription_grant = "credit_subscription_grant"
    credit_purchase = "credit_purchase"
    credit_promo = "credit_promo"
    debit_reserve = "debit_reserve"
    debit_capture = "debit_capture"
    credit_release = "credit_release"
    credit_refund = "credit_refund"
    debit_expire = "debit_expire"
    debit_adjustment = "debit_adjustment"
    credit_adjustment = "credit_adjustment"


class BillingMode(str, Enum):
    per_generation = "per_generation"
    per_minute = "per_minute"


class RoundingMode(str, Enum):
    ceil = "ceil"
    floor = "floor"
    nearest = "nearest"


class AuthProvider(str, Enum):
    apple = "apple"
    guest = "guest"
    device = "device"


class SubscriptionStatus(str, Enum):
    none = "none"
    active = "active"
    canceled = "canceled"
    expired = "expired"


class BillingProvider(str, Enum):
    apple = "apple"


class ProductKind(str, Enum):
    subscription = "subscription"
    coin_pack = "coin_pack"
    # Легаси-значения enum (PG не удаляет значения легко). В новом каталоге не используются.
    song_pack = "song_pack"
    cover_pack = "cover_pack"
    video_pack = "video_pack"
    mixed_pack = "mixed_pack"


class AssetKind(str, Enum):
    audio = "audio"
    video = "video"
    voice_sample = "voice_sample"
    source_video = "source_video"
    stem = "stem"
    image = "image"


class TrackKind(str, Enum):
    song = "song"
    cover = "cover"


class VoiceConsentKind(str, Enum):
    own_voice = "own_voice"
    third_party_authorized = "third_party_authorized"


class VoiceProfileStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    failed = "failed"


class VideoMode(str, Enum):
    avatar_performance = "avatar_performance"
    visual_clip = "visual_clip"
    lyrics_video = "lyrics_video"


class VideoStyle(str, Enum):
    """Стиль видеоряда. Хранится строкой в Asset.meta (без отдельного PG-типа)."""

    realistic = "realistic"
    cartoon = "cartoon"
    anime = "anime"
    cinematic = "cinematic"


class VideoAspect(str, Enum):
    """Соотношение сторон. Значения совпадают с enum aspect_ratio моделей seedance."""

    square = "1:1"
    portrait_3_4 = "3:4"
    landscape_4_3 = "4:3"
    vertical_9_16 = "9:16"


class ModerationStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    blocked = "blocked"
    needs_review = "needs_review"


class PresetKind(str, Enum):
    genre = "genre"
    mood = "mood"
    prompt = "prompt"


class WebhookProvider(str, Enum):
    fal = "fal"
    apple = "apple"


# Списание генераций теперь определяется прайс-листом `generation_prices` (цена в монетах),
# а не маппингом тип→категория. Бесплатность `lyrics`/`voice_clone` — отсутствие строки в
# прайс-листе (цена 0). Прежний JOB_TYPE_TO_CATEGORY удалён (ADR-005 / billing-coins-redesign §8).

# Терминальные статусы задач.
TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.completed, JobStatus.failed, JobStatus.canceled}
)

# Активные (незавершённые) статусы — используются poller'ом и recovery.
ACTIVE_JOB_STATUSES = frozenset(
    {JobStatus.created, JobStatus.queued, JobStatus.running, JobStatus.post_processing}
)

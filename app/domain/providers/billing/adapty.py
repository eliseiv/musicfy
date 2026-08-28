"""Дефенсивный разбор payload вебхука Adapty (ADR-019).

Adapty не версионирует payload строго: одно и то же логическое поле приезжает под разными
ключами в разных версиях SDK/дашборда. Функции ниже читают значение best-effort и НИКОГДА не
бросают на отсутствующем/неверно типизированном поле (вложенный доступ защищён `isinstance`).
Тело разбирается вручную, без Pydantic: кривой проверочный пинг обязан дать `ignored`, а не
422 — Adapty ретраит любой не-2xx бесконечно.

Реальный wire-формат (проверен в бою, отличается от документации Adapty):
* идентификатор события — `profile_event_id`, а НЕ `event_id`;
* бизнес-поля лежат в `event_properties`, top-level — fallback (плоский вид дашборда);
* id-подобные поля приходят голым `int` (`410003298316682` без кавычек) → приводим к `str`;
* `access_level_updated` — условное событие, его смысл определяется `is_active` /
  `access_level_id`, поэтому семантика резолвится через `classify_event`, а не членством
  в плоском множестве.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

# Распознаваемые (нормализованные в lower-case) типы событий Adapty.
GRANTING_EVENTS = frozenset({"trial_started", "subscription_started", "subscription_renewed"})
EXPIRING_EVENTS = frozenset({"subscription_expired", "subscription_cancelled"})
NOOP_EVENTS = frozenset({"subscription_renewal_cancelled", "trial_renewal_cancelled"})
CONDITIONAL_EVENTS = frozenset({"access_level_updated"})
KNOWN_EVENTS = GRANTING_EVENTS | EXPIRING_EVENTS | NOOP_EVENTS | CONDITIONAL_EVENTS

# Семантика события — результат `classify_event`.
SEM_GRANTING = "granting"
SEM_EXPIRING = "expiring"
SEM_NOOP = "noop"

# Уровень доступа, который считается «премиум выдан» для условного access_level_updated.
ACCESS_LEVEL_PREMIUM = "premium"


@dataclass(frozen=True)
class ParsedAdaptyEvent:
    """Разобранное событие Adapty. `customer_user_id` — уже провалидированный UUID."""

    event_id: str
    event_type: str
    customer_user_id: uuid.UUID
    vendor_product_id: str | None
    expires_at: datetime.datetime | None
    transaction_id: str | None
    original_transaction_id: str | None
    is_active: bool | None
    access_level_id: str | None
    will_renew: bool | None


def _as_dict(value: Any) -> dict[str, Any]:
    """`value`, если это dict, иначе пустой dict (безопасный вложенный доступ)."""
    return value if isinstance(value, dict) else {}


def _first_str(*candidates: Any) -> str | None:
    """Первый непустой `str` либо не-bool `int`, приведённый к `str`; иначе None.

    id-подобные поля Adapty (`profile_event_id` / `transaction_id` /
    `original_transaction_id`) приходят голым числом. `bool` отвергается явно
    (`isinstance(True, int)` в Python — True), чтобы случайный `True` не стал строкой "True".
    """
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return str(candidate)
    return None


def _first_bool(*candidates: Any) -> bool | None:
    """Первый строго булев кандидат, иначе None.

    Строгость намеренная: `1` / `0` / `"true"` НЕ схлопываются в bool — только настоящий
    JSON-boolean считается значением, иначе поле трактуется как «не пришло». От `is_active`
    зависит выдача/отзыв доступа, догадки здесь недопустимы.
    """
    for candidate in candidates:
        if isinstance(candidate, bool):
            return candidate
    return None


def parse_event_id(body: dict[str, Any]) -> str | None:
    """Идентификатор события: `profile_event_id` первым, затем ep и легаси-ключи."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        body.get("profile_event_id"),
        props.get("profile_event_id"),
        body.get("event_id"),
        body.get("id"),
    )


def parse_event_type(body: dict[str, Any]) -> str:
    """Тип события в lower-case; читается из нескольких ключей (позиция не гарантирована)."""
    props = _as_dict(body.get("event_properties"))
    raw = _first_str(
        body.get("event_type"),
        body.get("event"),
        props.get("event_type"),
        body.get("type"),
    )
    return raw.lower() if raw is not None else ""


def parse_customer_user_id(body: dict[str, Any]) -> uuid.UUID | None:
    """`customer_user_id` — то, что iOS передал в `Adapty.identify` (на практике deviceId).

    Порядок источников: `customer_user_id` → `profile.customer_user_id` →
    `event_properties.customer_user_id` → `user_id`. Отсутствующее или не-UUID значение
    вызывающий трактует как `missing_customer_user_id`.
    """
    profile = _as_dict(body.get("profile"))
    props = _as_dict(body.get("event_properties"))
    raw = _first_str(
        body.get("customer_user_id"),
        profile.get("customer_user_id"),
        props.get("customer_user_id"),
        body.get("user_id"),
    )
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def parse_vendor_product_id(body: dict[str, Any]) -> str | None:
    """`vendor_product_id` — ключ каталога `products.external_product_id`."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        props.get("vendor_product_id"),
        props.get("product_id"),
        body.get("vendor_product_id"),
        body.get("product_id"),
    )


def parse_expires_at(body: dict[str, Any]) -> datetime.datetime | None:
    """ISO8601 → tz-aware datetime; неразбираемое → None (событие всё равно применяется)."""
    props = _as_dict(body.get("event_properties"))
    profile = _as_dict(body.get("profile"))
    raw = _first_str(
        props.get("subscription_expires_at"),
        props.get("expires_at"),
        body.get("subscription_expires_at"),
        body.get("expires_at"),
        profile.get("expires_at"),
    )
    if raw is None:
        return None
    # Хвостовой 'Z' нормализуем явно (не полагаемся на версию fromisoformat).
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def parse_transaction_id(body: dict[str, Any]) -> str | None:
    """`transaction_id` — уникален на период оплаты (первичный ключ идемпотентности гранта)."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("transaction_id"), body.get("transaction_id"))


def parse_original_transaction_id(body: dict[str, Any]) -> str | None:
    """`original_transaction_id` — постоянен на всю цепочку подписки (fallback идемпотентности)."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(
        props.get("original_transaction_id"), body.get("original_transaction_id")
    )


def parse_is_active(body: dict[str, Any]) -> bool | None:
    """`is_active` — строго JSON-boolean, иначе None."""
    props = _as_dict(body.get("event_properties"))
    return _first_bool(props.get("is_active"), body.get("is_active"))


def parse_access_level_id(body: dict[str, Any]) -> str | None:
    """`access_level_id` (напр. `"premium"`) — для условного access_level_updated."""
    props = _as_dict(body.get("event_properties"))
    return _first_str(props.get("access_level_id"), body.get("access_level_id"))


def parse_will_renew(body: dict[str, Any]) -> bool | None:
    """`will_renew` — строго JSON-boolean, иначе None. Пишется в `subscription_state`."""
    props = _as_dict(body.get("event_properties"))
    return _first_bool(props.get("will_renew"), body.get("will_renew"))


def classify_event(event: ParsedAdaptyEvent) -> str:
    """Семантика события: `SEM_GRANTING` | `SEM_EXPIRING` | `SEM_NOOP`.

    Сюда попадают только события из `KNOWN_EVENTS` (неизвестные отсеиваются в `handle`).
    `access_level_updated` условное: premium+active → granting, inactive → expiring, иначе
    (active-но-не-premium или `is_active` не пришёл) → noop, доступ НЕ отзываем.
    """
    event_type = event.event_type
    if event_type in GRANTING_EVENTS:
        return SEM_GRANTING
    if event_type in EXPIRING_EVENTS:
        return SEM_EXPIRING
    if event_type in NOOP_EVENTS:
        return SEM_NOOP
    # CONDITIONAL_EVENTS == {"access_level_updated"}.
    if event.is_active is True and event.access_level_id == ACCESS_LEVEL_PREMIUM:
        return SEM_GRANTING
    if event.is_active is False:
        return SEM_EXPIRING
    return SEM_NOOP

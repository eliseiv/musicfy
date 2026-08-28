"""Двухступенчатый резолв идентификатора из платёжного вебхука → наш `users.id` (ADR-019).

Adapty присылает в `customer_user_id` то, что клиент передал в `Adapty.identify(...)`. На
практике iOS передаёт туда **deviceId**, а не наш `userId`, поэтому проверка только по `users`
роняла бы реальные оплаты в `user_not_found`. Связь deviceId → userId живёт в нашей таблице
`auth_identities` (`provider ∈ {guest, device}`, `subject` = device-id).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RESOLVED_VIA_USER_ID = "user_id"
RESOLVED_VIA_DEVICE_ID = "device_id"


async def resolve_webhook_user(
    session: AsyncSession, external_id: uuid.UUID
) -> tuple[uuid.UUID, str] | None:
    """Резолвит идентификатор из вебхука в наш `userId`. First-match wins, детерминированно.

    * (a) `external_id` есть в `users` → это уже наш userId; `resolved_via = "user_id"`.
    * (b) иначе `lower(external_id)` совпал с `lower(auth_identities.subject)` для
      provider ∈ {guest, device} → берём связанный `user_id`; `resolved_via = "device_id"`.
    * (c) иначе → `None` (вызывающий отвечает `user_not_found`).

    Пользователи здесь НИКОГДА не создаются: вебхук не является источником регистрации.

    Регистр сравнения. `auth_identities.subject` — `VARCHAR`, куда device-id пишется в
    клиентском регистре: iOS отдаёт `identifierForVendor.uuidString` в UPPERCASE, а
    `str(uuid.UUID)` в Python всегда lowercase. Точное сравнение потеряло бы ровно тех
    пользователей, ради которых ветка (b) и существует, поэтому сравниваем через `lower()`.

    `ORDER BY user_id LIMIT 1` — детерминизм при гипотетической коллизии регистра (две строки
    с одинаковым `lower(subject)`, но разным исходным написанием). Уникальность в схеме —
    `(provider, subject)`, то есть регистровая коллизия формально возможна; это деньги, и
    молчаливый произвольный выбор получателя недопустим.

    Фильтр по provider намеренный: `apple`-identity содержит в `subject` Apple `sub`, а не
    device-id, и не должен участвовать в резолве устройства.
    """
    if await session.scalar(
        text("SELECT 1 FROM users WHERE id = :x"),
        {"x": str(external_id)},
    ):
        return external_id, RESOLVED_VIA_USER_ID

    device_user_id = await session.scalar(
        text(
            "SELECT user_id FROM auth_identities "
            "WHERE lower(subject) = :x AND provider IN ('guest', 'device') "
            "ORDER BY user_id LIMIT 1"
        ),
        {"x": str(external_id)},
    )
    if device_user_id is not None:
        return uuid.UUID(str(device_user_id)), RESOLVED_VIA_DEVICE_ID

    return None

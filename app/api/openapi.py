"""Метаданные OpenAPI: описание API и теги. Документация для iOS-команды."""
from __future__ import annotations

API_DESCRIPTION = """
Backend приложения **Musicfy** — единый API над fal.ai для генерации музыки,
AI-каверов, клонирования голоса и AI-видеоклипов.

---

## Авторизация

Все защищённые эндпоинты используют **Bearer-токен сессии**.

1. Гость: `POST /v1/auth/guest` → вернёт `token`.
2. Apple: `POST /v1/auth/apple` (с `identityToken`) → вернёт `token`; гостевые
   данные автоматически переносятся в постоянный аккаунт.
3. Передавайте токен в заголовке: `Authorization: Bearer <token>`.

В Swagger UI нажмите **Authorize** и вставьте токен.

---

## Жизненный цикл генерации (song / cover / video)

Генерация — асинхронная. Ждать на экране не нужно.

1. `POST /v1/songs` (или `/v1/covers`, `/v1/videos`) → **202** с `jobId`.
2. Опрашивайте `GET /v1/jobs/{jobId}` — поле `status` проходит стадии:
   `created → queued → running → post_processing → completed | failed | canceled`.
   Поле `pipeline` показывает прогресс по стадиям.
3. При `completed`:
   - song/cover → `trackId` → `GET /v1/tracks/{trackId}` (массив `variants` с `audioUrl`).
   - video → `GET /v1/videos/{jobId}` (`videoUrl`).
4. По завершении долгих задач (видео) приходит push-уведомление (APNs).

`lyrics` и клонирование голоса — синхронные (результат сразу в ответе).

---

## Монеты и подписка

У пользователя единый баланс **монет** (coins) — без деления на song/cover/video.

- Каждая генерация стоит фиксированное число монет по прайс-листу
  `GET /v1/billing/pricing`: **song = 10**, **cover = 5**, **video = 30**.
  `lyrics` и `voice clone` бесплатны (монеты не тратят).
- Текущий баланс: `GET /v1/billing/balance` (`coins_available`, `coins_reserved`).
- Продукты (`GET /v1/billing/products`) — пакеты монет (`coin_pack`) и подписки;
  оба начисляют монеты на единый баланс. Монеты копятся бессрочно и не сгорают.
- Списание идёт по схеме **reserve → capture**: при создании задачи цена
  резервируется, при успехе — списывается, при провале — возвращается
  (release / refund).
- При нехватке монет создание задачи вернёт **402 `INSUFFICIENT_CREDITS`** —
  это сигнал показать paywall.
- Покупки идемпотентны по дедуп-ключу транзакции (environment-scoped `dedup_key`,
  ADR-013): повторный verify/restore **своего** чека не начислит монеты дважды;
  replay **чужого** чека → `rejected` / `transaction_already_claimed`.

---

## Формат ошибок

Все 4xx/5xx ответы имеют единый вид:

```json
{
  "error": {
    "code": "INSUFFICIENT_CREDITS",
    "message": "Not enough generation credits to perform the operation",
    "details": { "required": 10, "available": 3 }
  },
  "requestId": "b5830b11dc4747d4b6b85217eff10177"
}
```

Частые коды: `UNAUTHORIZED`, `INVALID_SESSION`, `INVALID_INPUT`,
`SUBSCRIPTION_REQUIRED`, `INSUFFICIENT_CREDITS`, `CONSENT_REQUIRED`,
`MODERATION_BLOCKED`, `JOB_NOT_FOUND`, `TRACK_NOT_FOUND`, `UPLOAD_REJECTED`,
`PROVIDER_FAILED`, `PROVIDER_TIMEOUT`, `RATE_LIMITED`.
"""

OPENAPI_TAGS = [
    {"name": "Авторизация", "description": "Guest-вход, Sign in with Apple, текущий пользователь."},
    {"name": "Пресеты", "description": "Каталог жанров, настроений и промпт-пресетов для формы генерации."},
    {"name": "Текст песни", "description": "Генерация и редактирование lyrics (синхронно)."},
    {"name": "Песни", "description": "Создание песни (text-to-song / lyrics-to-song)."},
    {"name": "Кавер", "description": "AI-кавер из загруженного аудио (разделение дорожек + смена голоса)."},
    {"name": "Голоса", "description": "Согласие на голос, клонирование, библиотека голосов."},
    {"name": "Видео", "description": "AI music video (lipsync). Самая долгая операция."},
    {"name": "Загрузка", "description": "Загрузка пользовательского аудио/видео (multipart) → Asset."},
    {"name": "Задачи", "description": "Унифицированный статус задачи генерации и её стадий."},
    {"name": "Треки", "description": "Готовые треки и их варианты."},
    {"name": "Библиотека", "description": "Медиатека пользователя: треки, видео, голоса."},
    {"name": "Биллинг", "description": "Баланс, каталог продуктов, покупки, restore, журнал кредитов."},
    {"name": "Устройства", "description": "Регистрация APNs push-токена."},
    {"name": "Аналитика", "description": "События воронки и правовые уведомления."},
    {"name": "Админ", "description": "Начисление кредитов и подписки. Доступ по ключу ADMIN_API_KEY."},
    {"name": "Webhooks", "description": "Серверные коллбэки (fal, App Store). Не для клиента."},
    {"name": "Система", "description": "Служебные эндпоинты (healthcheck)."},
]

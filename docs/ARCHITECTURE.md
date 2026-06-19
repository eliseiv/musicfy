# Архитектура musicfy-backend

## Назначение

Orchestration-слой над fal.ai. Клиент (iOS) никогда не обращается к fal напрямую — все генерации
проходят через наш единый API, который контролирует доступ, лимиты/кредиты, очерёдность долгих задач,
агрегацию результата и модерацию.

## Слои

```
app/
  api/v1/        — HTTP-эндпоинты (тонкие, без бизнес-логики)
  auth/          — Sign in with Apple, guest/device-сессии (opaque-токены)
  middleware/    — request-context (X-Request-Id), rate limit
  db/            — async engine / sessionmaker
  models/        — Base + TimestampMixin
  domain/
    enums.py     — все доменные перечисления (JobType, JobStatus, CreditCategory, ...)
    models/      — ORM-модели (агрегаты)
    repositories/— data access (по агрегату)
    schemas/     — pydantic DTO (camelCase наружу)
    providers/   — внешние интеграции (fal, billing/apple, push/apns)
    services/    — бизнес-логика (pipelines, credits, lyrics, moderation, ...)
    seed/        — сидеры справочников (presets, pricing, products)
```

## Идентичность и сессии

- `User` — единственная сущность пользователя; `is_guest` различает гостя и постоянного.
- `AuthIdentity(provider, subject)` — внешние идентичности: `apple` (sub из identity token),
  `guest`/`device`. Уникальна по `(provider, subject)`.
- Сессии — opaque-токены: клиенту выдаётся случайный токен, в БД хранится только его SHA-256
  (`sessions.token_hash`). Резолв сессии → `User`.
- **Guest → Apple merge** (ТЗ 5.6): при входе через Apple поверх guest-сессии данные guest
  переносятся на постоянный аккаунт. Точка расширения — `app.auth.sessions.MERGE_REASSIGNERS`:
  каждая доменная область (credits, jobs, library, voice) регистрирует свою reassign-корутину.

## Генерация: единый Job + пайплайны

Все тяжёлые операции — это `Job` с полем `job_type` (`song` / `lyrics` / `cover` / `voice_clone` /
`video`). Прогресс задачи фиксируется в `job_stage_log` (по стадии на строку) и в `job.current_stage`
(последняя запущенная async-стадия — нужна для идемпотентной обработки webhook).

Статусы задачи (ТЗ 12): `created → queued → running → post_processing → completed | failed | canceled`.

Обработка асинхронных стадий:
1. submit в fal queue API с `fal_webhook=<PUBLIC_BASE_URL>/v1/webhooks/fal` и `X-Idempotency-Key`.
2. Результат приходит webhook'ом (идемпотентность через `processed_webhooks`, 2 фазы:
   `received` → `applied`), либо подхватывается `FalPoller` (fallback-поллинг очереди fal).
3. Pipeline переходит к следующей стадии или финализирует задачу.

Отдельный класс-pipeline на тип задачи под общим ABC (`services/pipelines/base.py`); диспетчер по
`job_type`.

### Контракт интеграции fal.ai (форматы результата)

Два пути доставки результата отдают данные модели в **разной форме** — единый парсер
`parse_fal_webhook_event` (`domain/providers/fal/parsing.py`) учитывает оба. Провайдеры вызывают его
через метод-обёртку `parse_webhook_event` (`FalAiProvider` — `client.py`, `StubFalProvider` —
`stub.py`), который делегирует в этот модульный парсер:

- **Webhook (fal queue, основной путь).** fal queue доставляет результат в **конверте**:

  ```json
  {"request_id": "...", "gateway_request_id": "...", "status": "OK", "payload": {<результат модели>}, "error": null}
  ```

  Единый парсер `parse_fal_webhook_event` (`domain/providers/fal/parsing.py`) извлекает результат как
  `payload || result || output` (`payload` — первичный источник; `result`/`output` — fallback для
  обратной совместимости со старым форматом). `error_message` берётся с **верхнего уровня** конверта
  (`error`), а не из распакованного результата. Оба провайдера (`FalAiProvider` и `StubFalProvider`)
  обязаны вызывать этот модульный парсер; `parse_webhook_event` — лишь метод-обёртка провайдеров над ним.
  Из распакованного результата функция `extract_media` достаёт `media_url` (`audio.url` / `video.url`,
  с fallback на `audio_url` / `video_url`) **и** длительность `duration` (`duration` / `duration_seconds`);
  отдельно извлекаются `stems` (если это dict).

- **Прямой result-эндпоинт (poll-путь `FalPoller` / `fetch_status`, fallback).** Отдаёт
  **распакованный** результат без конверта (например `{"audio": {...}}`). На случай конвертного
  ответа есть защитная распаковка `payload`-dict.

> Эта разница форматов — контракт интеграции, обязательный к соблюдению при добавлении новых fal-моделей:
> отсутствие распаковки `payload` приводило к `media_url=None` и ложному `succeeded` стадии (см.
> [TD-002](./100-known-tech-debt.md#td-002)).

> **Обработка error-конверта (status `ERROR`) — известное ограничение.** При статусе `"ERROR"`
> `parse_fal_webhook_event` бросает `WebhookPayloadInvalid(reason=unknown_status)` (статус не входит в
> whitelist), а не маппит job-стадию в `failed`. Терминальный `failed`-статус задачи обеспечивает
> поллер-fallback (`FalPoller`, `POLL_ENABLED=true`). Подробности и план закрытия — см.
> [TD-003](./100-known-tech-debt.md#td-003).

## Кредиты и лимиты

Категорийная модель (решение по ТЗ):
- `Entitlement(category, granted, used, period)` — подписочные лимиты по категориям song/cover/video,
  периодные, сгорают. Грантятся при подписочном billing-событии.
- `CreditBalance(category, available, reserved)` — покупные кредиты (паки), non-expiring.
- Порядок списания: сначала entitlement категории, затем purchased credits. Reserve → capture → release
  симметрично, всё пишется в `credit_ledger` (audit).
- `lyrics` и `voice_clone` не списывают генерационные кредиты (lyrics дешёвый LLM; voice_clone —
  подготовительный шаг).

## Биллинг

Прямой StoreKit 2: `providers/billing/apple.py` (верификация подписанных JWS-транзакций через App Store
Server API), webhook `POST /v1/webhooks/billing/apple` (App Store Server Notifications V2). Подписки →
гранят entitlements; паки → кредитуют credit_balances. Restore — через `original_transaction_id`.

## Хранилище ассетов

fal storage (`upload_to_storage`): два шага initiate → PUT. Единая таблица `assets` хранит ссылки на
загруженные и сгенерированные медиа (audio/video/voice_sample/source_video/stem).

## Локальные особенности окружения

- Требуется Python 3.12 (project requires-python `>=3.12,<3.13`).
- Если на хосте занят порт 5432 (нативный Postgres) — поднимать контейнер через
  `PG_HOST_PORT=5544 docker compose up -d postgres` и указывать порт в `DATABASE_URL`.

## Развёртывание

Продовая топология (общий Traefik, сеть `web`, сервисы `api`/`postgres`), домены и TLS,
CI/CD flow, секреты, процедуры деплоя и отката — описаны в [DEPLOYMENT.md](./DEPLOYMENT.md).
Ключевые инфраструктурные решения — в [adr/INDEX.md](./adr/INDEX.md).

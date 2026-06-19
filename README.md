# musicfy-backend

Backend нового iOS-приложения **musicfy** — orchestration-слой над [fal.ai](https://fal.ai) для
генерации музыки, AI cover-треков, клонирования голоса и AI music video.

Стек: Python 3.12 · FastAPI (async) · SQLAlchemy 2.0 + asyncpg · PostgreSQL 16 · Alembic · httpx.
Долгие задачи обрабатываются через fal queue API (webhooks) + asyncio-poller (fallback), без отдельной
очереди/Redis.

См. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) для обзора архитектуры, сущностей и пайплайнов.

## Быстрый старт (локально)

```bash
# 1. Виртуальное окружение + зависимости (нужен Python 3.12)
py -3.12 -m venv .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"

# 2. Postgres в Docker. Если порт 5432 на хосте занят — задайте свой:
PG_HOST_PORT=5544 docker compose up -d postgres

# 3. Конфигурация
cp .env.example .env   # отредактируйте DATABASE_URL/FAL_* при необходимости

# 4. Миграции
DATABASE_URL="postgresql+asyncpg://musicfy:musicfy@localhost:5544/musicfy" \
  .venv/Scripts/python.exe -m alembic upgrade head

# 5. Запуск
DATABASE_URL="postgresql+asyncpg://musicfy:musicfy@localhost:5544/musicfy" \
  .venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Полный стек (api + postgres) поднимается через `docker compose up`.

## Тесты

```bash
DATABASE_URL="postgresql+asyncpg://musicfy:musicfy@localhost:5544/musicfy" \
  .venv/Scripts/python.exe -m pytest
```

Тесты используют живой Postgres (из docker-compose) и in-process stub fal-провайдера
(`FAL_USE_STUB=true`), поэтому реальный ключ fal для них не нужен.

## Линтинг

```bash
.venv/Scripts/python.exe -m ruff check app
```

## Статус реализации (V1 MVP — завершён + хардненинг)

- [x] Фаза 0 — инфраслой (config, db, errors, middleware, fal-провайдер, миграции)
- [x] Фаза 1 — auth (Sign in with Apple + guest + merge, opaque-сессии)
- [x] Фаза 2 — song generation (minimax-music/v2.6) + lyrics (LLM) + presets
- [x] Фаза 3 — credits / entitlements / StoreKit 2 + ledger
- [x] Фаза 4 — AI cover (demucs → voice-changer → mix)
- [x] Фаза 5 — voice clone + consent
- [x] Фаза 6 — AI music video (kling lipsync) + APNs push
- [x] Фаза 7 — moderation + analytics + library + legal notices

8 миграций, 36 интеграционных тестов (pytest), 32 API-эндпоинта.

### Реальный fal.ai E2E — пройден
- lyrics (LLM) и song (minimax-music/v2.6) проверены на живом fal через polling.
- Песня генерируется ~90с, отдаётся реальный `audioUrl` + длительность.

### Production-хардненинг (закрыто)
- [x] **Верификация подписи StoreKit** — x5c-цепочка до Apple Root CA - G3 + ES256
  (`providers/billing/apple.py`, `apple_certs.py`). Подделка транзакций невозможна.
- [x] **Верификация fal webhook** — ED25519 + JWKS fal + анти-replay по timestamp
  (`providers/fal/signature.py`). HMAC оставлен для dev/stub.
- [x] **APNs** — реальная отправка HTTP/2 (token-based ES256), `APNS_ENABLED`.
- [x] **Swagger/OpenAPI** — чистая документация + примеры; экспорт `docs/openapi.json`.
- [x] **Security-заголовки** (`SecurityHeadersMiddleware`), ранний guard размера загрузки.
- [x] **CI-черновик** — `.github/workflows/ci.yml` (ruff + миграции + pytest на Postgres).

### Осталось (на утро / по мере доступа к данным)
- Включить CI, настроить домен → `PUBLIC_BASE_URL` (для fal webhook вместо polling).
- Реальные ключи Apple: StoreKit (Issuer/Key/.p8) и APNs (.p8) — заполнить в `.env`.
- Замена базового блок-листа модерации на внешний провайдер.
- Метрики/трейсинг (Prometheus/OTel) и алерты на failure-rate.
- Для нескольких инстансов: leader-election поллера или вынос в Redis/Arq.

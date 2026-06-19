# ADR-001 — Fail-fast пароль БД в prod (без дефолта)

- Статус: Accepted
- Дата: 2026-06-19
- Контекст: prod-деплой musicfy за общим Traefik

## Context

В dev-compose (`docker-compose.yml`) `POSTGRES_PASSWORD` и пароль в `DATABASE_URL` имеют
захардкоженное значение `musicfy` — это удобно для локальной разработки. В prod такое значение
недопустимо: общий сервер несёт несколько сервисов, слабый/дефолтный пароль БД — прямой риск.
Нужно гарантировать, что prod-стек физически не стартует с небезопасным паролем по умолчанию.

## Decision

В `docker-compose.prod.yml` `POSTGRES_PASSWORD` и пароль в `DATABASE_URL` задаются через
обязательную подстановку `${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}` — **без дефолта**.
Значение приходит только из `/opt/musicfy/.env` (chmod 600, вне git). Если переменная не задана,
`docker compose` завершает запуск с явной ошибкой.

`DATABASE_URL` для `api` собирается в compose из этого же пароля
(`postgresql+asyncpg://musicfy:${POSTGRES_PASSWORD}@postgres:5432/musicfy`), чтобы значение
не могло разойтись с паролем сервиса `postgres`.

## Consequences

- (+) Невозможно случайно поднять prod с дефолтным/пустым паролем БД — fail-fast при старте.
- (+) Единый источник пароля; нет рассинхронизации между `postgres` и `DATABASE_URL`.
- (−) `.env` с `POSTGRES_PASSWORD` обязан существовать на сервере до первого `up`; это часть
  процедуры первичной настройки (заводит владелец).

## Alternatives

- **Дефолт как в dev** — отклонено: небезопасно на общем сервере.
- **Docker/compose secrets** — отклонено сейчас: избыточно для одного `.env`-файла на одной
  машине; усложняет деплой без выигрыша в текущем масштабе.

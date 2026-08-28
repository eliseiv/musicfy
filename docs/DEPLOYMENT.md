# Deployment — musicfy

Документ описывает продовую топологию сервиса `musicfy` (FastAPI backend), уже развёрнутого
в prod. Источник истины по инфраструктуре. При расхождении с реальными артефактами
(`docker-compose.prod.yml`, `.github/workflows/deploy.yml`, `entrypoint.sh`, `Dockerfile`)
— расхождение является дефектом и подлежит исправлению.

Деплой подчинён результату CI: `deploy.yml` триггерится завершением workflow «CI» и
выполняется только при зелёном CI на push в `main` (подробности — §4, обоснование — ADR-004).

Связанные документы: [ARCHITECTURE.md](./ARCHITECTURE.md), ADR [adr/INDEX.md](./adr/INDEX.md).

---

## 1. Топология

Сервис работает на общем Linux-сервере за общим reverse-proxy Traefik. `musicfy` управляет
только своим стеком (`/opt/musicfy`); общий edge (`/opt/edge`) и чужие сервисы — вне его зоны.

- **Сервер:** `87.239.135.154`, Ubuntu 22.04, каталог сервиса `/opt/musicfy`.
- **Edge / reverse-proxy:** общий Traefik `traefik:v3.3` (каталог `/opt/edge`, контейнер
  `edge-traefik-1`). Терминирует TLS, выпускает/продлевает Let's Encrypt (certresolver `le`),
  роутит трафик по доменам. Конфиги Traefik сервис `musicfy` **не трогает** — интеграция только
  через docker labels на сервисе `api`.
- **Сети:**
  - `web` — внешняя (`external: true`) docker-сеть, общая для Traefik и всех сервисов за ним.
    Через неё Traefik видит `api`. Создаётся один раз (`docker network create web`); deploy.yml
    идемпотентно её создаёт, если отсутствует.
  - `default` — внутренняя сеть стека `musicfy`, изолирует `api ↔ postgres`. `postgres`
    подключён только к `default` и не доступен извне.
- **Сервисы стека `musicfy`:**
  - `api` (контейнер `musicfy-api-1`) — FastAPI/uvicorn, слушает `8000` внутри контейнера,
    наружу хост-порты **не публикуются** (`expose: 8000`). Подключён к `web` и `default`.
  - `postgres` (контейнер `musicfy-postgres-1`) — `postgres:16-alpine`, только сеть `default`,
    данные в volume `pgdata`, хост-порты не публикуются.

> **Имена контейнеров неявны.** В compose нет `container_name` — имена (`musicfy-api-1`,
> `musicfy-postgres-1`) формируются из имени compose-проекта, равного имени каталога
> `/opt/musicfy`. Health gate в `deploy.yml` (§4) опрашивает контейнер по имени
> `musicfy-api-1`, поэтому переименование каталога сервиса на сервере молча сломает health gate.

```mermaid
flowchart TB
  client["iOS client"]
  dns["DNS: zavionix.shop / www.zavionix.shop<br/>A → 87.239.135.154"]
  client -->|HTTPS 443| dns

  subgraph server["Сервер 87.239.135.154 (Ubuntu 22.04)"]
    subgraph edge["/opt/edge (общий, вне зоны musicfy)"]
      traefik["edge-traefik-1<br/>traefik:v3.3<br/>TLS + Let's Encrypt (le)"]
    end

    subgraph stack["/opt/musicfy (docker-compose.prod.yml)"]
      api["api — musicfy-api-1<br/>FastAPI :8000 (expose)<br/>healthcheck /healthz"]
      pg["postgres — musicfy-postgres-1<br/>postgres:16-alpine<br/>volume pgdata"]
    end

    traefik -->|"net: web"| api
    api -->|"net: default :5432"| pg
  end

  dns --> traefik

  classDef ext fill:#eee,stroke:#999;
  class edge ext;
```

Маршрутизация Traefik задаётся labels на сервисе `api`:

| label | значение |
|---|---|
| router/service name | `musicfy` |
| `routers.musicfy.rule` | ``Host(`zavionix.shop`) \|\| Host(`www.zavionix.shop`)`` |
| `routers.musicfy.entrypoints` | `websecure` |
| `routers.musicfy.tls.certresolver` | `le` |
| `services.musicfy.loadbalancer.server.port` | `8000` |

---

## 2. Домены и TLS

- **Домены:** `zavionix.shop` и `www.zavionix.shop`, обе A-записи → `87.239.135.154`.
- **TLS:** терминируется Traefik на entrypoint `websecure` (443). Сертификат — Let's Encrypt
  через certresolver `le`. Авто-выпуск и авто-продление выполняет Traefik (сервис `musicfy`
  в продлении не участвует).
- **Публичный base URL приложения:** `PUBLIC_BASE_URL=https://zavionix.shop` (используется,
  в частности, как адрес fal webhook — см. ARCHITECTURE.md §«Генерация»).

---

## 3. Продовый compose vs dev compose

| Аспект | `docker-compose.yml` (dev) | `docker-compose.prod.yml` (prod) |
|---|---|---|
| Назначение | локальная разработка | продакшн за Traefik |
| Host-порты `api` | `8000:8000` (публикуется) | нет (`expose: 8000`) |
| Host-порты `postgres` | `${PG_HOST_PORT:-5432}:5432` | нет |
| Сети | дефолтная (одна) | `web` (external) + `default` |
| Traefik labels | нет | да (router/service `musicfy`) |
| `POSTGRES_PASSWORD` | дефолт `musicfy` (dev-значение) | `${POSTGRES_PASSWORD:?...}` fail-fast, без дефолта |
| `DATABASE_URL` | пароль `musicfy` зашит | пароль из `${POSTGRES_PASSWORD}`, fail-fast |
| `restart` | нет | `unless-stopped` (оба сервиса) |
| env | `env_file: .env` | `env_file: .env` + override `DATABASE_URL` |

Dev-compose **остаётся** для локальной разработки и не используется на сервере.
CI-workflow `.github/workflows/ci.yml` остаётся отдельным workflow и сам по себе ничего не
деплоит, но deploy подчинён его результату: `deploy.yml` триггерится завершением CI и
запускается только при зелёном CI (см. §4).

В проде `DATABASE_URL` для `api` переопределяется в compose
(`postgresql+asyncpg://musicfy:${POSTGRES_PASSWORD}@postgres:5432/musicfy`), чтобы значение в
`.env` не могло разойтись с реальным паролем `postgres`.

---

## 4. CI/CD flow

Workflow: `.github/workflows/deploy.yml`. Это **единый источник истины** по триггеру и gating
деплоя (вместе с ADR-004); остальные разделы ссылаются сюда.

**Триггер — `workflow_run` после завершения workflow «CI»** (`on: workflow_run: workflows:
["CI"], types: [completed]`). Деплой не стартует напрямую на `push`: сначала отрабатывает CI,
и только его завершение запускает `deploy.yml`.

**Gating (job-level `if`)** — деплой выполняется только при одновременном выполнении трёх
условий:

- `github.event.workflow_run.conclusion == 'success'` — CI завершился зелёным;
- `github.event.workflow_run.head_branch == 'main'` — CI был на ветке `main`;
- `github.event.workflow_run.event == 'push'` — CI был запущен `push`'ем.

> **Поведение PR.** Прогон CI на pull request проходит штатно, но деплой при этом **НЕ
> запускается**: gate `event == 'push'` отсекает PR-прогоны CI. Деплоятся только push-коммиты
> в `main` с зелёным CI.

> **Поведение `master`.** CI (`.github/workflows/ci.yml`) запускается на push в обе ветки
> `[main, master]`, но gate `head_branch == 'main'` деплоит только `main`: push в `master`
> штатно тестируется, но намеренно **не выкатывается** (fail-safe). Ветка `master` в `ci.yml`
> — наследие шаблона.

**Деплоится протестированный коммит.** `checkout` выполняется с `ref: ${{
github.event.workflow_run.head_sha }}` — на сервер уезжает ИМЕННО тот SHA, на котором CI стал
зелёным, а не последний `main` на момент запуска деплоя.

**Concurrency-группа `deploy-${{ github.workflow }}`** с `cancel-in-progress: false`
(параллельные деплои сериализуются, текущий не прерывается). Группировка по `github.workflow`,
а не по `github.ref`: при триггере `workflow_run` `github.ref` указывает на default branch и
как ключ группировки нестабилен (обоснование — ADR-004).

Стратегия: **rsync рабочего дерева на сервер**, затем сборка/запуск по SSH. Сервер не имеет
доступа к приватному репозиторию — он не делает `git pull` (обоснование — ADR-002).

Шаги:

1. **checkout** — `actions/checkout@v4` с `ref: ${{ github.event.workflow_run.head_sha }}`
   (протестированный коммит).
2. **Setup SSH key** — приватный ключ из `SSH_PRIVATE_KEY` пишется в `~/.ssh/deploy_key`
   (chmod 600), `known_hosts` из `SSH_KNOWN_HOSTS` (chmod 644). Используется
   `StrictHostKeyChecking=yes` — host сервера должен совпадать с записью в `SSH_KNOWN_HOSTS`.
3. **Ensure shared Traefik network** — идемпотентно создать сеть `web`, если её нет.
4. **Rsync + deploy all instances** — для каждой пары `dir:project` из `$INSTANCES`:
   - проверить, что `/opt/<dir>` и `/opt/<dir>/.env` существуют (провижининг — ручной);
   - `rsync -az --delete` рабочего дерева в `/opt/<dir>/` (исключения: `.git`, `.env`,
     `.venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `.coverage`, `.idea`);
   - `docker compose -p <project> -f docker-compose.prod.yml --env-file .env up -d --build`;
   - **health gate:** до ~120 с ждать `State.Health.Status == healthy` у `<project>-api-1`.
     Если api не стал healthy — деплой падает (`exit 1`), старый образ остаётся (см. §5);
   - после успеха всех инстансов — `docker image prune -f` (обоснование — ADR-003).
5. **Cleanup SSH key** — `if: always()`, удаляет `~/.ssh/deploy_key`.

### INSTANCES-loop (мульти-инстанс)

Переменная `INSTANCES` в `deploy.yml` — список пар `dir:project`:

| dir | project | домен | каталог |
|---|---|---|---|
| `musicfy` | `musicfy` | `zavionix.shop` | `/opt/musicfy` |
| `norqelia` | `norqelia` | `norqelia.shop` | `/opt/norqelia` |

Новый инстанс добавляется в `$INSTANCES` **только после** ручного провижининга и
`GET https://<домен>/healthz → 200` (см. §Мульти-инстанс ниже).

### GitHub Secrets (заводит владелец репозитория)

| секрет | назначение |
|---|---|
| `SSH_HOST` | хост сервера (`87.239.135.154`) |
| `SSH_USER` | пользователь SSH (`root`) |
| `SSH_PRIVATE_KEY` | приватный ключ для деплой-доступа |
| `SSH_KNOWN_HOSTS` | запись known_hosts сервера (для `StrictHostKeyChecking=yes`) |

---

## 5. Секреты и конфигурация

- Секреты приложения живут **только** в `/opt/musicfy/.env` на сервере: `chmod 600`,
  вне git, **не перезаписывается** rsync (через `--exclude='.env'`).
- Обязательные значения в prod `.env`: `APP_ENV=prod`,
  `PUBLIC_BASE_URL=https://zavionix.shop`, `POSTGRES_PASSWORD=<сгенерированный hex>`
  (плюс ключи внешних провайдеров — fal, Apple/StoreKit, APNs — см. ARCHITECTURE.md).
- `POSTGRES_PASSWORD` обязателен: при его отсутствии compose падает с явной ошибкой
  (`${POSTGRES_PASSWORD:?...}`), а не стартует с дефолтным паролем (обоснование — ADR-001).
- **StoreKit fail-fast (ADR-013 §D3):** прод обязан иметь `APPLE_STOREKIT_VERIFY_SIGNATURE=true`.
  `APP_ENV=prod` + `false` → приложение **не поднимается** (проверка в `Settings`,
  `config.py`) — без проверки подписи любой аутентифицированный пользователь подделает
  неподписанный JWS и намайнит монеты. Инвариант защищён fail-fast'ом, не полагается на дисциплину.
- **`APPLE_STOREKIT_TEST_ROOT_CERTS` (ADR-013 §D3, [Q-BILL-1](./99-open-questions.md#q-bill-1)):**
  конкатенация PEM корневых сертификатов Xcode StoreKit Test. **Дефолт пустой = Xcode-покупки на
  проде выключены** (принимаются только Apple-подписанные `Production`/`Sandbox`). Владелец решил
  включать пин тестового корня разработчика на проде для отладки покупок; это осознанный аудируемый
  акт (пин корня конкретной машины), а **перед реальным запуском корень удаляется**. Смена
  машины/Xcode → обновить env + редеплой ([TD-010](./100-known-tech-debt.md#td-010)).
- **`APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS` (ADR-014, [Q-BILL-1](./99-open-questions.md#q-bill-1)):**
  CN-trust Xcode тест-сертификатов за флагом. **Дефолт `false` = прод строгий** (поведение ADR-013).
  `true` + `VERIFY_SIGNATURE=true` → доверять любому self-signed EC-сертификату с
  `CN="StoreKit Testing in Xcode"` → `environment=Xcode` **без** пина по DER (масштабируется на
  нескольких тестеров, где `APPLE_STOREKIT_TEST_ROOT_CERTS` не тянет из-за уникального DER машины).
  **Флаг легален в prod (fail-fast НЕТ)** — тестеры бьют по прод-бэкенду. **РИСК:** при `true` любой
  намайнит коины бесплатно себе (per-user; чужой баланс/боевой namespace недостижимы). Приемлемо
  **только** в Testing-режиме. **Перед публичным релизом выключить** — см. §8.
- В образ и репозиторий секреты не попадают: `.env` исключён из rsync и из git.

---

## 6. Процедуры эксплуатации

### Деплой

Обычный путь — `git push` в `main`. Дальше срабатывает gate: push → CI → (если CI зелёный на
push в `main`) → deploy протестированного SHA. Сам деплой (`deploy.yml`) выполняется
автоматически (rsync → build → health gate → prune). Подробности триггера и gating — §4.
Ручной деплой на сервере не предусмотрен как штатный.

### Rollback

Текущая стратегия отката — **повторный деплой предыдущего коммита**:

1. На локальной машине/в репозитории откатить состояние `main` на предыдущий рабочий коммит
   (`git revert <bad>` — предпочтительно, либо `git checkout <good> -- .` + commit) и `git push`.
2. После `push` отката прогоняется CI; только при зелёном CI деплоится протестированный SHA
   состояния отката (gate по §4).
3. Образ предыдущей (рабочей) версии сохраняется до подтверждения health нового деплоя:
   `docker image prune -f` выполняется только после прохождения health gate, поэтому
   проваленный деплой не удаляет рабочий образ.

> Откат БД-миграций автоматически не выполняется. Миграции применяются `alembic upgrade head`
> в `entrypoint.sh` до старта uvicorn (см. §7). Несовместимые с предыдущей версией изменения
> схемы требуют ручного down-migration — это известное ограничение текущей стратегии отката
> (см. [100-known-tech-debt.md](./100-known-tech-debt.md) TD-001).

### Эксплуатационные запреты

- ❌ Не хранить в `/opt/musicfy/` серверные файлы, отсутствующие в репозитории (кроме `.env`):
  `rsync --delete` сотрёт их при следующем деплое. Всё, что должно жить на сервере постоянно
  и не быть в git, — только `.env`.
- ❌ Не трогать `/opt/edge` и конфиги общего Traefik; не вмешиваться в чужие сервисы на сервере.
- ❌ Не публиковать host-порты 80/443 со стороны `musicfy` — TLS/80/443 владеет общий Traefik.
- ❌ Не менять конфигурацию Docker daemon (требуется `DOCKER_MIN_API_VERSION=1.24` для Traefik).

---

## 7. Образ и старт контейнера

- **Dockerfile:** multi-stage (`python:3.12-slim`). builder ставит проект через
  `pip install --prefix=/install .`; runtime копирует `/install`, добавляет `curl` и `ffmpeg`,
  запускается от непривилегированного пользователя `app`. `EXPOSE 8000`, встроенный
  `HEALTHCHECK` на `/healthz`.
- **entrypoint.sh:** `alembic upgrade head` (миграции применяются до старта приложения),
  затем `uvicorn app.main:app --host "${HTTP_HOST:-0.0.0.0}" --port "${HTTP_PORT:-8000}" --workers "${UVICORN_WORKERS:-1}"`.
  Host/port/workers переопределяемы через env-переменные `HTTP_HOST` / `HTTP_PORT` /
  `UVICORN_WORKERS`; дефолты `0.0.0.0` / `8000` / `1` соответствуют `expose: 8000` и labels Traefik.
- **Healthcheck приложения:** `GET /healthz` (используется и в Dockerfile, и в compose,
  и в CI health gate, и в Traefik loadbalancer).

---

## 8. Pre-launch чеклист (перед публичным релизом в App Store)

Пока приложение в **Testing-режиме** (не опубликовано), для отладки покупок у тестеров на проде
включены послабления StoreKit-верификации. **Перед публичным релизом их обязательно снять** —
иначе на боевом контуре останется сознательно принятая дыра «намайнить коины бесплатно себе»
([ADR-014](./adr/ADR-014-storekit-cn-trust-xcode-flag.md), риск per-user). Чеклист (в `.env` на
сервере + редеплой):

- [ ] **`APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS=false`** (или удалить строку) — выключить CN-trust
      Xcode-сертификатов ([ADR-014](./adr/ADR-014-storekit-cn-trust-xcode-flag.md)). Это главный
      пункт: флаг легален в prod и **не** защищён fail-fast'ом, поэтому его никто не выключит
      автоматически.
- [ ] **`APPLE_STOREKIT_TEST_ROOT_CERTS=`** (пусто) — убрать DER-пины тестовых корней
      ([ADR-013 §D3](./adr/ADR-013-storekit-dedup-environment-scoping.md), [TD-010](./100-known-tech-debt.md#td-010)).
- [ ] **`APPLE_STOREKIT_VERIFY_SIGNATURE=true`** — подтвердить (инвариант ADR-013, защищён
      fail-fast'ом; здесь — контрольная сверка).
- [ ] **Зачистить тестовые начисления** `purchases.environment != 'Production'` (Xcode/Sandbox) +
      компенсирующие ledger-записи перед финансовой отчётностью
      ([TD-010](./100-known-tech-debt.md#td-010)).
- [ ] **Редеплой** и проверка: Xcode-транзакция на проде теперь получает `untrusted_root` (ветка
      выключена), Apple-подписанные `Production` — начисляются.

После первых двух пунктов Xcode-ветка на проде полностью выключена — принимаются только
Apple-подписанные `Production`/`Sandbox` (строгое боевое поведение).

---

## 9. Мульти-инстанс / клонирование

Модель как у claude-ios: один репозиторий, несколько каталогов `/opt/<dir>` на общем
сервере за общим Traefik. Каждый инстанс — полный стек `api + postgres` с префиксом
compose-project `<project>`, свой volume `pgdata`, свой `.env`. Общее — только сеть `web`
и edge Traefik (`/opt/edge`).

### Параметры compose

| переменная | назначение | дефолт (первый инстанс) |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | имя project / Traefik router / image tag | `musicfy` |
| `SERVICE_DOMAIN` | `Host()` (+ `www.`-алиас) | `zavionix.shop` |
| `PUBLIC_BASE_URL` | публичный HTTPS URL (fal webhooks) | `https://zavionix.shop` |
| `POSTGRES_PASSWORD` | пароль БД инстанса (уникальный) | — (обязателен) |
| `API_KEY` / `ADMIN_API_KEY` | сервисный / админ-ключ (уникальные) | — |
| `FAL_WEBHOOK_SECRET` | HMAC webhook fal (уникальный) | — |
| `ADAPTY_WEBHOOK_SECRET` | bearer вебхука Adapty (уникальный, см. ниже) | — |

`FAL_API_KEY` можно разделять между инстансами (как Anthropic-ключ у claude-ios).
Apple/StoreKit/APNs — per-instance (свой bundle id / ключи).

### Постановка секрета вебхука Adapty ([ADR-019](./adr/ADR-019-adapty-subscription-webhook.md))

Adapty **не подписывает** payload, поэтому единственная защита эндпоинта — статический общий
секрет. Его генерирует оператор и прописывает в ДВУХ местах; расхождение даёт 401, пустое
значение — 500.

```bash
# 1. Сгенерировать (на сервере или локально)
SECRET=$(openssl rand -hex 32)

# 2. Записать в .env инстанса
echo "ADAPTY_WEBHOOK_SECRET=$SECRET" >> /opt/<instance>/.env

# 3. Перезапустить только api (без пересборки образа)
cd /opt/<instance>
docker compose -p <project> -f docker-compose.prod.yml --env-file .env up -d --no-build api

# 4. Проверить, что переменная доехала в контейнер
docker compose -p <project> -f docker-compose.prod.yml exec api   sh -lc 'test -n "$ADAPTY_WEBHOOK_SECRET" && echo set'
```

> ⚠️ **`-f docker-compose.prod.yml` обязателен в КАЖДОЙ команде compose на сервере.**
> Без него compose подхватит dev-файл `docker-compose.yml`, лежащий в том же каталоге. Он
> задаёт `DATABASE_URL` с dev-паролем `musicfy` и **не содержит Traefik-лейблов**, поэтому
> `up` из него пересоздаёт контейнеры так, что api падает на миграциях
> (`password authentication failed for user "musicfy"` — prod-volume хранит другой пароль), а
> Traefik теряет роутер домена и отдаёт **404** на все запросы. Проверить, из какого файла
> собран работающий контейнер:
> `docker inspect <project>-api-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'`
> — должно быть `docker-compose.prod.yml`. Восстановление:
> `docker compose -p <project> -f docker-compose.prod.yml --env-file .env up -d`
> (volume не трогать — данные в нём).

**4. Adapty Dashboard** → Integrations → Webhook:
- URL: `https://<SERVICE_DOMAIN>/v1/billing/adapty/webhook`
- Header: `Authorization: Bearer <значение $SECRET>`

Adapty при сохранении шлёт проверочный пинг с пустым телом — эндпоинт ответит
`200 {"result":"ignored","reason":"empty_body"}`, и конфигурация сохранится.

**Верификация после настройки:**

```bash
# Без токена → 401
curl -si -X POST https://<SERVICE_DOMAIN>/v1/billing/adapty/webhook -d '{}' | head -1

# С верным токеном и пустым телом → 200 ignored/empty_body
curl -s -X POST https://<SERVICE_DOMAIN>/v1/billing/adapty/webhook   -H "Authorization: Bearer $SECRET" -d ''
```

Диагностика: `401` — значения в `.env` и в Adapty разошлись; `500` — переменная пуста или не
доехала в контейнер (шаг 3 пропущен).

**Секрет — ПЕР-ИНСТАНС.** Не делить между `zavionix.shop` и `norqelia.shop`: общий секрет
означает, что компрометация одного инстанса даёт право начислять монеты во втором.

Мониторинг: записи `adapty_webhook_outcome` уровня WARNING — класс «Adapty аутентифицировался,
но монеты не доехали» (`user_not_found`, `missing_customer_user_id`, неизвестный тип события).
На них имеет смысл повесить алерт.

### Процедура провижининга клона

Шаги 1–8 **без** записи в CI; шаг 9 — только после `healthz 200`.

1. **DNS:** A-запись `<домен>` → `87.239.135.154` (до старта, для ACME).
2. **Код:** скопировать дерево первого инстанса (или rsync с runner), например:
   ```bash
   mkdir -p /opt/<dir>
   rsync -a --exclude='.env' --exclude='.git' /opt/musicfy/ /opt/<dir>/
   ```
3. **`.env`:** свежий файл (не копировать целиком чужой). Обязательно:
   ```
   APP_ENV=prod
   COMPOSE_PROJECT_NAME=<dir>
   SERVICE_DOMAIN=<домен>
   PUBLIC_BASE_URL=https://<домен>
   POSTGRES_PASSWORD=<openssl rand -hex 32>
   API_KEY=<openssl rand -hex 32>
   ADMIN_API_KEY=<openssl rand -hex 32>
   FAL_WEBHOOK_SECRET=<openssl rand -hex 32>
   # FAL_API_KEY — можно взять с musicfy; Apple/APNs — свои или пустые до релиза
   ```
   `chmod 600 /opt/<dir>/.env`
4. **Проверка compose config:**
   ```bash
   cd /opt/<dir>
   docker compose -p <dir> -f docker-compose.prod.yml --env-file .env config | grep -E "image:|Host\(|routers\."
   ```
   Ожидается: image `<dir>-api:prod`, router `<dir>`, Host(`<домен>`).
5. **Первый подъём:**
   ```bash
   docker network inspect web >/dev/null 2>&1 || docker network create web
   docker compose -p <dir> -f docker-compose.prod.yml --env-file .env up -d --build
   # дождаться healthy у <dir>-api-1
   ```
6. **Smoke:** `GET https://<домен>/healthz` → 200; `GET https://<домен>/health` → 200;
   `GET https://<домен>/v1/billing/products` → 200.
7. **Соседи не затронуты:** `docker inspect --format '{{.State.Health.Status}}' musicfy-api-1`
   остаётся `healthy`.
8. **В CI:** добавить `<dir>:<dir>` в `$INSTANCES` в `.github/workflows/deploy.yml` + строку
   в таблицу §4 INSTANCES-loop → commit/push **только после** healthz 200.

### Текущий флот

| dir | project | домен | PUBLIC_BASE_URL |
|---|---|---|---|
| musicfy | musicfy | zavionix.shop | https://zavionix.shop |
| norqelia | norqelia | norqelia.shop | https://norqelia.shop |

# Deployment — musicfy

Документ описывает продовую топологию сервиса `musicfy` (FastAPI backend), уже развёрнутого
в prod. Источник истины по инфраструктуре. При расхождении с реальными артефактами
(`docker-compose.prod.yml`, `.github/workflows/deploy.yml`, `entrypoint.sh`, `Dockerfile`)
— расхождение является дефектом и подлежит исправлению.

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
CI-workflow `.github/workflows/ci.yml` деплоем не затрагивается.

В проде `DATABASE_URL` для `api` переопределяется в compose
(`postgresql+asyncpg://musicfy:${POSTGRES_PASSWORD}@postgres:5432/musicfy`), чтобы значение в
`.env` не могло разойтись с реальным паролем `postgres`.

---

## 4. CI/CD flow

Workflow: `.github/workflows/deploy.yml`. Триггер — `push` в `main`. Concurrency-группа
`deploy-${{ github.ref }}` с `cancel-in-progress: false` (параллельные деплои одной ветки
сериализуются, текущий не прерывается).

Стратегия: **rsync рабочего дерева на сервер**, затем сборка/запуск по SSH. Сервер не имеет
доступа к приватному репозиторию — он не делает `git pull` (обоснование — ADR-002).

Шаги:

1. **checkout** — `actions/checkout@v4`.
2. **Setup SSH key** — приватный ключ из `SSH_PRIVATE_KEY` пишется в `~/.ssh/deploy_key`
   (chmod 600), `known_hosts` из `SSH_KNOWN_HOSTS` (chmod 644). Используется
   `StrictHostKeyChecking=yes` — host сервера должен совпадать с записью в `SSH_KNOWN_HOSTS`.
3. **Rsync project to server** — `rsync -az --delete` рабочего дерева в
   `${SSH_USER}@${SSH_HOST}:/opt/musicfy/`. Исключаются: `.git`, `.env`, `.venv`,
   `__pycache__`, `*.pyc`, `.pytest_cache`, `.ruff_cache`, `.coverage`, `.idea`.
   `--delete` означает, что серверный каталог приводится к состоянию репозитория
   (см. §6 — запрет хранить там не-`.env` файлы).
4. **Deploy on server** — по SSH:
   - идемпотентно создать сеть `web`, если её нет;
   - `docker compose -f docker-compose.prod.yml up -d --build`;
   - **health gate:** до ~120 с (24 попытки × 5 с) ждать `State.Health.Status == healthy`
     у `musicfy-api-1`. Если api не стал healthy — деплой падает (`exit 1`), старый образ
     остаётся (см. §5 rollback);
   - `docker image prune -f` — **только после** подтверждения health (обоснование — ADR-003).
5. **Cleanup SSH key** — `if: always()`, удаляет `~/.ssh/deploy_key`.

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
- В образ и репозиторий секреты не попадают: `.env` исключён из rsync и из git.

---

## 6. Процедуры эксплуатации

### Деплой

Обычный путь — `git push` в `main`. Дальше всё делает `deploy.yml` автоматически
(rsync → build → health gate → prune). Ручной деплой на сервере не предусмотрен как штатный.

### Rollback

Текущая стратегия отката — **повторный деплой предыдущего коммита**:

1. На локальной машине/в репозитории откатить состояние `main` на предыдущий рабочий коммит
   (`git revert <bad>` — предпочтительно, либо `git checkout <good> -- .` + commit) и `git push`.
2. CI выполнит обычный деплой этого состояния.
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

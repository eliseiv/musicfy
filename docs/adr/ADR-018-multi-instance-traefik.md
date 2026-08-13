# ADR-018 — Мульти-инстанс musicfy за общим Traefik (клоны по доменам)

- Статус: Accepted
- Дата: 2026-08-13
- Контекст: нужен второй прод-инстанс на домене `norqelia.shop` без форка репозитория;
  образец — мульти-инстанс claude-ios (`docs/07-deployment.md` §Мульти-инстанс).
- Связанные: [ADR-002](./ADR-002-rsync-deploy.md) (rsync), [ADR-003](./ADR-003-health-gate-before-prune.md)
  (health gate), [ADR-004](./ADR-004-ci-gating-before-deploy.md) (CI gating),
  [DEPLOYMENT.md](../DEPLOYMENT.md) §9.

## Контекст

Первый инстанс (`/opt/musicfy`, `zavionix.shop`) имел захардкоженные Traefik labels и
одиночный rsync в `/opt/musicfy` в `deploy.yml`. Клон требовал бы копии репо или ручного
патча labels — оба варианта расходятся с кодом в git.

## Решение

1. **Параметризация `docker-compose.prod.yml`:**
   - `COMPOSE_PROJECT_NAME` (дефолт `musicfy`) — имя project / Traefik router / image tag;
   - `SERVICE_DOMAIN` (дефолт `zavionix.shop`) — `Host()` + `www.`-алиас;
   - образ ` ${COMPOSE_PROJECT_NAME}-api:prod` для стабильного тега между rebuild.
2. **`deploy.yml` INSTANCES-loop** (`dir:project …`): rsync в `/opt/<dir>/` (с preserve `.env`),
   `docker compose -p <project> … up -d --build`, health gate на `<project>-api-1`.
3. **Провижининг клона — ручной** (как у claude-ios): DNS → дерево кода → свежий `.env` →
   первый `up --build` → smoke → только потом запись в `$INSTANCES`.
4. **Изоляция:** отдельный postgres-контейнер + volume `<project>_pgdata`, уникальные
   `POSTGRES_PASSWORD` / `API_KEY` / `ADMIN_API_KEY` / `FAL_WEBHOOK_SECRET`. `FAL_API_KEY`
   допускается общим.

Первый клон: `/opt/norqelia`, `COMPOSE_PROJECT_NAME=norqelia`, `SERVICE_DOMAIN=norqelia.shop`.

## Последствия

- (+) Один репозиторий обслуживает несколько white-label доменов.
- (+) CI/CD обновляет все зарегистрированные инстансы одним пушем.
- (−) Новый инстанс нельзя «просто добавить в INSTANCES» без ручного провижининга `.env`.
- (−) `--delete` rsync по-прежнему запрещает держать в `/opt/<dir>/` файлы вне репо (кроме `.env`).

## Альтернативы (отклонены)

- **Форк репо на каждый домен** — расхождение кода, двойной CI.
- **Один контейнер, несколько Host()** — общая БД/секреты, нет изоляции white-label.

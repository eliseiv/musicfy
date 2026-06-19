# ADR Index — musicfy

Реестр архитектурных решений. Каждый ADR неизменяем после принятия; новое решение заводится
новым ADR со ссылкой на заменяемый (status `Superseded by ADR-NNN`).

| ADR | Заголовок | Статус | Дата |
|---|---|---|---|
| [ADR-001](./ADR-001-fail-fast-db-password.md) | Fail-fast пароль БД в prod (без дефолта) | Accepted | 2026-06-19 |
| [ADR-002](./ADR-002-rsync-deploy.md) | Rsync-деплой на сервер вместо git-pull | Accepted | 2026-06-19 |
| [ADR-003](./ADR-003-health-gate-before-prune.md) | Health gate перед `docker image prune` | Accepted | 2026-06-19 |

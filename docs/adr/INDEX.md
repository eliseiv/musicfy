# ADR Index — musicfy

Реестр архитектурных решений. Каждый ADR неизменяем после принятия; новое решение заводится
новым ADR со ссылкой на заменяемый (status `Superseded by ADR-NNN`).

| ADR | Заголовок | Статус | Дата |
|---|---|---|---|
| [ADR-001](./ADR-001-fail-fast-db-password.md) | Fail-fast пароль БД в prod (без дефолта) | Accepted | 2026-06-19 |
| [ADR-002](./ADR-002-rsync-deploy.md) | Rsync-деплой на сервер вместо git-pull | Accepted | 2026-06-19 |
| [ADR-003](./ADR-003-health-gate-before-prune.md) | Health gate перед `docker image prune` | Accepted | 2026-06-19 |
| [ADR-004](./ADR-004-ci-gating-before-deploy.md) | CI gating перед деплоем (дополняет ADR-002) | Accepted | 2026-06-19 |
| [ADR-005](./ADR-005-coin-wallet-billing.md) | Единый кошелёк монет вместо мультивалютных кредитов | Accepted | 2026-07-01 |
| [ADR-006](./ADR-006-preset-voices-catalog.md) | Каталог пресет-голосов (AI Voices) + превью профилей | Accepted | 2026-07-06 |
| [ADR-007](./ADR-007-video-three-modes.md) | Видео-генерация на 3 режима (Avatar / Visual Clip / Lyrics Video) | Accepted | 2026-07-06 |
| [ADR-008](./ADR-008-demucs-stems-and-track-metadata.md) | Разбор demucs-стемов (верхнеуровневые ключи) + метаданные трека (промпт + автозаголовок) | Accepted | 2026-07-07 |
| [ADR-009](./ADR-009-cover-cloned-voice-conversion.md) | Cover с клонированным голосом: совместимый провайдер конвертации (chatterbox speech-to-speech) | Accepted | 2026-07-07 |
| [ADR-010](./ADR-010-lyrics-sync-billing.md) | Биллинг синхронной генерации lyrics: атомарный charge + refund (дополняет ADR-005) | Accepted | 2026-07-08 |
| [ADR-011](./ADR-011-user-resource-deletion.md) | Удаление пользовательских ресурсов (voices/tracks/videos): soft-delete | Accepted | 2026-07-08 |
| [ADR-012](./ADR-012-user-resource-rename.md) | Переименование пользовательских ресурсов (tracks/voices/videos) + хранение title видео в meta | Accepted | 2026-07-09 |
| [ADR-013](./ADR-013-storekit-dedup-environment-scoping.md) | Дедуп StoreKit-покупок: environment-scoped ключ + trust anchor вместо глобального bypass (дополняет ADR-005) | Accepted | 2026-07-13 |
| [ADR-014](./ADR-014-storekit-cn-trust-xcode-flag.md) | CN-trust Xcode StoreKit Test сертификатов за флагом для Testing-режима (дополняет ADR-013) | Accepted | 2026-07-13 |

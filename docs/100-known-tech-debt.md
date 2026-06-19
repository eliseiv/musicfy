# Known Tech Debt — musicfy

Реестр осознанно принятого технического долга. Каждый пункт имеет ID `TD-NNN`, на который
ссылаются другие документы и комментарии. Запись остаётся, пока долг не закрыт.

| ID | Тема | Серьёзность | Статус |
|---|---|---|---|
| [TD-001](#td-001) | Нет автоматического отката БД-миграций при rollback | medium | open |
| [TD-002](#td-002) | Async-стадия без media_url помечается succeeded, опираясь на safety-net в _finalize | low | open |
| [TD-003](#td-003) | fal error-конверт (status ERROR) отвергается как невалидный payload вместо маппинга job в failed | low | closed |

---

## TD-001 — Нет автоматического отката БД-миграций при rollback {#td-001}

- **Контекст:** миграции применяются `alembic upgrade head` в `entrypoint.sh` до старта uvicorn.
  Стратегия отката (DEPLOYMENT.md §5) — повторный деплой предыдущего коммита, но down-migration
  при этом не выполняется автоматически.
- **Последствие:** если новый релиз содержал несовместимое со старой версией изменение схемы,
  откат кода без отката схемы может оставить БД в состоянии, неподходящем для предыдущей версии.
  Требуется ручной down-migration.
- **Митигация сейчас:** предпочитать backward-compatible миграции (expand/contract); рискованные
  изменения схемы планировать с явным планом отката.
- **Возможное закрытие:** автоматизировать down-migration в rollback-процедуре либо перейти на
  expand/contract как обязательную политику с проверкой в CI.

---

## TD-002 — Async-стадия без media_url помечается succeeded, опираясь на safety-net в _finalize {#td-002}

- **Контекст:** при completed-событии async-стадии (`music_generation` / `vocal_tts`) **без** media_url
  стадия сейчас помечается `'succeeded'`, хотя медиа фактически нет (`extract_media` не нашёл url в
  распакованном результате fal). Несоответствие выявлено в баге парсинга конверта fal queue
  (см. [ARCHITECTURE.md → Контракт интеграции fal.ai](./ARCHITECTURE.md#контракт-интеграции-falai-форматы-результата)).
- **Последствие:** `_finalize` в `song.py` ловит отсутствие аудио как `PROVIDER_FAILED` (safety-net),
  поэтому задача в итоге корректно падает. Но `job_stage_log` вводит в заблуждение: стадия `succeeded`,
  а песни нет. Это маскирует реальную причину и рискованно для будущих fal-моделей/форматов, не
  покрытых `extract_media`.
- **Митигация сейчас:** safety-net в `_finalize` (`song.py`) гарантирует `failed`-статус задачи при
  отсутствии аудио после пайплайна; единый парсер конверта fal (`parse_fal_webhook_event`) исправлен и
  берёт результат из `payload`.
- **Возможное закрытие:** отдельной итерацией помечать такую стадию `'failed'` + `fail(PROVIDER_FAILED)`
  ближе к источнику события (в обработчике completed-стадии), а не полагаться на safety-net в
  `_finalize`.

---

## TD-003 — fal error-конверт (status ERROR) отвергается как невалидный payload вместо маппинга job в failed {#td-003}

> **Статус: closed.** Контракт обработки error-конверта реализован в `parse_fal_webhook_event`
> (`app/domain/providers/fal/parsing.py`) строго по
> [ARCHITECTURE.md → Контракт интеграции fal.ai](./ARCHITECTURE.md#контракт-интеграции-falai-форматы-результата)
> (пункты 1-6: нормализация error-статусов в `failed`, fallback-цепочка `error_message`, edge
> `payload_error`). Фикс затронул **только** `parsing.py`; webhook-route, success-путь, подпись и
> идемпотентность не менялись. Прошёл backend-review (approve, 0 findings) и qa (33 passed, 0 failed,
> coverage `parsing.py` 90%). Закрытие подтверждено.

- **Контекст (исходная проблема):** реальный fal queue webhook при ошибке генерации приходит в
  конверте со статусом `"ERROR"` (см. docstring `parsing.py`: `status: "OK"|"ERROR"|...`). До фикса
  единый парсер `parse_fal_webhook_event` (`app/domain/providers/fal/parsing.py`) приводил статус к
  lower-case и сверял с whitelist нормализованных статусов плюс отдельным набором success-алиасов
  (`ok`/`success`); `"error"` не входил ни туда, ни туда, поэтому функция бросала
  `WebhookPayloadInvalid(details={"reason": "unknown_status", "status": "error"})`, а не маппила
  job-стадию в `failed`. После фикса статус нормализуется через единый dict `_STATUS_ALIASES`
  (`ok`/`success`→`completed`, `error`/`failed`→`failed`, `canceled`/`cancelled`→`canceled`,
  `in_progress`→`in_progress`); нормализованные статусы из `_WEBHOOK_STATUSES`
  (`{"completed","failed","canceled","in_progress"}`) пропускаются как есть, остальные по-прежнему
  отвергаются как `WebhookPayloadInvalid(reason="unknown_status")`.
- **Последствие:** error-событие fal не транслируется в немедленный `failed`-статус задачи через
  webhook-путь. Webhook отвергается как невалидный payload, и явная причина ошибки от fal (`error`
  верхнего уровня конверта) не используется для маппинга.
- **Митигация сейчас:** поллер `FalPoller` (`POLL_ENABLED=true`) в итоге подбирает финальный статус
  очереди fal либо отрабатывает по таймауту, после чего пайплайн доходит до safety-net в `_finalize`
  (`app/domain/services/pipelines/song.py`, строки ~215-220: `no audio after pipeline` →
  `_mark_failed(PROVIDER_FAILED)`). Поэтому задача в итоге корректно падает — но позже и без явной
  причины из error-конверта.
- **Серьёзность — low:** функциональной потери нет (poller-fallback гарантирует терминальный `failed`),
  ухудшается лишь скорость и информативность диагностики ошибки.
- **Закрыто:** реализовано в `parse_fal_webhook_event` (`parsing.py`). `"error"`/`"failed"` (и
  прочие алиасы fal) сводятся через `_STATUS_ALIASES` к нормализованному `failed`. Для `failed`
  `media_url`/`duration_seconds`/`stems = None`, а `error_message` формируется fallback-цепочкой
  `_resolve_error_message` (`error`/`error_message`/`payload_error` → если пусто и `payload` —
  dict, то `payload.detail` либо сам `payload` через `_compact_json`; итог усекается до
  `_ERROR_MESSAGE_MAX_LEN = 500`; иначе `None`). Edge `OK`/`success` (нормализован в `completed`) +
  пустой `payload` + `payload_error` принудительно переводится в `failed`. Полный контракт — в
  [ARCHITECTURE.md → Контракт интеграции fal.ai](./ARCHITECTURE.md#контракт-интеграции-falai-форматы-результата).
  Подтверждено backend-review (approve, 0 findings) и qa (33 passed, coverage `parsing.py` 90%;
  тесты `tests/test_fal_webhook_error_envelope.py`, `tests/test_fal_webhook_parse.py`).

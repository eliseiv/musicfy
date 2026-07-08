# Known Tech Debt — musicfy

Реестр осознанно принятого технического долга. Каждый пункт имеет ID `TD-NNN`, на который
ссылаются другие документы и комментарии. Запись остаётся, пока долг не закрыт.

| ID | Тема | Серьёзность | Статус |
|---|---|---|---|
| [TD-001](#td-001) | Нет автоматического отката БД-миграций при rollback | medium | open |
| [TD-002](#td-002) | Async-стадия без media_url помечается succeeded, опираясь на safety-net в _finalize | low | open |
| [TD-003](#td-003) | fal error-конверт (status ERROR) отвергается как невалидный payload вместо маппинга job в failed | low | closed |
| [TD-004](#td-004) | Lyrics Video V1 — равномерная синхронизация строк вместо форс-алайнмента | medium | open |
| [TD-005](#td-005) | Нет background job runner для тяжёлых синхронных задач вне request-пути (статический фон lyrics_video отложен) | medium | open |
| [TD-006](#td-006) | Превью-сэмплы пресет-голосов не сгенерированы/не забэкфилены — `preset_voices.preview_url` / `sample_duration_seconds` = NULL; ▶️ на вкладке AI Voices нефункционально | low | closed |
| [TD-007](#td-007) | Инструментал cover — простой ffmpeg-микс не-вокальных стемов; при отсутствии ffmpeg/стемов деградирует до чистого конвертированного вокала | low | open |
| [TD-008](#td-008) | minimax/voice-clone вестигиален для cover: его custom_voice_id не используется, но «готовность» клона всё ещё гейтится успехом minimax-вызова | low | open |
| [TD-009](#td-009) | Дедуп самого драфта lyrics по Idempotency-Key не реализован — при ретрае с тем же ключом списания нет, но fal вызовется снова и создастся новый драфт; без ключа сетевой ретрай POST даёт двойное списание + двойную генерацию | low | open |

---

## TD-009 — Нет дедупа драфта lyrics по Idempotency-Key {#td-009}

- **Контекст:** биллинг синхронного `POST /v1/lyrics`
  ([ADR-010](./adr/ADR-010-lyrics-sync-billing.md)) делает списание (`charge`) идемпотентным
  по `Idempotency-Key` заголовку: повторный запрос с тем же ключом **не списывает** повторно
  (дедуп `credit_ledger`). Но **сам драфт** не дедуплицируется — маппинг `op_id → draft_id`
  не хранится. Поэтому ретрай с тем же ключом всё равно вызовет fal и создаст новый
  `LyricsDraft`. Без `Idempotency-Key` каждый POST независим: сетевой ретрай = двойное
  списание + двойная генерация.
- **Последствие:** при сетевом ретрае клиент может получить два драфта; при отсутствии
  `Idempotency-Key` — ещё и двойное списание монет. Для творческого эндпоинта, где каждый
  вызов даёт другой текст, повторная генерация по «двойному нажатию» — ожидаемое поведение,
  но по сетевому ретраю одного логического запроса — нежелательное.
- **Серьёзность — low:** операция дешёвая (цена lyrics в монетах), списание уже
  идемпотентно при переданном `Idempotency-Key`; функциональной потери нет.
- **Митигация сейчас:** клиент шлёт `Idempotency-Key` на ретраях — двойного **списания**
  не будет (ADR-010 §3). Двойной драфт при этом возможен, но безвреден.
- **Возможное закрытие:** хранить маппинг `Idempotency-Key → draft_id` (напр. столбец/таблица)
  и возвращать существующий `LyricsDraft` на ретрай с тем же ключом (полный end-to-end дедуп,
  симметрично `JobsRepository.get_by_idempotency_key` в `create_job`).

## TD-008 — minimax voice-clone вестигиален для cover, но гейтит готовность клона {#td-008}

- **Контекст:** после [ADR-009](./adr/ADR-009-cover-cloned-voice-conversion.md) cover для собственного
  клона конвертирует через `fal-ai/chatterbox/speech-to-speech`, используя **образец голоса**
  (`VoiceProfile.sample_asset_url`) как аудио-референс. `VoiceProfile.provider_voice_id` (minimax
  `custom_voice_id`, `FAL_VOICE_CLONE_MODEL = fal-ai/minimax/voice-clone`) для cover **больше не
  используется**. При этом `voice_clone.py` по-прежнему вызывает minimax и при его сбое помечает
  профиль `failed`.
- **Последствие:** «готовность» клона гейтится успехом minimax-вызова, который для cover не нужен.
  Сбой/недоступность minimax блокирует клон, который по факту работал бы в cover через chatterbox
  (нужен лишь валидный consent + образец). Плюс лишний fal-cost на каждый клон.
- **Серьёзность — low:** основной путь (minimax доступен) даёт `ready`-профиль, cover работает;
  minimax `custom_voice_id` может пригодиться будущей фиче «TTS в вашем голосе». Деградация — только
  при недоступности minimax.
- **Митигация сейчас:** осознанно оставлено вне scope фикса ADR-009 (минимизация изменений). cover
  для существующих и новых `ready`-клонов работает через сохранённый образец.
- **Возможное закрытие:** отвязать «готовность» клона от minimax-вызова — гейтить только на
  `consent + sample_asset_url`; minimax-clone сделать best-effort (не валит профиль) либо удалить,
  если фича «TTS в вашем голосе» не планируется.

---

## TD-007 — Инструментал cover: простой микс стемов, деградация без ffmpeg {#td-007}

- **Контекст:** demucs отдаёт `drums/bass/other/guitar/piano` (без `accompaniment`), поэтому инструментал
  для cover собирается ffmpeg-миксом всех не-вокальных стемов
  ([ADR-008](./adr/ADR-008-demucs-stems-and-track-metadata.md) §A2). Микс — прямой `amix` без ремастеринга,
  баланса громкостей по партиям и мастеринга.
- **Последствие:** качество инструментала «MVP», не студийное (эквалайзинг/громкости партий не выравниваются).
  При отсутствии ffmpeg в PATH **или** отказе сепарации (нет не-вокальных стемов) cover деградирует до
  **чистого конвертированного вокала** без музыкальной подложки (микс поверх исходного трека запрещён —
  двойной вокал).
- **Серьёзность — low:** основной путь (реальный demucs + ffmpeg) даёт полноценный инструментал; функциональной
  потери нет, страдает лишь качество/наличие подложки в деградированных ветках.
- **Митигация сейчас:** прод-окружение с ffmpeg в PATH → основной путь; фикс A1 гарантирует наличие стемов на
  реальном demucs, поэтому деградация — только для отказа сепарации.
- **Возможное закрытие:** пер-партийный баланс громкостей + нормализация/мастеринг инструментала; опционально —
  провайдерский stems-to-instrumental эндпоинт вместо локального `amix`.

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

---

## TD-004 — Lyrics Video V1: равномерная синхронизация строк вместо форс-алайнмента {#td-004}

- **Контекст:** режим Lyrics Video (см. [ADR-007](./adr/ADR-007-video-three-modes.md), стадия
  `lyrics_render` в `app/domain/services/pipelines/video.py` / `video_mux.py`) в V1 распределяет
  строки лирики по длительности трека **равномерно** (`probe_duration_seconds` ÷ число строк),
  генерируя `.ass`/`.srt` и бёрня их поверх фона через ffmpeg. Реального выравнивания текста по
  вокалу (force alignment) нет. **Исполнение теперь async** (MAJOR-6, ADR-007 §3a): стадия
  `lyrics_render` + мукс выполняются в `advance()` (фон webhook/поллера) поверх сгенерированного
  fal t2v-фона, а **не** синхронно в `start()`/request-пути. Равномерность таймингов — по-прежнему
  V1-ограничение (этот TD).
- **Последствие:** тайминги строк смещаются относительно фактического пения — тем сильнее, чем
  неравномернее вокал (проигрыши, растянутые/быстрые куски). Приемлемо для V1 lyrics-клипа, но
  не «karaoke-точно».
- **Митигация сейчас:** V1 сознательно равномерная; лирика берётся из
  `Job.input_payload['_lyrics']` (song-пайплайн пишет `_lyrics` в `input_payload` задачи-песни,
  `song.py:37,58`; в `Track.meta` его **нет** — там только `{"runtime": ...}`, `song.py:243`).
  При `trackId` резолв идёт через `Track.job_id` → `Job` (обратный доступ track→job — задача
  backend, см. [ADR-007](./adr/ADR-007-video-three-modes.md) §3/MAJOR-2); без трека — явное поле
  `lyrics` запроса. Качество зависит от наличия текста.
- **Возможное закрытие (V2):** отдельная стадия `align_lyrics` (`JobStage`, зарезервирован в
  enum) с форс-алайнментом (whisperx / aeneas / STT) — генерирует точные тайминги строк/слов до
  `lyrics_render`. Требует тяжёлой зависимости и доп. стоимости — отложено осознанно.

---

## TD-005 — Нет background job runner для тяжёлых синхронных задач вне request-пути {#td-005}

- **Контекст:** `pipeline.start()` вызывается **инлайн** в `create_job` внутри HTTP-хендлера
  `POST /v1/videos` (`generation_service.py:192` → `runner.py:53`). Единственный механизм
  асинхронной фоновой обработки — стадии, продвигаемые `advance()` через webhook/`FalPoller` по
  завершении **fal**-задачи. Собственного background job runner (worker + очередь) под тяжёлые
  **не-fal** задачи (ffmpeg-рендер/мукс/upload на всю длину трека) в системе нет.
- **Последствие:** режим, который хотел бы делать тяжёлую работу **без** fal-стадии (например,
  Lyrics Video со статическим/градиентным фоном, ADR-007), не может — синхронное исполнение в
  `start()` заблокировало бы request-путь (таймаут Traefik/uvicorn) и оставило бы job `running`
  без `provider_request_id`, который `recover_orphan_jobs` пометит `failed` при рестарте
  (`recovery.py:16`). Поэтому статический фон lyrics_video **отложен**, а V1 всегда использует
  генеративный fal t2v-фон (async, симметрично visual_clip) — доп. стоимость и время генерации.
- **Митигация сейчас:** все тяжёлые video-стадии привязаны к fal-submit → `advance()` (инвариант
  ADR-007 §3a); `start()` во всех режимах быстрый (только fal-submit).
- **Возможное закрытие:** ввести background job runner (напр. отдельный worker + очередь / таблица
  задач с поллингом), чтобы `start()` мог поставить тяжёлую не-fal задачу в фон и сразу вернуть
  `202`. Тогда статический/градиентный фон lyrics_video (без затрат на fal) станет возможен.

---

## TD-006 — Превью-сэмплы пресет-голосов не сгенерированы/не забэкфилены {#td-006}

> **Статус: closed.** Превью-сэмплы для всех 8 пресет-голосов сгенерированы реальным fal
> voice-changer (эталонный вокал-клип → `fal-ai/elevenlabs/voice-changer` по каждому
> `provider_voice`, queue submit → poll result) и забэкфилены миграцией
> `0014_seed_preset_voice_previews` (`down_revision="0013_video_stages"`, голова цепочки теперь
> `0014`). `UPDATE preset_voices SET preview_url / sample_duration_seconds` для 8 ключей
> (aria / max / luna / kai / nova / leo / sage / rex); `downgrade` обнуляет обратно в NULL.
> Все 8 URL проверены (HTTP 200, `content-type audio/mpeg`, ~5с), round-trip alembic up/down
> пройден. Контракт превью — [ADR-006](./adr/ADR-006-preset-voices-catalog.md) §4/§6. Закрытие
> подтверждено.

- **Контекст (исходная проблема):** каталог пресет-голосов (AI Voices) заведён миграцией `0012_preset_voices`
  (8 голосов: Aria / Max / Luna / Kai / Nova / Leo / Sage / Rex), где `preview_url` и
  `sample_duration_seconds` сознательно засеяны `NULL` (см.
  [ADR-006](./adr/ADR-006-preset-voices-catalog.md) §4). По ADR-006 §4/§6 превью заполняются
  **отдельной оффлайн-бэкфилл-миграцией** (`UPDATE preset_voices SET preview_url /
  sample_duration_seconds ...`). Эта миграция **не создана**: изначально в ADR за ней был
  зарезервирован номер `0013`, но слот занят Feature B (`0013_video_stages`, ADR-007). Голова
  цепочки миграций сейчас — `0013_video_stages`; свободный слот под бэкфилл — следующий
  (напр. `0014_seed_preset_voice_previews`).
- **Последствие:** все 8 пресет-голосов уходят в прод с `preview_url = NULL` → кнопка ▶️
  превью на вкладке AI Voices (экран Create Cover) **нефункциональна** до бэкфилла.
- **Серьёзность — low/medium:** функциональной потери в основном флоу нет. Каталог, имена,
  метаданные (`gender` / `style` / `subtitle`), выбор и резолв голоса (`cover.targetVoice` →
  `provider_voice`) работают; эндпоинт `GET /v1/presets/voices` и iOS-клиент терпят `null`
  `previewUrl` (▶️ просто неактивна). Превью на вкладке My Clones работает независимо — оно
  берётся из `voice_profiles.sample_asset_url`, а не из пресет-ветки. Пробел ограничен
  превью-сэмплами пресетов.
- **Митигация сейчас:** сид каталога (`0012`) и генерация превью разнесены и не блокируют друг
  друга (ADR-006 §4); рантайм production-ready — все NULL-пути обрабатываются gracefully на
  бэкенде и клиенте.
- **Закрыто:** одноразовая оффлайн-генерация превью выполнена — эталонный вокал-клип (сгенерирован
  через `FAL_SPEECH_MODEL`) прогнан через fal voice-changer (`FAL_VOICE_CHANGER_MODEL =
  fal-ai/elevenlabs/voice-changer`) по каждому `provider_voice` реальным queue-вызовом
  (submit → poll result по `request_id`); durable-URL результата (`v3b.fal.media`) используются
  напрямую. Пары `(key → preview_url, sample_duration_seconds)` забэкфилены миграцией
  `0014_seed_preset_voice_previews` (`down_revision="0013_video_stages"`; `upgrade` —
  `UPDATE preset_voices SET preview_url = :url, sample_duration_seconds = :dur WHERE key = :key`
  для 8 ключей; `downgrade` — обнуление в NULL). Голова цепочки миграций теперь `0014`. Все 8 URL
  проверены (HTTP 200, `audio/mpeg`, ~5с), alembic round-trip up/down пройден. Сид каталога
  (`0012`, NULL) и бэкфилл (`0014`, реальные URL) разнесены и не блокируют друг друга. Контракт —
  [ADR-006](./adr/ADR-006-preset-voices-catalog.md) §4/§6.

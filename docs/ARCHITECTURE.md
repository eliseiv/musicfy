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
  отдельно извлекаются `stems` (см. ниже — `extract_stems`).

**Разбор стемов — `extract_stems` (demucs, [ADR-008](./adr/ADR-008-demucs-stems-and-track-metadata.md) §A).**
Модель `fal-ai/demucs` отдаёт стемы **верхнеуровневыми ключами** результата, без обёртки `"stems"`:
`{"vocals":{"url":..},"drums":{"url":..},"bass":{"url":..},"other":{"url":..},"guitar":{"url":..},"piano":{"url":..}}`.
Поэтому `stems` извлекаются по правилам (`extract_stems`, `domain/providers/fal/parsing.py`):
1. **Приоритет — явная обёртка `result["stems"]`** (dict) — обратная совместимость с моделями/фикстурами, кладущими стемы под `"stems"`.
2. Иначе — **верхнеуровневый demucs-путь:** собрать ключи из известного словаря
   `STEM_NAMES = {vocals, vocal, drums, bass, other, guitar, piano, accompaniment, instrumental, backing}`,
   у которых значение даёт url (`{url:..}` или строка). Порог **`>=2`** ключей (demucs всегда отдаёт 4-6) —
   защита от коллизии одиночного stem-именованного ключа у не-сепараторных моделей.
3. Иначе → `None`.

> `extract_media` при этом **не** затрагивается: demucs-payload не содержит медиа-ключей (`audio`/`video`/`output`/`result`/`audio_url`/`video_url`) → `media_url=None` (корректно, cover не использует media_url со стадии `stem_separation`). song-стемы не ломаются: payload песни — `{"audio":{"url":..}}`, ключ `audio` не входит в `STEM_NAMES`. **Инвариант:** стаб и тест-фикстуры fal обязаны эмитить те же формы payload, что реальный fal (demucs — верхнеуровневые стемы); расхождение формы уже дважды давало «зелёные тесты / сломанный прод» ([TD-002](./100-known-tech-debt.md#td-002), [TD-003](./100-known-tech-debt.md#td-003)).

**Инструментал cover из demucs ([ADR-008](./adr/ADR-008-demucs-stems-and-track-metadata.md) §A2).**
demucs не отдаёт `accompaniment`/`instrumental` — только `drums/bass/other/guitar/piano`. Инструментал = ffmpeg-микс
**всех не-вокальных стемов** (не одиночный `other`). Деградация: нет не-вокальных стемов или ffmpeg недоступен →
отдаётся **чистый конвертированный вокал** (микс поверх исходного трека запрещён — двойной вокал). См.
[TD-007](./100-known-tech-debt.md#td-007).

- **Прямой result-эндпоинт (poll-путь `FalPoller` / `fetch_status`, fallback).** Отдаёт
  **распакованный** результат без конверта (например `{"audio": {...}}`). На случай конвертного
  ответа есть защитная распаковка `payload`-dict.

> Эта разница форматов — контракт интеграции, обязательный к соблюдению при добавлении новых fal-моделей:
> отсутствие распаковки `payload` приводило к `media_url=None` и ложному `succeeded` стадии (см.
> [TD-002](./100-known-tech-debt.md#td-002)).

**Обработка error-конверта fal queue (контракт реализован).** fal queue webhook доставляет ошибку в
конверте с верхнеуровневыми полями `request_id`, `gateway_request_id`, `status`, `payload`, `error`
(при ошибке генерации) и `payload_error` (при ошибке сериализации результата). Парсер
`parse_fal_webhook_event` (`domain/providers/fal/parsing.py`) обрабатывает этот конверт по
следующим правилам (реализовано, см. [TD-003 — closed](./100-known-tech-debt.md#td-003)):

1. **Нормализация статуса.** Сырой `status` приводится к lower-case, затем к нормализованному
   множеству `{completed, failed, canceled, in_progress}` (новых статусов не вводится) по алиасам:
   - `"ok"` / `"success"` → `completed`;
   - `"error"` / `"failed"` → `failed`;
   - `"canceled"` / `"cancelled"` → `canceled` (оба написания валидны);
   - `"in_progress"` → `in_progress`.

   Error-статус (`"ERROR"` / `"error"`) **больше не** бросает `WebhookPayloadInvalid(reason=unknown_status)` —
   он маппится в нормализованный `failed`. Статусы вне множества алиасов по-прежнему отвергаются как
   `WebhookPayloadInvalid`.

2. **Источник `error_message` для `failed` (fallback-цепочка по приоритету):**
   - a) первое непустое из верхнеуровневых `error` / `error_message` / `payload_error` (все три
     рассматриваются на одном шаге, в этом порядке приоритета — совпадает с
     `_resolve_error_message`, `domain/providers/fal/parsing.py`);
   - b) если пусто → компактная сериализация `payload.detail` (если присутствует) или `payload`.

   Результат компактный, усекается до **500 символов**; чувствительные данные в сообщение не
   попадают. Если итог пустой → `error_message` остаётся пустым, а webhook-route делает fallback на
   сам нормализованный статус (`event.error_message or event.status`, `api/v1/webhooks.py`).

3. **Edge — ошибка сериализации (`OK` + пустой `payload` + `payload_error`).** Конверт вида
   `{"status":"OK","payload":null,"payload_error":"..."}` трактуется как `failed` (исключение из
   success-пути), `error_message` берётся из `payload_error`.

4. **Success-путь без изменений.** `status:"OK"` с непустым dict `payload` → `completed`, распаковка
   `payload`, `extract_media` (см. выше). Этот путь не затрагивается.

5. **Pipeline-контракт не меняется.** Нормализованный `failed` через webhook-route
   (`api/v1/webhooks.py`) маппится в `runner.fail(error_code="PROVIDER_FAILED", error_message=...)` →
   `_mark_failed` / refund. Маршрут уже корректен — изменение касается только нормализации в парсере.

6. **Подпись и идемпотентность не меняются.** `verify_webhook` и дедупликация по
   `event_id` / `payload_digest` остаются как есть.

С реализацией этого контракта поллер-fallback (`FalPoller`) перестал быть единственным путём
терминализации error-задачи: webhook сразу падает с явной причиной из конверта fal.

## Голоса: пресет-каталог (AI Voices) + резолв targetVoice

Решение — [ADR-006](./adr/ADR-006-preset-voices-catalog.md).

Экран Create Cover предлагает голос для кавера из двух источников: **каталог пресет-голосов**
(вкладка «AI Voices») и **собственные клоны** пользователя (вкладка «My Clones», `VoiceProfile`).

- **Справочник `preset_voices`** (образец `prompt_presets`): `key` (публичный, unique), `title`,
  `subtitle`, `provider_voice` (**внутренний** id голоса fal voice-changer — наружу не отдаётся),
  `preview_url` / `sample_duration_seconds` (▶️), `gender` / `style` / `language` (строки, без
  enum), `sort_order`, `active`, `meta`. Эндпоинт `GET /v1/presets/voices` → `list[PresetVoiceView]`
  (только активные, сортировка `sort_order, title`); схема **без** `provider_voice`.
- **Резолв `cover.targetVoice`** (в `generation_service.create_job`, `JobType.cover`): значение
  валидно если пустое **или** UUID собственного `ready`-клона **или** активный `key` пресета;
  иначе `ValidationFailed(reason="unknown_voice")` (422). Резолв кладёт во внутренний
  `job.input_payload` (`_`-префиксные ключи) **дискриминатор ветки конвертации** `_voice_kind`
  (`preset` | `clone`), см. ниже. Наружу клиент оперирует только `key`/UUID.
- **Ветвление cover-конвертации по источнику голоса ([ADR-009](./adr/ADR-009-cover-cloned-voice-conversion.md)).**
  cover — это **audio-to-audio voice conversion** вокального стема. Провайдер конвертации выбирается
  по источнику голоса, потому что клон (minimax) и ElevenLabs voice-changer **несовместимы**
  (minimax `custom_voice_id` пригоден только для minimax **TTS**, не для конверсии вокала; ElevenLabs
  voice-changer принимает только имена голосов своего аккаунта и не имеет на fal endpoint добавления
  кастомного голоса):

  | `_voice_kind` | Источник | Модель | Целевой голос в fal |
  |---|---|---|---|
  | `preset` (или пусто) | активный `preset_voices.key` | `FAL_VOICE_CHANGER_MODEL` (ElevenLabs voice-changer) | `voice` = `preset.provider_voice`; пусто → дефолт |
  | `clone` | UUID собственного `ready`-профиля | `FAL_VOICE_CONVERSION_MODEL` = `fal-ai/chatterbox/speech-to-speech` | `target_voice_audio_url` = `VoiceProfile.sample_asset_url` (zero-shot аудио-референс) |

  **Инвариант резолва (пресет):** `payload["target_voice"]` переписывается на `preset.provider_voice`
  **до** сохранения job (в fal уходит провайдерское имя ElevenLabs). **Инвариант резолва (клон):**
  вместо перезаписи `target_voice` на minimax-id (это давало fal 422) выставляются
  `_voice_kind="clone"` и `_target_voice_sample_url = profile.sample_asset_url`; при пустом
  `sample_asset_url` у `ready`-клона → `422 unknown_voice`. `provider_voice_id` (minimax) для cover
  **не используется** — cover опирается на образец голоса. **Миграция клонов не нужна:**
  `sample_asset_url` сохраняется у каждого профиля при `create_profile` (request-time). Poller/webhook
  для cover-конвертации не зависят от `job.provider_model` (опрашиваются сохранённые
  `_fal_status_url`/`_fal_response_url`), поэтому смена модели на chatterbox polling не ломает. Схема
  `VoiceProfile` не меняется, колонка `provider` не вводится (дискриминатор — источник голоса).
  Вестигиальность minimax-clone — [TD-008](./100-known-tech-debt.md#td-008).
- **Превью** заполнены оффлайн (бэкфилл-миграция `0014_seed_preset_voice_previews`,
  `down_revision="0013_video_stages"` — реальные fal voice-changer URL для 8 пресетов,
  выполнена → [TD-006 (closed)](./100-known-tech-debt.md#td-006)), не в request-флоу: схема терпит
  `NULL` в `preview_url` (для новых пресетов до бэкфилла ▶️ неактивна). Профиль
  клона (`VoiceProfileResponse`) отдаёт `previewUrl`
  (из `voice_profiles.sample_asset_url`) и `sampleDurationSeconds` (новая колонка, best-effort
  `probe_duration_seconds`).

## Видео: режимы генерации (Avatar / Visual Clip / Lyrics Video)

Решение — [ADR-007](./adr/ADR-007-video-three-modes.md).

`CreateVideoRequest.mode` (`VideoMode` enum) ветвит **выбор fal-модели** и **набор стадий**
пайплайна; общий финал (`upload_cdn` → capture → `Asset` → `mark_succeeded` → push)
переиспользуется. Цена единая `video=30` (ADR-005), инварианты монет reserve→capture→release
не меняются.

**Маппинг режима → fal-модель** (env `FAL_VIDEO_*` в `config.py`, `_provider_model` для
`JobType.video` вычисляет модель по `mode` + наличию reference/source; `job.provider_model` обязан
совпадать с реально вызванной моделью — его опрашивает `FalPoller`):

| Режим | Условие | Модель (env) |
|---|---|---|
| avatar_performance | `sourceVideoUrl` | `FAL_VIDEO_AVATAR_MODEL` (текущий kling lipsync) |
| avatar_performance | только `referenceImageUrl` | `FAL_VIDEO_AVATAR_IMAGE_MODEL` |
| visual_clip | без референса | `FAL_VIDEO_VISUAL_MODEL` (t2v) |
| visual_clip | с `referenceImageUrl` | `FAL_VIDEO_VISUAL_IMAGE_MODEL` (i2v) |
| lyrics_video | всегда (async t2v-фон под бёрн-ин лирики) | `FAL_VIDEO_LYRICS_BG_MODEL` (t2v, дефолт задан) |

**Инвариант async (все режимы, ADR-007 §3a):** `pipeline.start()` вызывается **инлайн** в
`create_job` внутри HTTP-хендлера `POST /v1/videos` (`generation_service.py:192` → `runner.py:53`).
Поэтому `start()` во всех режимах — только дешёвые операции + **fal-submit** (возвращает
`request_id`, ставит `current_stage`/`provider_request_id`), POST отдаёт `202` мгновенно. Тяжёлый
ffmpeg-рендер / мукс / бёрн-ин лирики / upload — **запрещены на request-пути** и выполняются
**только в `advance()`** (фон webhook/поллера, образец `cover._mix`). Синхронный ffmpeg в `start()`
заблокировал бы POST (таймаут Traefik/uvicorn) и породил бы ложный orphan-recovery.

**Стадии пайплайна** (`pipelines/video.py`, `ASYNC_STAGES`; ffmpeg-вызов синхронен внутри
`advance()`, образец `cover._mix` / `audio_mixer.py`, новый `video_mux.py`):

- **Avatar Performance:** `submit_lipsync_video` (video) / `submit_avatar_image_video` (image) →
  финал. Аудио вшито моделью, мукс не нужен.
- **Avatar Performance:** image-ветка (`submit_avatar_image_video`, только `referenceImageUrl`)
  **переиспользует `JobStage.lipsync`** — отдельной стадии нет; в `advance()` `completed_stage =
  lipsync` (совпадает с `current_stage`-guard), `idempotency_key = {job.id}:avatar`.
- **Visual Clip:** `prepare_prompt` → `visual_gen` (async fal) → **`mux_audio`** (ffmpeg) →
  `upload_cdn` → `finalize`. fal t2v/i2v выдаёт клип на **секунды**, трек — **минуты**; наивный
  `-shortest` обрезал бы видео до длины короткого клипа, поэтому границей ставится **длина
  аудио-трека** (`probe_duration_seconds`), а видео **зацикливается/растягивается** под неё
  (`-stream_loop`/concat) — рекомендуемая стратегия. Сбой ffmpeg → немое видео + `quality_flag`.
- **Lyrics Video (async, симметричен visual_clip):** `prepare_prompt` → `source_prep` (лирика +
  длительность, дёшево, без ffmpeg) → **`visual_gen`** (async fal **t2v-фон**,
  `FAL_VIDEO_LYRICS_BG_MODEL`, submit в `start()`, `idempotency_key={job.id}:lyrics_bg`) →
  [webhook/poller → `advance()`] → **`lyrics_render`** (ffmpeg subtitles/drawtext поверх
  сгенерированного фона) → мукс (длина = длина трека) → `upload_cdn` → `finalize`. **Вся
  ffmpeg-работа и upload — в `advance()`** (не в `start()`); `advance()` при `visual_gen` ветвит по
  `mode`: visual_clip → `mux_audio`, lyrics_video → `render_lyrics_video`(фон=`media_url` от fal) +
  мукс. `job.provider_model = FAL_VIDEO_LYRICS_BG_MODEL` (**не** `None`), `provider_request_id`
  выставлен в `start()` → poller/webhook ведут job как у visual_clip; инварианты монет
  reserve→capture→release сохранены. V1-синхронизация лирики — равномерное распределение строк (см.
  [TD-004](./100-known-tech-debt.md#td-004)), но исполняется async. **Лирика — из
  `Job.input_payload['_lyrics']`**, а **не** из `Track.meta`: song-пайплайн пишет `_lyrics` в
  `input_payload` задачи-песни (`song.py:37,58`), `Track.meta` содержит лишь `{"runtime": ...}`
  (`song.py:243`). При `trackId` резолв: `Track.job_id` (колонка есть, `track.py:46`) →
  `JobsRepository.get_by_id` → `input_payload['_lyrics']` (обратный доступ track→job — задача
  backend; альтернатива — писать `_lyrics` в `Track.meta` в `song.py`); без трека — явное поле
  `lyrics` запроса. **Статический фон без fal** (полностью синхронный пайплайн, `provider_model=None`)
  — **отложен** (требует background job runner, которого нет; синхронный рендер в request-пути
  запрещён, ADR-007 §3a).

Новые `JobStage`: `visual_gen`, `mux_audio`, `lyrics_render` (+резерв `align_lyrics` под V2) —
`job_stage` **нативный PG enum**, требует миграции `ALTER TYPE ... ADD VALUE`. `VideoStyle` /
`VideoAspect` / `VideoMode` живут строками в `Asset.meta` (без PG-типа). Референс-картинка —
`POST /v1/uploads/image` (`AssetKind.image`); `trackId` резолвится в `audio_url` через
`TracksRepository` с проверкой владельца (для lyrics_video лирика — дополнительно из `Job` по
`track.job_id`, см. выше); «Surprise me» — случайный шаблон из `prompt_presets`,
проходит модерацию.

`Asset.meta` для видео-результата: `mode`, `style`, `aspect_ratio`, `quality_flag` (флаг
деградации ffmpeg), `title` (отображаемое имя, [ADR-012](./adr/ADR-012-user-resource-rename.md)).
`VideoResultResponse` отдаёт `mode` / `aspectRatio` / `style` / `title` из `Asset.meta`.

## Контракты API — новые/изменённые (ADR-006 / ADR-007)

> `docs/openapi.json` перегенерируется backend после реализации. Ниже — целевой контракт.

**Голоса (ADR-006):**
- `GET /v1/presets/voices` (**новый**) → `200 list[PresetVoiceView]`.
  `PresetVoiceView` = `{ key, title, subtitle?, previewUrl?, sampleDurationSeconds?, gender?,
  style?, language? }`. Только активные; **без** `provider_voice`.
- `VoiceProfileResponse` (**изменён, аддитивно**) — добавлены `previewUrl?`, `sampleDurationSeconds?`.
- `cover.targetVoice` (**ЛОМАЮЩЕЕ**): было freeform-строкой, стало — пусто **или** UUID своего
  `ready`-клона **или** активный `key` пресета. Неизвестное значение → `422 { reason:
  "unknown_voice" }`.

**Видео (ADR-007):**
- `POST /v1/uploads/image` (**новый**) → `AssetResponse` (образец `/uploads/source-video`,
  `AssetKind.image`, `UPLOAD_IMAGE_CONTENT_TYPES`).
- `CreateVideoRequest` (**ЛОМАЮЩЕЕ**): `mode` из freeform-строки-с-default стал обязательным
  `VideoMode`; добавлены `trackId?`, `variantId?`, `referenceImageUrl?`, `style?`, `aspectRatio?`
  (default `9:16`), `prompt?`, `surpriseMe` (default false); `audioUrl` / `sourceVideoUrl` больше
  не всегда обязательны — условны по режиму (`model_validator._validate_by_mode`). Источник аудио —
  `audioUrl` XOR `trackId`.
- `VideoResultResponse` (**изменён, аддитивно**) — добавлены `mode`, `aspectRatio`, `style`;
  `title?` (из `Asset.meta["title"]`, [ADR-012](./adr/ADR-012-user-resource-rename.md)).
- `LibraryItem.id` для видео (**ЛОМАЮЩЕЕ**, [ADR-012 §7](./adr/ADR-012-user-resource-rename.md)) — было
  `Asset.id` (не принимался ни одним эндпоинтом), стало `Asset.meta["job_id"]`. Инвариант контракта library:
  **`id` элемента = идентификатор, принимаемый эндпоинтами ресурса** — треки `Track.id` (`/v1/tracks/{id}`),
  голоса `VoiceProfile.id` (`/v1/voices/{id}`), видео `job_id` (`GET/PATCH/DELETE /v1/videos/{job_id}`).
  Форма поля (`id: str`) не меняется; ветки tracks/voices не затронуты.

**Трек: промпт и автозаголовок ([ADR-008](./adr/ADR-008-demucs-stems-and-track-metadata.md) §B):**
- `TrackResponse` (**изменён, аддитивно**) — добавлено `prompt?: string` (из `Track.meta["prompt"]`).
  `title` уже присутствовал и теперь **всегда непустой** (автозаголовок на этапе создания трека).
- `LibraryItem` (**изменён, аддитивно**) — добавлено `prompt?: string` (для треков — `Track.meta["prompt"]`;
  для video/voice — `null`). `title` теперь непустой для треков.
- `GET /v1/tracks/{id}` / `GET /v1/library` — отдают `prompt` и непустой `title`.

**Статусы валидации (единый контракт всех эндпоинтов):**
- **Схемная валидация тела** (Pydantic: отсутствие обязательного поля, неверный enum, провал
  `model_validator`) → `RequestValidationError` → `validation_handler` → **`400 INVALID_INPUT`**
  (фикс `validation_handler`: сериализация деталей не может уронить хендлер в 500, статус всегда 400).
- **Endpoint-level `ValidationFailed`** (бизнес-проверка уже после схемы: `unknown_variant`,
  `track_has_no_audio`, `unknown_voice`) → **`422`** (класс `ValidationFailed` с явным
  `http_status=422`, код тела остаётся `INVALID_INPUT`).

## Метаданные трека: промпт и автозаголовок

Решение — [ADR-008](./adr/ADR-008-demucs-stems-and-track-metadata.md) §B. `Track` уже имеет `title`
(String255, nullable) и `meta` (JSONB) — **миграция не нужна**.

- **Промпт генерации** сохраняется в `Track.meta["prompt"]` при создании трека в `_finalize`:
  - **song:** сырой пользовательский `input_payload.get("prompt")` (не композитная строка
    `_compose_song_prompt` — та внутренняя, с genre/mood-хвостами для fal); при пустом — `null`.
  - **cover:** `null` (текстового промпта нет).
- **Автозаголовок** — детерминированный helper `derive_track_title(kind, input_payload)`
  (`app/domain/services/track_title.py`), **без** доп. генерации/сетевых вызовов. Заменяет
  `title=(input_payload).get("title")` в обоих `_finalize` (`song.py`, `cover.py`):
  - **song:** `title` (если задан) → усечение `prompt` до 40 симв. по границе слова + `…` →
    `custom_lyrics` → `lyrics_prompt` → `"New Song"`.
  - **cover:** `title` (если задан) → `Cover • <basename(source_audio_url) без расширения, ≤40>` →
    `"Cover"`. Имя пресет-голоса не резолвится (избегаем доп. запроса; детерминизм важнее).
- Обратная совместимость: старые треки без `meta["prompt"]` → `prompt=null`, `title` из колонки как был.

## Удаление пользовательских ресурсов

Решение — [ADR-011](./adr/ADR-011-user-resource-deletion.md). **Soft-delete**: nullable-колонка
`deleted_at TIMESTAMPTZ` на `voice_profiles`, `tracks`, `assets` (миграция `0015_soft_delete`).
Удаление = `SET deleted_at = now()`; строки физически остаются (сохраняется история/биллинг,
восстановимо, идемпотентно).

Эндпоинты (owner-check; чужой/несуществующий/уже удалённый → `404`, успех → `204 No Content`):
- `DELETE /v1/voices/{id}` — soft-delete `VoiceProfile`. `VoiceConsent` не удаляется (юр. артефакт);
  голос у провайдера (`provider_voice_id`) не удаляется. Прошлые covers не ломаются — они снапшотят
  `target_voice`/`_target_voice_sample_url` в `job.input_payload` (FK на профиль нет).
- `DELETE /v1/tracks/{id}` — soft-delete `Track` (варианты остаются, но недостижимы вне трека).
  Уже созданные видео не затрагиваются (снапшот `audio_url`); новое видео из удалённого трека →
  `404 TRACK_NOT_FOUND`.
- `DELETE /v1/videos/{id}` — soft-delete video-`Asset` (`kind=video`, `meta.job_id == {id}`; `{id}` =
  `job_id`). `Job` не трогаем. Нет ассета → `404` (новый код `VIDEO_NOT_FOUND`).

Правила:
- **Фильтр `deleted_at IS NULL`** — только на пользовательских read/ownership-путях (`GET /v1/library`,
  `GET /v1/voices`, `GET /v1/tracks/{id}`, `GET /v1/videos/{id}`, резолв источника видео
  `_resolve_track_audio`). Внутренний путь записи результата (`get_by_job_id`, webhook/poller,
  `finalize`) — **не** фильтруется (иначе сломается дозапись вариантов/ассетов). Реализуется через
  явный `include_deleted`-параметр/отдельные методы, не глобально.
- **Монеты не возвращаются** (генерация оплачена, [ADR-005](./adr/ADR-005-coin-wallet-billing.md)).
- **Медиа на CDN провайдера не удаляются** — soft-delete скрывает нашу запись (Q-DEL-2).
- `DELETE /v1/uploads/{asset_id}` не вводится в этой итерации (входные ассеты не видны в UI, Q-DEL-1).

## Переименование пользовательских ресурсов

Решение — [ADR-012](./adr/ADR-012-user-resource-rename.md). Три `PATCH`-эндпоинта симметрично
`DELETE` (ADR-011). Успех → `200` с обновлённым объектом (прецедент `PATCH /v1/lyrics`, не `204`);
пустая/пробельная строка → `400 INVALID_INPUT`; чужой/несуществующий/удалённый → `404`. **Без
миграции** (`Track.title`/`VoiceProfile.name` уже есть; `title` видео — в `Asset.meta`).

- `PATCH /v1/tracks/{track_id}` — тело `{ title }` (≤255). Owner-check `TracksRepository.get`
  (фильтрует `deleted_at`) → `404 TRACK_NOT_FOUND`. Ответ `TrackResponse`.
- `PATCH /v1/voices/{voice_id}` — тело `{ name }` (≤120). Owner-check
  `VoiceRepository.get_active_profile` → `404 VOICE_PROFILE_NOT_FOUND`. Разрешено для любого
  не-удалённого профиля (в т.ч. `pending`). Ответ `VoiceProfileResponse`.
- `PATCH /v1/videos/{job_id}` — тело `{ title }` (≤255). Целевой объект — video-`Asset.meta["title"]`
  (`kind=video`, `meta.job_id == {id}`, `deleted_at IS NULL`). Owner-check по `Job` (owned +
  `job_type==video`) и наличию ассета → `404 VIDEO_NOT_FOUND` (rename до готовности видео → `404`,
  но `title` из `POST` сохранится при `_finalize`). Ответ `VideoResultResponse`.

Валидация единая: trim + `min_length=1` после trim (обязательное поле); храним trimmed. Инварианты:
не трогает монеты/ledger/jobs, не меняет `deleted_at`, идемпотентно (повтор тем же значением → `200`).
Схемы запросов: `RenameTrackRequest{title}` / `RenameVideoRequest{title}` / `RenameVoiceRequest{name}`.

**`title` видео (закрытие пробела ADR-007).** Ранее `CreateVideoRequest.title` принимался, но
`_finalize` его не сохранял (`Asset.meta` = `mode/style/aspect_ratio/quality_flag`) — терялся.
Теперь `_finalize` пишет `meta["title"] = derive_video_title(payload)`; `POST /v1/videos` с `title`
реально сохраняет его. Дефолт (без явного `title`) — детерминированная метка режима
(`Avatar Video`/`Visual Clip`/`Lyrics Video`, fallback `Music Video`), без генерации/сети
(симметрично `derive_track_title`, ADR-008 — избегаем «Untitled»). `VideoResultResponse` +=
`title?` (из `meta`); `LibraryItem.videos` отдаёт `title=meta.get("title")`.

## Кредиты и лимиты

Единый кошелёк монет (решение [ADR-005](./adr/ADR-005-coin-wallet-billing.md); переход с
мультивалютной модели, полный дизайн — [billing-coins-redesign.md](./billing-coins-redesign.md)):
- `CoinWallet(user, coins_available, coins_reserved)` — один баланс на пользователя, монеты
  non-expiring. Заменяет `Entitlement`/`CreditBalance` (per-category — удалены).
- `GenerationPrice(job_type, price_coins, active)` — прайс-лист: цена генерации в монетах.
  Стартовые дефолты: `song=10`, `cover=5`, `video=30`. Меняется admin-эндпоинтом без передеплоя.
- Списание **async job-путь** (`song`/`cover`/`video`, `create_job`): `reserve(price_of(job_type))`
  → `capture` (успех) / `release` (refund при провале), идемпотентно по `capture:{job.id}` /
  `release:{job.id}`, атомарно через `SELECT ... FOR UPDATE` на `coin_wallets`, аудит в
  `credit_ledger` (amount в монетах).
- Списание **sync-путь** (`POST /v1/lyrics`, [ADR-010](./adr/ADR-010-lyrics-sync-billing.md)):
  `LyricsService.generate` использует `CoinWalletService.charge(user_id, "lyrics")` (атомарный
  одношаговый debit, FOR UPDATE, 402 `INSUFFICIENT_CREDITS` до вызова fal при нехватке) →
  при любом сбое после списания `refund(...)` (компенсация). Резервный бакет не задействован
  (`coins_reserved` для lyrics = 0). Идемпотентность — по заголовку `Idempotency-Key`
  (иначе новый `uuid4()` на запрос). `debit_capture`/`credit_refund` в `credit_ledger`.
  Two-phase reserve→capture для sync отклонён (нет окна расчёта и recovery-раннера) — см. ADR-010.
- `voice_clone` бесплатен (async `Job`; при появлении цены биллится штатным reserve→capture).
  `lyrics` биллится sync-путём выше при наличии строки `generation_prices(lyrics)`; без строки
  или `price_coins=0` — бесплатно (charge вернёт 0, обратная совместимость).

## Биллинг

Прямой StoreKit 2: `providers/billing/apple.py` (верификация подписанных JWS-транзакций через App Store
Server API), webhook `POST /v1/webhooks/billing/apple` (App Store Server Notifications V2). Продукты —
пакеты монет (`coin_pack`, `grants={"coins":N}`) и подписки, начисляющие монеты за период; оба
пополняют единый `coin_wallets`. Restore — через `original_transaction_id`, идемпотентно по transaction.

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

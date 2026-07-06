# ADR-007 — Видео-генерация на 3 режима (Avatar / Visual Clip / Lyrics Video)

- Статус: Accepted
- Дата: 2026-07-06
- Контекст: видео-пайплайн (`app/domain/enums.py` (`VideoMode` — уже есть),
  `app/domain/schemas/videos.py`, `app/config.py`, `app/main.py`,
  `app/domain/providers/fal/{base,client,stub}.py`,
  `app/domain/services/generation_service.py`,
  `app/domain/services/pipelines/video.py`, `app/domain/services/audio_mixer.py`,
  `app/api/v1/videos.py`, `app/api/v1/uploads.py`; в образе есть ffmpeg; миграции — голова
  `0011_reseed_coin_products`)

## Context

iOS-дизайн экрана Create Video ждёт **три режима** генерации, каждый со своим сценарием ввода:

1. **Avatar Performance** — «поющий аватар» (липсинк): аудио + исходное видео/фото исполнителя.
2. **Visual Clip** — абстрактный/сюжетный видеоряд по промпту, поверх которого играет трек.
3. **Lyrics Video** — фон + бёрн-ин текста песни, тайминги строк выровнены по треку.

Плюс общие параметры: Style (Realistic / Cartoon / Anime / Cinematic), Aspect ratio
(1:1 / 3:4 / 4:3 / 9:16), prompt, референс-картинка, кнопка «Surprise me», источник аудио
«My track» (`trackId`).

Текущий backend поддерживает только avatar-липсинк: `CreateVideoRequest` требует
`audioUrl` + `sourceVideoUrl`, `mode` — **freeform-строка** с default `avatar_performance`;
`_provider_model` выбирает модель только по `job_type` (одна `FAL_VIDEO_MODEL`); пайплайн шлёт
в fal лишь `{video_url, audio_url}`. Enum `VideoMode` уже объявлен в `enums.py:152`
(`avatar_performance` / `visual_clip` / `lyrics_video`), но нигде не используется как тип.
`AssetKind.image` уже есть в enum. `JobStage.stage` — **нативный PG enum** (`job.py:72`,
`SAEnum(JobStage, name="job_stage", native_enum=True)`).

Владелец утвердил: реализовать **все 3 режима сразу**; **цена видео пока единая** (`video=30`,
ADR-005) — per-mode цена откладывается.

## Decision

`mode` становится типизированным `VideoMode` и **ветвит две вещи**: выбор fal-модели и набор
стадий пайплайна. Общий финал (`upload_cdn` → capture → `Asset` → `mark_succeeded` → push)
переиспользуется. **Все три режима async**: `start()` делает только fal-submit, тяжёлая
ffmpeg-стадия (mux/бёрн-ин) исполняется **в `advance()`** — фон webhook/поллера, не на request-пути
(образец `cover._mix` / `audio_mixer.py`; инвариант — §3a). Visual Clip и Lyrics Video добавляют
ffmpeg-стадию перед `upload_cdn` **в `advance()`** (термин «синхронная» относится к самому
ffmpeg-вызову внутри `advance()`, а не к request-пути `POST /v1/videos`).

### 1. Схема запроса `CreateVideoRequest` (переработка)

| Поле | Тип | Обяз. | Примечание |
|---|---|---|---|
| `mode` | `VideoMode` (enum) | да | было: freeform-строка с default |
| `audioUrl` | str \| null | по режиму | прямой URL трека |
| `trackId` | UUID \| null | по режиму | «My track» — источник аудио; для lyrics_video также источник лирики (резолв `track.job_id` → `Job.input_payload['_lyrics']`, см. §3) |
| `variantId` | UUID \| null | нет | конкретный вариант трека |
| `sourceVideoUrl` | str \| null | только avatar | из `/v1/uploads/source-video` |
| `referenceImageUrl` | str \| null | нет | из нового `/v1/uploads/image` |
| `style` | `VideoStyle` \| null | нет | realistic / cartoon / anime / cinematic |
| `aspectRatio` | `VideoAspect` \| null | нет | 1:1 / 3:4 / 4:3 / 9:16, default 9:16 |
| `prompt` | str \| null | по режиму | сюжет/описание |
| `lyrics` | str \| null | нет | явная лирика для lyrics_video, когда используется `audioUrl` без `trackId` |
| `surpriseMe` | bool = false | нет | серверный подбор промпта |
| `title` | str \| null | нет | |

`model_validator` по режиму (источник аудио = `audioUrl` ИЛИ `trackId`, ровно один требуется):
- **avatar_performance:** аудио + аватар (`sourceVideoUrl` **или** `referenceImageUrl`);
- **visual_clip:** аудио + (`prompt` **или** `surpriseMe`);
- **lyrics_video:** аудио + доступная лирика, где лирика берётся из **одного** источника: явное поле `lyrics` в запросе **или** `trackId` (тогда лирика резолвится из трека → его job → `Job.input_payload['_lyrics']`, см. §3, MAJOR-2). Для `lyrics_video` с `trackId` валидатор проверяет наличие `trackId`; фактическая доступность лирики трека проверяется на стадии `source_prep` (пустая лирика → деградация/`quality_flag`, а не отказ запроса).

`VideoResultResponse` дополняется `mode`, `aspectRatio`, `style` (читаются из `Asset.meta`).

### 2. Маппинг режима → fal-модель

Новые env-строки в `app/config.py` (+ проброс в `app/main.py`, `.env.example`):

| Env | Модель (умолчание) | Когда |
|---|---|---|
| `FAL_VIDEO_AVATAR_MODEL` | `fal-ai/kling-video/lipsync/audio-to-video` (текущий) | avatar + `sourceVideoUrl` |
| `FAL_VIDEO_AVATAR_IMAGE_MODEL` | `fal-ai/sync-lipsync/v3/image-to-video` | avatar + только `referenceImageUrl` |
| `FAL_VIDEO_VISUAL_MODEL` | `bytedance/seedance-2.0/text-to-video` | visual_clip без референса |
| `FAL_VIDEO_VISUAL_IMAGE_MODEL` | `bytedance/seedance-2.0/image-to-video` | visual_clip с `referenceImageUrl` |
| `FAL_VIDEO_LYRICS_BG_MODEL` | `bytedance/seedance-2.0/text-to-video` (t2v, задать дефолт — не пусто) | lyrics_video: фон под бёрн-ин лирики (**всегда** в V1, режим async) |

Существующая `FAL_VIDEO_MODEL` сохраняется как алиас `FAL_VIDEO_AVATAR_MODEL` для обратной
совместимости конфигов. Хелпер маппинга `mode` (+ наличие reference/source) → модель.
`generation_service._provider_model` принимает `payload` и для `JobType.video` вычисляет модель
по `mode` (сейчас маппинг только по `job_type`). **Инвариант:** `job.provider_model` обязан
совпадать с реально вызванной моделью — его опрашивает `FalPoller` (`poller.py:84`).

Провайдер fal (`providers/fal/client.py` + `base.py` Protocol + `stub.py` + wiring в `main.py` +
конструктор в `tests/test_fal_webhook_parse.py`) получает новые методы `submit_avatar_image_video`,
`submit_text_to_video`, `submit_image_to_video` (образец `submit_lipsync_video` / `_submit`).
Каждый шлёт **только поддерживаемые моделью поля** — fal отвечает 422 на лишние ([RISK-B1]).

### 3. Ветвление пайплайна `pipelines/video.py`

`start` диспетчеризует по `mode`; вводится `ASYNC_STAGES` для video:

- **Avatar Performance:** `source_video` → `submit_lipsync_video` (как сейчас); только
  `referenceImageUrl` → `submit_avatar_image_video`. Аудио вшито моделью — мукс не нужен.
  **Отдельной `JobStage` под image-ветку нет** — переиспользуется `JobStage.lipsync`: в `advance()`
  `completed_stage = lipsync`, чтобы совпасть с `current_stage`-guard (`video.py:76`);
  `idempotency_key = {job.id}:avatar` (video-ветка использует `:lipsync`).
- **Visual Clip:** `prepare_prompt` → `visual_gen` (async fal t2v/i2v) → **`mux_audio`**
  (ffmpeg, синхронно) → `upload_cdn` → `finalize`. Деградация при сбое ffmpeg — немое видео +
  `quality_flag` (образец `cover._mix`). **Длительность (MAJOR-3):** fal t2v/i2v (seedance)
  выдаёт короткий клип на **секунды**, а трек — **минуты**; наивный `mux_audio -shortest` обрезал
  бы результат до длины короткого видео → музыкальное видео в пару секунд. Поэтому границей
  длительности ставится **длина аудио-трека** (`probe_duration_seconds`), а короткое видео
  **зацикливается/растягивается** под неё (`ffmpeg -stream_loop` / concat входного клипа), затем
  муксится аудио. **Рекомендуемая стратегия — зацикливание видео под длину аудио.** Реализация
  цикла в `video_mux.mux_audio_into_video` — **задача backend**.
- **Lyrics Video ([RISK-B2]) — АСИНХРОННЫЙ, симметричен visual_clip (MAJOR-6):**
  `prepare_prompt` → `source_prep` (лирика + длительность, **дёшево**, без ffmpeg) →
  **`visual_gen`** (async fal text-to-video фон, модель `FAL_VIDEO_LYRICS_BG_MODEL`) →
  [webhook/poller → `advance`] → **`lyrics_render`** (ffmpeg subtitles/drawtext поверх
  сгенерированного фона) → **мукс аудио** (длина = длина трека) → `upload_cdn` → `finalize`.
  **Ключевой инвариант (см. §3a):** `start()` для lyrics_video выполняет **только** fal-submit
  фоновой t2v-генерации (быстро, без скачивания/рендера/upload) → возвращает `request_id`,
  ставит `provider_model = FAL_VIDEO_LYRICS_BG_MODEL` и `current_stage = visual_gen`, POST
  отдаёт **202 сразу**. Вся тяжёлая ffmpeg-работа (`render_lyrics_video` = бёрн-ин лирики
  поверх фона + мукс аудио) и upload выполняются **в `advance()`** (фон webhook/поллера),
  образец `cover._mix` / `visual_clip.mux_audio`. `advance()` при `completed_stage == visual_gen`
  ветвит по `job.input_payload['mode']`: `visual_clip` → `mux_audio`; `lyrics_video` →
  `render_lyrics_video` (фон = `media_url` от fal) + мукс → `_finalize`.
  **V1-синхронизация лирики — равномерное распределение строк по длительности** трека
  (`probe_duration_seconds`), генерация `.ass`/`.srt` (остаётся [TD-004], но исполняется async).
  **Источник лирики (MAJOR-2):** song-пайплайн пишет `_lyrics` в **`Job.input_payload`** самой
  задачи-песни (`song.py:37,58` через `_update_payload`), а `Track` создаётся с
  `meta={"runtime": runtime}` (`song.py:243`) — **в `Track.meta` ключа `_lyrics` НЕТ**. Поэтому при
  `trackId` лирика резолвится через **обратный доступ track→job**: `Track.job_id` (колонка уже есть
  в модели, `track.py:46`) → `JobsRepository.get_by_id(track.job_id)` → `Job.input_payload['_lyrics']`.
  `TracksRepository.get_by_job_id` — это прямой резолв job→track; обратный (`track → job_id → Job`)
  нужно добавить — **задача backend**. **Альтернатива:** song-пайплайн дополнительно пишет `_lyrics`
  в `Track.meta` при создании трека (доп. правка `song.py` — **задача backend**), тогда чтения `Job`
  не требуется. Второй прямой источник — явное поле `lyrics` запроса (когда `audioUrl` без `trackId`).
  Фон V1 — **генеративный fal t2v** (`FAL_VIDEO_LYRICS_BG_MODEL`), поверх которого бёрнится лирика.
  **Async-инвариант (MINOR-5, переработан → MAJOR-6):** lyrics_video **async** и симметричен
  visual_clip — `job.provider_model = FAL_VIDEO_LYRICS_BG_MODEL` (**не** `None`),
  `current_stage = visual_gen`, `provider_request_id` выставлен в `start()`; webhook/poller
  **участвуют** (`FalPoller` отбирает job по `provider_request_id` через `list_active_with_request`
  и опрашивает `provider_model`, `poller.py:70,84`) — как у visual_clip. Инварианты монет
  (`reserve` в `create_job`, `capture` в `_finalize`, `release` в `_mark_failed`) — идентичны
  остальным режимам. **Отвергнутая для V1 альтернатива — статический/градиентный фон без fal**
  (`provider_model = None`, полностью синхронный пайплайн): требует, чтобы весь ffmpeg-рендер
  выполнялся вне request-пути, а фонового job-runner’а под синхронные задачи в системе **нет**
  (`start()` вызывается инлайн в `create_job` → HTTP-хендлер `POST /v1/videos`,
  `generation_service.py:192`). Такой синхронный рендер заблокировал бы POST на минуты (таймаут
  Traefik/uvicorn, занятый воркер) и оставил бы job `running` без `provider_request_id`, который
  `recover_orphan_jobs` пометит `failed` при рестарте (`recovery.py:16`). Поэтому статический фон
  отложен как tech-debt до появления background job runner ([TD-005]; см. Alternatives и §3a).

Новый модуль `app/domain/services/video_mux.py` (образец `audio_mixer.py`): `ffmpeg_available`,
`mux_audio_into_video(...)`, `render_lyrics_video(...)`. `_finalize` дополняет `Asset.meta`
полями `mode` / `style` / `aspect_ratio` / `quality_flag`.

### 3a. ИНВАРИАНТ: `start()` всегда быстрый; тяжёлый ffmpeg — только в `advance()` (MAJOR-6)

**Инвариант (обязателен для всех режимов video, нарушение = блокер ревью):**
`pipeline.start()` вызывается **инлайн** в `create_job` внутри HTTP-хендлера `POST /v1/videos`
(`generation_service.py:192` → `runner.py:53` → `pipeline.start`). Поэтому `start()` во **всех**
режимах обязан быть **быстрым**: допустимы только дешёвые операции (валидация, резолв промпта/лирики,
`probe_duration_seconds`) и **fal-submit** (сетевой вызов, возвращающий `request_id`). После submit
`start()` через `_set_current_stage` фиксирует `current_stage` + `provider_request_id` и **немедленно
возвращает управление** → POST отдаёт `202`.

**Запрещено в `start()` (и в любом коде на request-пути `POST /v1/videos`):** синхронное
скачивание медиа, ffmpeg-рендер/мукс/бёрн-ин лирики на всю длину трека, upload результата в
storage. Всё это — **тяжёлые стадии**, которые исполняются **только в `advance()`** (фон
webhook/поллера, вне HTTP-запроса), образец: `cover._mix` работает в `advance()`→`_finalize`,
`visual_clip.mux_audio` — в `advance()`. Причина: `advance()` дёргает `FalPoller._apply` /
webhook-route, а не HTTP-хендлер пользователя.

**Что ломает нарушение инварианта** (исходный дефект синхронного lyrics_video): POST не отдаёт
`202`, пока не завершится минутный рендер → таймаут Traefik/uvicorn, занятый event-loop/воркер;
job висит `running` без `provider_request_id` и распознаётся `recover_orphan_jobs` как orphan →
помечается `failed` при рестарте (`recovery.py:16`). Симметричный async через fal-submit устраняет
всё это: job сразу получает `provider_request_id`, поллер его ведёт, тяжёлая работа — в `advance()`.

### 4. Новые enum и стадии (миграция PG enum)

`app/domain/enums.py`:
- новые `JobStage`: `visual_gen`, `mux_audio`, `lyrics_render` (+ опционально `align_lyrics`
  под V2 форс-алайнмент);
- новые `VideoStyle` (`realistic` / `cartoon` / `anime` / `cinematic`) и
  `VideoAspect` (`1:1` / `3:4` / `4:3` / `9:16`).

Поскольку `job_stage` — **нативный PG enum** (`job.py:72-73`, `128-129`), новые значения
`JobStage` требуют миграции **`ALTER TYPE job_stage ADD VALUE`** (в autocommit-блоке; см.
[RISK-B6]). `VideoStyle` / `VideoAspect` / `VideoMode` в БД **как строки в `Asset.meta`** —
отдельного PG-типа и миграции не требуют. `AssetKind.image` уже присутствует в enum — backend
обязан подтвердить, что PG-тип `asset_kind` уже содержит `image` (иначе `ADD VALUE`).

### 5. Image-upload + trackId + Surprise me

- `POST /v1/uploads/image` (`api/v1/uploads.py`, образец `source-video`, `AssetKind.image`);
  `config.py` — `UPLOAD_IMAGE_CONTENT_TYPES` + computed-свойство.
- **trackId → audio_url:** резолв в `create_video` до `create_job` через `TracksRepository`
  (проверка `Track.user_id == current.id`, взять `TrackVariant.audio_url`; для lyrics_video —
  вытащить лирику). `create_video` получает `get_sessionmaker` в сигнатуру.
- **Surprise me (V1, серверный):** случайный шаблон из `prompt_presets` (`PresetKind.prompt`),
  резолв в `prepare_prompt`, проходит `moderation.screen_text`.

### 6. Цена — единая (V1)

`video = 30` (ADR-005), биллинг не трогаем. Инварианты монет сохраняются: `reserve` в
`create_job`, `capture` в `_finalize._capture_credits`, `release` в `_mark_failed`; все новые
ветки `start`/`advance` при безвозвратной ошибке идут через `_mark_failed`. Per-mode цена —
**открытый вопрос [Q-VIDEO-1]** (см. ниже).

Прочие инварианты: `idempotency_key` per-stage уникален (`{job.id}:lipsync` для avatar-video,
`{job.id}:avatar` для avatar-image, `{job.id}:visual` для visual_clip, `{job.id}:lyrics_bg` для
lyrics_video-фона); `advance` идемпотентен через `current_stage`-guard. И visual_clip, и
lyrics_video завершают async-стадию на `JobStage.visual_gen` — `advance()` различает их по
`job.input_payload['mode']` (visual_clip → `mux_audio`; lyrics_video → `render_lyrics_video` +
мукс). Avatar-image-ветка (`submit_avatar_image_video`) **переиспользует `JobStage.lipsync`**
(отдельной стадии нет), поэтому её `completed_stage` в `advance()` = `lipsync` и совпадает с
`current_stage`-guard. `VIDEO_JOB_HARD_TIMEOUT_SECONDS=5400`.

## Consequences

- (+) Один эндпоинт `POST /v1/videos` покрывает 3 сценария; iOS шлёт `mode` + режимные поля.
- (+) Модель и стадии выбираются данными (`mode` + env-модели) — новый режим/модель добавляется
  локально, без переписывания диспетчера.
- (+) ffmpeg-стадии (`mux_audio` / `lyrics_render`) переиспользуют паттерн `cover._mix` с
  деградацией через `quality_flag` — частичный успех вместо полного провала.
- (+) Биллинг/инварианты монет и fal-контракт (конверт webhook) не меняются — только новые
  submit-методы и стадии.
- (−) **Ломающее изменение `CreateVideoRequest`:** `mode` из freeform-строки с default стал
  обязательным `VideoMode`-enum; добавлены поля (`trackId`/`referenceImageUrl`/`style`/
  `aspectRatio`/`prompt`/`surpriseMe`); `audioUrl` и `sourceVideoUrl` больше не всегда
  обязательны, а условны по режиму. Старые клиенты, слашие только `{audioUrl, sourceVideoUrl}`
  без `mode`, получат **400 `INVALID_INPUT`** (схемная валидация тела: отсутствие обязательного
  `mode` и провал `model_validator._validate_by_mode` — оба через `RequestValidationError` →
  `validation_handler`, ADR-007 не меняет этот контракт; фикс `validation_handler` гарантирует
  именно 400, а не 422/500). Endpoint-level `ValidationFailed` (уже после схемы — `unknown_variant`,
  `track_has_no_audio` при резолве `trackId`, `unknown_voice` для cover) отдаёт **422**. Допустимо:
  интеграция iOS не завершена.
- (−) Требуется миграция `ALTER TYPE job_stage ADD VALUE` (нативный PG enum) — рискованная
  операция в autocommit; несовместима с авто-rollback (см. [TD-001]).
- (−/[RISK-B1]) Точные поля fal video-моделей различаются и не гарантированы — шлём только
  поддерживаемые, модели вынесены в env, требуется сверка полей по fal-доке до реального вызова
  (см. [RISK-B1]).
- (−/MAJOR-3) Visual Clip: fal t2v/i2v выдаёт клип на секунды, трек — на минуты. Наивный
  `mux_audio -shortest` обрезал бы музыкальное видео до длины короткого клипа. Стратегия V1 —
  зациклить/растянуть видео под длину аудио-трека (`-stream_loop`/concat) и ставить границей
  длину трека, а не видео. Реализация цикла — задача backend; при недоступности ffmpeg действует
  общая деградация (`quality_flag`).
- (−/MAJOR-2) Источник лирики для lyrics_video — `Job.input_payload['_lyrics']` (не `Track.meta`).
  Резолв по `trackId` требует обратного доступа `track.job_id → Job` (нет готового метода — задача
  backend) либо дополнительной записи `_lyrics` в `Track.meta` в `song.py` (альтернативная задача
  backend). Прямой источник без трека — поле `lyrics` запроса.
- (−/[RISK-B2] → [TD-004]) Lyrics Video V1 — равномерная (неточная) синхронизация строк;
  форс-алайнмент отложен в tech-debt. Исполнение стадии `lyrics_render` теперь **асинхронное**
  (в `advance()`), но равномерность таймингов остаётся V1-ограничением.
- (+/MAJOR-6) Lyrics Video переведён в **async** и симметричен visual_clip: `start()` делает
  только fal-submit t2v-фона (`FAL_VIDEO_LYRICS_BG_MODEL`), POST отдаёт `202` мгновенно; весь
  ffmpeg-рендер (бёрн-ин лирики + мукс) и upload — в `advance()`. Устранён MAJOR-дефект блокировки
  request-пути и ложного orphan-recovery (см. §3a). Инварианты монет и участие поллера — как у
  остальных режимов.
- (−/MAJOR-6) Статический/градиентный фон без fal для lyrics_video **отложен** (нужен background
  job runner, которого нет) — см. Alternatives и [TD-005]. В V1 lyrics_video **всегда** тратит fal
  t2v-генерацию (стоимость и время как у visual_clip).
- (−/[RISK-B3]) aspect ratio поддержан не всеми моделями — best-effort кроп/пад ffmpeg; точное
  соответствие не гарантируется.
- (−/[RISK-B4]) Стоимость реальной генерации — CI гоняет только на стабе; smoke реальных
  режимов за флагом, вручную (стоит монет).

## Open questions

- **[Q-VIDEO-1] Per-mode цена видео.** V1 — единая `video=30`. Разные режимы имеют разную
  реальную себестоимость (t2v дороже липсинка). Введение per-mode цены потребует pricing-ключа,
  отличного от `job_type.value`, и проверки, что `capture` списывает `job.reserved_credits`,
  а не делает повторный lookup цены (**[RISK-B5]** — иначе рассинхрон резерва и списания).
  Решение отложено до появления реальной статистики себестоимости. Не блокирует V1.

## Alternatives

- **Оставить один режим (avatar-only), доложить остальные позже.** Отклонено: владелец
  утвердил все 3 режима сразу; дизайн iOS уже рассчитан на выбор режима.
- **Отдельные эндпоинты на режим (`/videos/avatar`, `/videos/visual`, `/videos/lyrics`).**
  Отклонено: дублирование резерва/финализации/webhook-контракта; `mode`-диспетчеризация в одном
  пайплайне переиспользует общий финал и инварианты монет.
- **Мукс аудио и бёрн-ин лирики через отдельный async-сервис/очередь.** Отклонено:
  переусложнение; ffmpeg синхронно в стадии (образец `cover._mix`) достаточно, деградация через
  `quality_flag`.
- **Форс-алайнмент лирики (whisperx/aeneas/STT) сразу в V1.** Отклонено: тяжёлая зависимость,
  доп. стадия и стоимость; V1 — равномерное распределение, V2 — отдельная стадия `align_lyrics`
  (см. [TD-004]).
- **Статический/градиентный фон для Lyrics Video без fal (полностью синхронный пайплайн,
  `provider_model=None`).** Отклонено для V1: `start()` вызывается инлайн в request-пути
  `POST /v1/videos`, а фонового job-runner’а под тяжёлые синхронные ffmpeg-задачи вне HTTP-запроса
  в системе нет. Синхронный рендер на всю длину трека заблокировал бы POST (таймаут Traefik/uvicorn)
  и оставил бы job orphan-ом для `recover_orphan_jobs`. Поэтому V1 использует **генеративный fal
  t2v-фон** (async, симметрично visual_clip); статический фон без fal — tech-debt до появления
  background job runner ([TD-005], отдельный worker + очередь). Прежняя формулировка
  «дефолт V1 — статический фон, fal за флагом» **отменена** (MAJOR-6).
- **Хранить `style`/`aspect_ratio` отдельными колонками/enum на `assets`.** Отклонено: это
  атрибуты одного результата, живут в `Asset.meta` (как прочая метаинформация генерации), без
  новых PG-типов.

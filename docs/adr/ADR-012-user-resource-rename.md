# ADR-012 — Переименование пользовательских ресурсов (tracks / voices / videos)

- Статус: Accepted
- Дата: 2026-07-09
- Контекст: фидбек ревью iOS App Store — у пользователя нет возможности переименовать свои
  треки, клоны голоса и видео (симметрично удалению из ADR-011). Требуется добавить «Rename».
- Связанные: [ADR-011](./ADR-011-user-resource-deletion.md) (soft-delete, owner-check → 404),
  [ADR-008](./ADR-008-demucs-stems-and-track-metadata.md) (метаданные/автозаголовок трека),
  [ADR-010](./ADR-010-lyrics-sync-billing.md) (прецедент `PATCH` → 200 с объектом),
  [ADR-007](./ADR-007-video-three-modes.md) (видео = `Asset(kind=video)` с `meta.job_id`).

## Контекст

Пользователь видит три вида результатов в медиатеке (`GET /v1/library`), каждый с отображаемым
именем:

- **Трек** — `Track.title` (`String(255)`, nullable). Уже отдаётся в `TrackResponse` и
  `LibraryItem`; с ADR-008 заполняется непустым автозаголовком при создании.
- **Голос** — `VoiceProfile.name` (`String(120)`, nullable). Отдаётся в `VoiceProfileResponse`.
- **Видео** — `Asset(kind=video)`, адресуется по `job_id` (`meta.job_id`). Отдельной колонки/поля
  `title` **нет**: `Asset` = `id/user_id/kind/url/mime/duration/size/meta/deleted_at`.

Ключевые наблюдения по коду (влияют на решение):

1. **`title` видео сейчас теряется.** `CreateVideoRequest.title` (`schemas/videos.py:62`,
   `str|None`, `max_length=255`) принимается и попадает в `payload` (`create_video`:
   `body.model_dump(exclude_none=True)`), но `VideoPipeline._finalize` пишет в `Asset.meta`
   только `{job_id, mode, style, aspect_ratio, quality_flag}` — `title` **не сохраняется**.
   `VideoResultResponse` и `LibraryItem.videos` его не отдают. Поле фактически «проглатывается» —
   это пробел, закрываемый вместе с rename.
2. **Прецедент `PATCH` → 200 с объектом.** `PATCH /v1/lyrics/{draft_id}` (`lyrics.py:62`) отдаёт
   `200` с обновлённым `LyricsDraftResponse`; `UpdateLyricsRequest` = `{content}` с `min_length=1`
   (пустая строка → `400`). `DELETE`-эндпоинты (ADR-011) отдают `204` — иная семантика (нечего
   возвращать).
3. **Owner-check + soft-delete фильтр уже есть.** `TracksRepository.get` и
   `VoiceRepository.get_active_profile` фильтруют `deleted_at IS NULL`; `get_video`/`delete_video`
   резолвят video-`Asset` с `deleted_at IS NULL`. Чужой/несуществующий/удалённый → `404`.
4. **`updated_at` бампается автоматически.** `TimestampMixin` (`updated_at`, `onupdate=func.now()`)
   на `tracks`/`voice_profiles`/`assets` — любое `UPDATE` строки (title/name/meta) обновит
   `updated_at` без ручного кода.

## Решение

### 1. Три эндпоинта `PATCH` (симметрично `DELETE` из ADR-011)

| Метод / путь | Тело | Целевой объект | Успех |
|---|---|---|---|
| `PATCH /v1/tracks/{track_id}` | `{ "title": "..." }` | `Track.title` | `200 TrackResponse` |
| `PATCH /v1/voices/{voice_id}` | `{ "name": "..." }` | `VoiceProfile.name` | `200 VoiceProfileResponse` |
| `PATCH /v1/videos/{job_id}` | `{ "title": "..." }` | video-`Asset.meta["title"]` | `200 VideoResultResponse` |

`{id}` для tracks/voices — id ресурса; для videos — `job_id` (как в `GET/DELETE /v1/videos/{job_id}`).

**Код ответа — `200` с обновлённым объектом (не `204`).** Обоснование:
- Прецедент проекта — `PATCH /v1/lyrics/{id}` → `200` с объектом (наблюдение 2).
- Клиент (iOS) обновляет строку списка сразу из ответа, без дополнительного `GET`.
- PATCH-with-body-return идиоматичен. `204` уместен для `DELETE` (нечего возвращать), но не здесь.
- Response-схемы уже существуют (`TrackResponse`/`VoiceProfileResponse`/`VideoResultResponse`) —
  переиспользуем, новых read-схем не заводим.

**PATCH /v1/tracks/{track_id}:**
- Owner-check через `TracksRepository.get(track_id)` (фильтрует `deleted_at IS NULL`).
  `None` или `track.user_id != current.id` → `404 TRACK_NOT_FOUND`.
- `UPDATE tracks SET title = :title` (значение — trimmed, см. §3). Не трогаем `meta`/`prompt`/`job_id`.
- Ответ — полный `TrackResponse` (перечитываем варианты, как в `get_track`).

**PATCH /v1/voices/{voice_id}:**
- Owner-check через `VoiceRepository.get_active_profile(voice_id)` (фильтрует `deleted_at IS NULL`,
  как в `DELETE /v1/voices/{id}`). `None`/чужой → `404 VOICE_PROFILE_NOT_FOUND`.
- `UPDATE voice_profiles SET name = :name`. Не трогаем `status`/`provider_voice_id`/`consent_id`.
- Разрешено для любого не-удалённого профиля (в т.ч. `pending`/`failed`) — переименование не
  зависит от готовности клона.
- Ответ — `VoiceProfileResponse` (как в `list_voices`; `job_id` в ответе rename = `null`).

**PATCH /v1/videos/{job_id}:**
- Owner-check как в `DELETE /v1/videos/{id}`: `Job` существует, `user_id == current.id`,
  `job_type == video`; иначе `404 VIDEO_NOT_FOUND`.
- Затем резолв video-`Asset` (`kind=video`, `meta.job_id == {id}`, `deleted_at IS NULL`).
  Нет ассета (видео не готово / уже удалено) → `404 VIDEO_NOT_FOUND` (нечего переименовывать).
- `title` хранится в `meta` (§2). JSONB не мутируется in-place — **реассайн словаря**:
  `asset.meta = {**(asset.meta or {}), "title": <trimmed>}`.
- Ответ — `VideoResultResponse` (как `get_video`, с новым `title` из `meta`).
- **Переименование до готовности видео** (ассета ещё нет) → `404`. При этом `title`, переданный в
  `POST /v1/videos`, всё равно сохранится при `_finalize` (§2) — потери нет; повторный rename
  доступен после готовности. Это осознанное следствие «rename целится в video-asset», симметрично
  `DELETE` (ADR-011 §3, расхождение GET vs DELETE/PATCH по коду — то же обоснование).

### 2. Хранение `title` видео — `Asset.meta["title"]` (без миграции)

Выбор: `Asset.meta["title"]`, а **не** новая колонка `Asset.title`.

Обоснование:
- **Минимализм / без миграции.** `meta` (JSONB) уже хранит `mode`/`style`/`aspect_ratio`/
  `quality_flag`; `title` — такой же презентационный атрибут видео-результата. Колонка потребовала
  бы миграцию `0016` и backfill, не давая выигрыша (по `title` не фильтруем/не джойним).
- **Симметрия** с существующей моделью видео-метаданных (все презентационные поля — в `meta`).
- **Читатели уже готовы** доставать из `meta` (`get_video`, `library` читают `Asset.meta`).

Требуемые изменения (закрывают пробел «title теряется»):
- **`VideoPipeline._finalize`** — в `meta` добавить `"title": derive_video_title(payload)` (payload =
  `job.input_payload`). Теперь `POST /v1/videos` с `title` **реально сохраняет** его.
- **`VideoResultResponse`** += `title: str | None` (из `meta.get("title")`); заполняется в
  `get_video` и в ответе `PATCH /v1/videos/{id}`. Изменение **аддитивное**.
- **`LibraryItem` для видео** — заполнять `title=v.meta.get("title")` (поле `title` в `LibraryItem`
  уже есть; сейчас у видео-элементов не заполняется → `null`). Изменение **аддитивное**.

### 3. Валидация и пустая строка — единый контракт для всех трёх

- **Длины (schema).** `title` трека ≤ 255, `title` видео ≤ 255, `name` голоса ≤ 120 (совпадают с
  колонками `Track.title` String(255) / `VoiceProfile.name` String(120); для видео — тот же
  лимит 255, что и `CreateVideoRequest.title`).
- **Trim.** Значение обрезается по краям (strip) перед проверкой и сохранением (храним trimmed).
- **Инвариант: явный `title` видео сохраняется как ввёл пользователь** — только trim, длина ≤ 255
  (лимит гарантирован схемой `CreateVideoRequest.title` / `RenameVideoRequest.title`, `max_length=255`).
  **Никакого усечения до 40 симв.** Ключевое следствие: **create и rename дают идентичный результат
  для одного и того же введённого title** — `POST /v1/videos` с `title` 41–255 симв. и последующий
  `PATCH /v1/videos/{id}` тем же значением сохраняют строку одинаково (обе ветки = trim, без потери
  ввода). Это устраняет расхождение медиатеки между create-путём и rename-путём.
- **Пустая строка / только пробелы → `400`** (а не «очистка в null»). Единообразно для трёх ресурсов.
  Реализация: поле **обязательное**, после strip `min_length=1`; иначе схемная валидация →
  `RequestValidationError` → **`400 INVALID_INPUT`** (единый контракт валидации, ARCHITECTURE
  «Статусы валидации»). Симметрично `UpdateLyricsRequest.content` (`min_length=1`).
  Обоснование выбора «reject» над «empty = clear null»:
  - «Rename» семантически = задать осмысленное имя, а не очистить его; очистка — не сценарий iOS.
  - Убирает двусмысленность `""` (пусто) vs `null` (не передано) в PATCH.
  - Инвариант: после успешного rename `title`/`name` **всегда непустой** (согласуется с ADR-008 —
    трек не показывает «Untitled»).

**Схемы запросов (camelCase, `CamelModel`):** три отдельные —
`RenameTrackRequest { title: str }`, `RenameVideoRequest { title: str }`,
`RenameVoiceRequest { name: str }`. Отдельные (а не одна общая) — потому что поле различается
(`title` vs `name`) и лимит различается (255 vs 120). Общий strip-валидатор (trim + reject empty)
допустимо вынести в переиспользуемый helper/mixin — деталь реализации, не контракт.

### 4. Дефолтный `title` видео — детерминированный, непустой

Если `POST /v1/videos` не передал `title`, при `_finalize` подставляется детерминированный дефолт
(симметрично `derive_track_title` из ADR-008 — «без генерации/сетевых вызовов», избегаем «Untitled»).

Helper `derive_video_title(payload)` (например `app/domain/services/video_title.py`, тестируемый):
1. **explicit `payload["title"]`** (задан пользователем): **только trim, вернуть как есть** —
   **без усечения**. Лимит ≤ 255 уже гарантирован схемой `CreateVideoRequest.title` (`max_length=255`),
   так что дополнительная обрезка не нужна и запрещена (иначе create потерял бы ввод, а rename — нет).
2. **иначе (title не задан) — дефолт из `payload["mode"]`**: фиксированная человекочитаемая метка
   режима — `avatar_performance → "Avatar Video"`, `visual_clip → "Visual Clip"`,
   `lyrics_video → "Lyrics Video"`;
3. **fallback** (неизвестный/пустой mode) → `"Music Video"`.

**Усечение (по границе слова + `…`) в `derive_video_title` НЕ применяется вовсе.** Обрезка по границе
слова оправдана только для дефолта, деривируемого из источника **переменной длины** — как в
`derive_track_title` (ADR-008), где дефолт трека строится из промпта. Здесь такого источника нет:
дефолт видео = фиксированные короткие метки режима (усекать нечего), а промпт как источник дефолта
**не** берётся (см. ниже). Поэтому `_truncate`-логика из хелпера убрана полностью.

Обоснование «непустой дефолт» над `null`: единообразие медиатеки (все элементы имеют имя, как
треки после ADR-008). Промпт как источник дефолта **не** берём — для visual/lyrics он часто
серверный (`DEFAULT_VISUAL_PROMPT`/`DEFAULT_LYRICS_BG_PROMPT`), метка режима чище и стабильнее.
Пользовательский `title` (когда задан) — первичен, сохраняется как введён (только trim, без усечения)
и не подменяется меткой.

> Отличие от трека: у `derive_track_title` (ADR-008) явный title усекается до 40 симв. — там это
> осознанное решение по треку, которое **не** переносится на видео. Для видео явный title
> сохраняется целиком (≤255), чтобы create и rename были согласованы.

### 5. Инварианты переименования

- **Не трогает биллинг.** Rename не читает/не пишет `coin_wallet`/`credit_ledger`/`usage_event` и
  не создаёт `jobs` — бесплатная операция (в отличие от генерации). Прайс-листа для rename нет.
- **Не меняет `deleted_at`.** Обновляется только `title`/`name`/`meta.title` (+ авто-`updated_at`).
- **Идемпотентно.** Повторный `PATCH` тем же значением → `200` с тем же объектом (не ошибка).
- **Удалённый ресурс не переименовывается** → `404` (owner-check фильтрует `deleted_at IS NULL`,
  §1). Симметрично `DELETE` (ADR-011 §3).
- **Owner-check → `404`** (не `403`) — не раскрываем существование чужих ресурсов (как GET/DELETE).

### 6. Миграция — не требуется

- Трек: колонка `Track.title` (String255) уже есть.
- Голос: колонка `VoiceProfile.name` (String120) уже есть.
- Видео: `title` в `Asset.meta` (JSONB) — новый ключ, схема БД не меняется.

Head миграций остаётся `0015_soft_delete`. Миграция `0016` **не заводится**. (Колонка `Asset.title`
рассматривалась и отвергнута — см. §2 и «Альтернативы».)

### 7. Контракт `GET /v1/library`: `id` видео-элемента = `job_id` (ЛОМАЮЩЕЕ, семантика `id`)

**Проблема (подтверждена backend-reviewer).** `LibraryItem` для видео отдаёт `id = Asset.id`, тогда как
весь video-API адресуется по `job_id`: `GET /v1/videos/{job_id}`, `DELETE /v1/videos/{job_id}` (ADR-011),
`PATCH /v1/videos/{job_id}` (§1). Клиент, получив список библиотеки, **не имеет `job_id`** и не может
открыть / переименовать / удалить видео — `Asset.id` не принимается ни одним эндпоинтом. Дефект затрагивает
и уже задеплоенное удаление (ADR-011), и вводимый rename (§1).

**Инвариант контракта library (устанавливается этим решением).**
> `id` элемента `GET /v1/library` = идентификатор, который принимают соответствующие ресурсу эндпоинты.

Проверка по коду (все три типа приводятся к единому правилу):
- **Трек** — `library.id = Track.id`; принимается `GET/PATCH/DELETE /v1/tracks/{track_id}`. Уже консистентно.
- **Голос** — `library.id = VoiceProfile.id`; принимается `GET/PATCH/DELETE /v1/voices/{voice_id}`. Уже консистентно.
- **Видео** — сейчас `library.id = Asset.id`, а эндпоинты принимают `job_id`. **Аномалия — исправляется.**

**Решение — вариант B:** для видео `LibraryItem.id = Asset.meta["job_id"]` (то, чем адресуется ресурс во
всём video-API). `Asset.id` наружу в library **не отдаётся** (симметрично `get_video`/`delete_video`, которые
Asset.id никогда не раскрывают). `job_id` гарантированно присутствует: video-пайплайн (`_finalize`) всегда
пишет `meta["job_id"]`, и любой адресуемый video-`Asset` резолвится именно по нему.

**Почему B, а не A (добавить отдельное поле `jobId`, `id` оставить `Asset.id`):**
- **Единый инвариант вместо частного исключения.** С B клиент всегда адресует элемент библиотеки по
  `item.id` — единообразно для tracks/voices/videos. С A пришлось бы кодировать асимметрию «для видео
  игнорируй `id`, бери `jobId`».
- **Убирает footgun.** При A `id = Asset.id` остаётся мёртвым значением, которое не принимает ни один
  эндпоинт, — прямое приглашение к тому же багу (`DELETE /v1/videos/{item.id}` → 404).
- **Симметрия с треками** (`id` = Track.id, он же в `/tracks/{id}`) прямо указывает: для видео `id` обязан
  быть тем, чем адресуют ресурс.
- Вариант A (аддитивный, неломающий) отвергнут: техническая безопасность миграции не окупает постоянную
  асимметрию контракта и сохранение бесполезного `Asset.id`.

**Почему это допустимо как ЛОМАЮЩЕЕ.** Меняется **семантика значения** `LibraryItem.id` для video-элементов
(было `Asset.id` → стало `job_id`). Формально ломающее, но **радиус поражения ≈ ноль**: старое значение
(`Asset.id`) не принималось ни одним эндпоинтом и клиенту бесполезно; iOS ещё в интеграции. Изменение не
ломает существующую функциональность — оно её впервые включает (list → open/rename/delete видео).
Форма поля не меняется: `id: str` остаётся `str`.

**Изменение `LibraryItem` — только для ветки видео (tracks/voices не трогаем):**
- `videos[].id` = `str(v.meta["job_id"])` (было `str(v.id)` = `Asset.id`).
- `tracks[].id` = `Track.id`, `voices[].id` = `VoiceProfile.id` — без изменений.
- Схема `LibraryItem` (`id: str`) не меняется — правится только значение, подставляемое в ветке видео.

## Указания backend (backend_instructions)

- `PATCH /v1/tracks/{track_id}` (`api/v1/tracks.py`): `RenameTrackRequest{title}`; owner-check
  `TracksRepository.get` (уже фильтрует deleted) → `404 TRACK_NOT_FOUND`; `SET title`; вернуть
  `TrackResponse` (перечитать варианты). В транзакции (`session.begin()`).
- `PATCH /v1/voices/{voice_id}` (`api/v1/voices.py`): `RenameVoiceRequest{name}`; owner-check
  `VoiceRepository.get_active_profile` → `404 VOICE_PROFILE_NOT_FOUND`; `SET name`; вернуть
  `VoiceProfileResponse` (`job_id=null`).
- `PATCH /v1/videos/{job_id}` (`api/v1/videos.py`): `RenameVideoRequest{title}`; owner-check по
  `Job` (owned + `job_type==video`) → `404 VIDEO_NOT_FOUND`; резолв video-`Asset`
  (`deleted_at IS NULL`), нет → `404 VIDEO_NOT_FOUND`; `asset.meta = {**(asset.meta or {}),
  "title": trimmed}` (реассайн — JSONB не мутируется in-place); вернуть `VideoResultResponse`.
- Схемы (`domain/schemas`): `RenameTrackRequest`/`RenameVideoRequest` (`title`, ≤255),
  `RenameVoiceRequest` (`name`, ≤120); все — обязательное поле, strip + `min_length=1` после trim
  (пусто → `400`). Общий strip-валидатор можно вынести в helper.
- `VideoPipeline._finalize` (`services/pipelines/video.py`): в `meta` добавить
  `"title": derive_video_title(payload)`.
- `derive_video_title(payload)` (новый helper, напр. `services/video_title.py`) — §4: **explicit
  `payload["title"]` → только `strip()`, вернуть как есть (лимит 255 из схемы, БЕЗ усечения до 40);
  иначе дефолт = фиксированная метка режима (`mode` → «Avatar Video»/«Visual Clip»/«Lyrics Video»,
  fallback «Music Video»).** `_truncate`/усечение по границе слова в хелпере не заводить — источника
  переменной длины нет. Инвариант: create (`_finalize`) и rename дают одинаковый `title` для
  одинакового ввода.
- `VideoResultResponse` (`schemas/videos.py`) += `title: str | None`; заполнять из `meta.get("title")`
  в `get_video` и в PATCH-ответе.
- `library.py`: у видео-элементов `title=v.meta.get("title")`.
- `library.py` (**ЛОМАЮЩЕЕ, §7**): у видео-элементов `id=str((v.meta or {})["job_id"])` **вместо**
  `id=str(v.id)`. Так `LibraryItem.id` для видео = `job_id`, принимаемый `GET/PATCH/DELETE /v1/videos/{job_id}`.
  Ветки tracks/voices не менять (`id` = `Track.id` / `VoiceProfile.id` уже корректны). `Asset.id` для видео
  наружу не отдавать. `job_id` в `meta` гарантирован пайплайном; на всякий случай отсутствие ключа трактовать
  как «ассет неадресуем» (в норме не встречается). После правки — перегенерировать `docs/openapi.json`.
- Ошибки: переиспользуем существующие `TRACK_NOT_FOUND`/`VOICE_PROFILE_NOT_FOUND`/`VIDEO_NOT_FOUND`
  (новых кодов не вводим).

## Последствия

Плюсы:
- Закрывает требование App Store (Rename), симметрично Delete (ADR-011).
- Заодно закрывает пробел «`title` видео теряется» — `POST /v1/videos` с `title` теперь сохраняет его.
- Минимальная поверхность: 3 PATCH-эндпоинта + 3 request-схемы + `meta.title` + аддитивные поля
  ответа. **Без миграции.**
- Единый контракт: `200` с объектом, `400` на пусто, `404` на чужой/удалённый, идемпотентно.

Минусы / долг:
- `title` видео в `meta` (JSONB) не индексируется — приемлемо (по нему не фильтруем/не сортируем).
- Rename бампает `updated_at`, но ответы отдают только `created_at` — клиент не увидит «последнее
  изменение» (см. Q-REN-1).

## Альтернативы

- **`204 No Content` для PATCH.** Отвергнут: клиенту нужен обновлённый объект (иначе доп. `GET`);
  прецедент проекта — `PATCH /v1/lyrics` → `200` с объектом.
- **Колонка `Asset.title` + миграция 0016.** Отвергнут: требует миграции/backfill без выигрыша
  (по `title` не фильтруем/не джойним); `meta` уже несёт презентационные поля видео.
- **Пустая строка = очистка в `null`.** Отвергнут: «rename» = задать имя, не очистить; неоднозначно
  `""` vs `null`; ломает инвариант «непустое имя» (ADR-008). Единое правило — reject `400`.
- **Дефолтный `title` видео = `null`.** Отвергнут: медиатека показывала бы «Untitled» у видео,
  расходясь с треками (ADR-008). Непустая метка режима дешевле и единообразнее.
- **Общий `PATCH`-эндпоинт с `type`+`id`.** Отвергнут: разные ключи (`title`/`name`), разные
  owner-check и целевые объекты (колонка vs JSONB); три явных эндпоинта прозрачнее и симметричны
  трём `DELETE`.

## Открытые вопросы

- **Q-REN-1** (low): Выставлять ли `updatedAt` в `TrackResponse`/`VoiceProfileResponse`/
  `VideoResultResponse`, чтобы клиент сортировал медиатеку по последнему изменению (rename бампает
  `updated_at`, но наружу отдаётся только `created_at`)? Сейчас — нет; аддитивно вводимо позже.

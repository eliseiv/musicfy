# ADR-011 — Удаление пользовательских ресурсов (voices / tracks / videos): soft-delete

- Статус: Accepted
- Дата: 2026-07-08
- Контекст: фидбек ревью iOS App Store — у пользователя нет возможности удалять свои голоса,
  треки и видео. В API отсутствуют DELETE-эндпоинты для пользовательских ресурсов
  (единственный DELETE — admin revoke subscription, `app/api/v1/admin.py`).
- Связанные: ADR-005 (кошелёк монет), ADR-008 (метаданные трека), ADR-009 (cover с клоном).

## Контекст

Пользователь создаёт три вида результатов, видимых в медиатеке (`GET /v1/library`):

- **Голоса** — `VoiceProfile` (`voice_profiles`), клон голоса; `provider_voice_id`, `consent_id`
  (FK → `voice_consents`, `ondelete=SET NULL`), `sample_asset_url` (строка URL, не FK).
- **Треки** — `Track` (`tracks`) + 1..N `TrackVariant` (`track_variants`, FK `ondelete=CASCADE`).
  `Track.job_id` → `jobs` (`ondelete=SET NULL`).
- **Видео** — результат = `Asset(kind=video)` c `meta.job_id`. Адресуется по `job_id`
  (`GET /v1/videos/{job_id}` находит asset по `meta.job_id`). Отдельной таблицы videos нет.

Ключевые наблюдения по коду (влияют на решение):

1. **Cover снапшотит голос, а не ссылается FK.** `pipelines/cover.py` кладёт в `job.input_payload`
   строки `target_voice` (= `provider_voice_id`) и `_target_voice_sample_url`. FK на
   `voice_profiles` из `jobs` нет. → Удаление профиля голоса **не рвёт** прошлые cover-задачи:
   у них остаётся снапшот.
2. **Видео снапшотит аудио трека.** `videos._resolve_track_audio` резолвит `audio_url`/лирику
   в `job.input_payload` в момент создания. → Удаление трека **не ломает** уже созданные видео
   (у видео свой `audio_url` в payload + свой `Asset(kind=video)`).
3. **Внутренние пайплайны пишут результат по `job_id`.** `TracksRepository.get_by_job_id`,
   webhook/poller дописывают варианты/ассеты после создания строки. Любая фильтрация «удалённых»
   не должна ломать этот внутренний путь записи.
4. **`credit_ledger` / `usage_event` / `jobs`** ссылаются на историю генераций и списаний.
   История биллинга неизменяема (ADR-005) — её удаление недопустимо.

## Решение

### 1. Soft-delete (а не hard-delete)

Вводим nullable-колонку `deleted_at TIMESTAMPTZ` на `voice_profiles`, `tracks`, `assets`.
Удаление = `UPDATE ... SET deleted_at = now()`. Строки физически остаются.

Обоснование выбора soft над hard:

- **Целостность истории.** `tracks.job_id`, `assets.meta.job_id`, `credit_ledger`, `usage_event`
  завязаны на генерации. Soft-delete сохраняет аудит биллинга (ADR-005) и связь job↔результат,
  не осиротляя ledger.
- **Снапшот-модель уже развязала ссылки.** Прошлые covers/videos не ломаются при удалении
  источника (см. наблюдения 1–2), поэтому hard-delete не даёт выигрыша в «чистоте», но добавляет
  риск потери данных.
- **Идемпотентность и восстановимость.** Повторный DELETE отфильтровывается как «не найдено»;
  при необходимости (саппорт, отмена) запись восстановима.
- **Внутренний путь записи не трогаем.** Пайплайны продолжают работать с полными строками; фильтр
  `deleted_at IS NULL` живёт только на пользовательских read/ownership-запросах.

Hard-delete отвергнут: рвёт восстановимость и усложняет каскад (нужно явно чистить варианты/ассеты),
при этом реальные медиафайлы всё равно остаются на CDN провайдера (см. §5).

### 2. Эндпоинты

| Метод / путь | Что удаляется | Каскад |
|---|---|---|
| `DELETE /v1/voices/{id}` | `VoiceProfile.deleted_at = now()` | нет (см. ниже) |
| `DELETE /v1/tracks/{id}` | `Track.deleted_at = now()` | варианты остаются недостижимыми (доступ только через трек) |
| `DELETE /v1/videos/{id}` | video-`Asset.deleted_at = now()` (`kind=video`, `meta.job_id == {id}`) | `Job` остаётся (история/биллинг) |

`{id}` для voices/tracks — id ресурса; для videos — `job_id` (как в `GET /v1/videos/{job_id}`).

**DELETE /v1/voices/{id}:**
- Soft-delete профиля. Скрывается из `GET /v1/voices` и `GET /v1/library.voices`.
- `VoiceConsent` **не удаляется** — юридический артефакт согласия, хранится как доказательство.
- `sample_asset_url` — строковый URL, не FK; профиль скрывается целиком, отдельно не чистим.
  Если это наш загруженный `Asset(kind=voice_sample)` — он не листается в library, утечки нет;
  каскад на него не делаем (надёжного маппинга URL→asset нет).
- `provider_voice_id`: голос у провайдера **не удаляем** (нет гарантированного management API;
  см. §5). Прошлые covers со снапшотом голоса продолжают работать.

**DELETE /v1/tracks/{id}:**
- Soft-delete трека. Варианты (`track_variants`) остаются в БД, но недостижимы (листинг вариантов
  идёт только через трек).
- Трек как источник видео: **не запрещаем и не каскадим**. Уже созданные видео не затрагиваются
  (снапшот `audio_url`). Создание нового видео из удалённого трека → `404 TRACK_NOT_FOUND`
  (резолв в `_resolve_track_audio` фильтрует `deleted_at IS NULL`).

**DELETE /v1/videos/{id}:**
- Проверка: `Job` существует, принадлежит пользователю, `job_type == video`; иначе `404`.
- Находим video-`Asset` (`kind=video`, `meta.job_id == {id}`, `deleted_at IS NULL`).
  Нет ассета (видео ещё не готово / уже удалено) → `404` (нечего удалять).
- Soft-delete ассета. `Job` не трогаем (история/биллинг/ledger). После удаления
  `GET /v1/videos/{id}` → `404` (video-asset отфильтрован).

### 3. Owner-check, статус-коды, идемпотентность

- Успех → **`204 No Content`** (тело пустое).
- Не найдено / чужой ресурс / уже удалён (`deleted_at IS NOT NULL`) → **`404`** с существующими
  кодами ошибок: `VOICE_PROFILE_NOT_FOUND`, `TRACK_NOT_FOUND`; для видео — новый код
  `VIDEO_NOT_FOUND` (`http_status=404`).
- **Расхождение кодов video GET vs DELETE — осознанное.** `GET /v1/videos/{job_id}`
  (`videos.py:103`) при отсутствии/чужом/не-video джобе отдаёт `JOB_NOT_FOUND`, а при
  наличии джобы, но неготовом ассете — `200` с `video_url=null` (адресация идёт по `job_id`,
  видео-ассет опционален). `DELETE /v1/videos/{id}` целится **именно в video-asset** («удалить
  видео»), поэтому «нет ассета» здесь — не «джоба не найдена», а «нечего удалять»: код
  `VIDEO_NOT_FOUND` точнее по семантике. Единый `JOB_NOT_FOUND` для DELETE не вводим —
  он смешал бы «нет такой генерации» и «видео уже удалено/не готово». GET и DELETE отдают
  разные коды сознательно: у них разный целевой объект (job vs video-asset).
- Owner-check единообразен с текущими GET (`get_track`, `get_video`): чужой → `404` (а не `403`),
  чтобы не раскрывать существование чужих ресурсов.
- **Идемпотентность:** повторный DELETE уже удалённого → `404` (строка отфильтрована как
  удалённая). Осознанный выбор в пользу `404`, а не «`204` всегда»: не требует различать
  «не существовало» и «было удалено», консистентно с owner-check.

### 4. Влияние на read/ownership-пути (фильтр `deleted_at IS NULL`)

Фильтр `deleted_at IS NULL` применяется **на всех пользовательских read/ownership и
resolve-путях**, которые (а) листают/отдают ресурс пользователю, (б) проверяют владение
при DELETE, или (в) резолвят пользовательский UUID-источник для создания **нового** результата.
Полный перечень:

| Путь | Файл | Роль фильтра |
|---|---|---|
| `GET /v1/library` | library | не листать удалённые треки/видео-ассеты/голоса |
| `GET /v1/voices` (`VoiceRepository.list_profiles`) | `voice.py:73` | не листать удалённые профили |
| `GET /v1/tracks/{id}` (ownership + read) | tracks | удалённый трек → `404 TRACK_NOT_FOUND` |
| `GET /v1/videos/{id}` (video-asset) | `videos.py:103` | удалённый video-asset → `video_url=null`/`404` |
| `GET /v1/jobs/{job_id}` (`track_id` в ответе) | `jobs.py:30` | **не отдавать `track_id` удалённого трека** (см. ниже) |
| `_resolve_track_audio` (источник нового видео) | `videos.py:24` | удалённый трек → `404 TRACK_NOT_FOUND` |
| `_resolve_target_voice` (источник нового cover) | `generation_service.py:47` | удалённый профиль → `422 unknown_voice` (см. ниже) |

**`GET /v1/jobs/{job_id}` — протечка идентификатора удалённого трека.**
Эндпоинт (`jobs.py:30`) вызывает `TracksRepository.get_by_job_id(job_id)` и кладёт `track.id`
в `JobStatusResponse.track_id`. После soft-delete трека `GET /v1/tracks/{id}` вернёт `404`, но
`GET /v1/jobs/{job_id}` продолжит отдавать `track_id` удалённого ресурса — протечка
идентификатора. **Требование:** в user-read контексте `jobs.py` не должен отдавать ссылку на
удалённый трек. Backend обязан вернуть `track_id = null`, если найденный трек имеет
`deleted_at IS NOT NULL` (эквивалентно: резолвить трек с фильтром `deleted_at IS NULL`).

**`get_by_job_id` обслуживает ДВА контекста — конфликт разводится явно.**
Один и тот же метод `TracksRepository.get_by_job_id` (`tracks.py:18`) используется:

1. **user-read** — `jobs.py:30` (отдаёт `track_id` наружу) → **обязан фильтровать** удалённые;
2. **internal finalize-дедуп** — `cover.py:167`, `song.py:237` (проверка «трек по job уже
   создан?» перед `create`) → **обязан НЕ фильтровать**: иначе после soft-delete финализатор
   решит, что трека нет, и создаст дубликат по тому же `job_id`.

**Указание backend:** ввести параметр `include_deleted: bool` в `get_by_job_id`
(`async def get_by_job_id(self, job_id, *, include_deleted: bool = False)`):
`WHERE Track.job_id == job_id [AND deleted_at IS NULL if not include_deleted]`.
`jobs.py:30` вызывает с дефолтом (`include_deleted=False` → фильтрует, `track_id=null` для
удалённого); `cover.py:167` и `song.py:237` вызывают с `include_deleted=True` (нефильтруемый
finalize-дедуп). Не делать фильтр глобальным по умолчанию без учёта finalize-путей.

**`_resolve_target_voice` — создание нового cover из удалённого голоса.**
Симметрично трекам (`_resolve_track_audio` уже фильтрует удалённый источник видео), путь
резолва target-voice для cover тоже обязан отсекать удалённые профили.
`generation_service._resolve_target_voice` (`generation_service.py:47`) резолвит UUID-профиль
через `VoiceRepository.get_profile` (`voice.py:56`, `session.get` **по PK, без фильтра**
`deleted_at`). После soft-delete голоса пользователь всё ещё может создать НОВЫЙ cover, передав
UUID удалённого профиля.
**Указание backend:** soft-deleted профиль при резолве трактуется как несуществующий.
Так как `session.get(VoiceProfile, id)` не фильтрует по PK, ввести в `VoiceRepository` явный
read-метод (`get_active_profile(profile_id)` → `SELECT ... WHERE id = :id AND deleted_at IS NULL`)
и использовать его в `_resolve_target_voice` **и** в owner-check `DELETE /v1/voices/{id}`.
Удалённый профиль → `422 unknown_voice` при резолве cover и `404 VOICE_PROFILE_NOT_FOUND` при
повторном DELETE — симметрично `_resolve_track_audio`/`TRACK_NOT_FOUND`.
Внутренние write-пути голоса (`update_profile` в `finalize` клон-пайплайна) продолжают
использовать нефильтруемый `session.get` — их не трогаем.

**Не фильтруется** внутренний путь записи/финализации результата: `TracksRepository.get_by_job_id`
с `include_deleted=True`, webhook/poller, `finalize` пайплайнов, `VoiceRepository.update_profile` —
они работают с полными строками (иначе сломается дозапись вариантов/ассетов и finalize-дедуп
после создания). Backend вводит фильтр через явный параметр (`include_deleted`) или отдельные
read-методы, а не глобально.

**backend_instructions (сводка обязательных изменений):**
- `TracksRepository.get_by_job_id(job_id, *, include_deleted=False)` — фильтр по умолчанию;
  `jobs.py:30` → дефолт; `cover.py:167`, `song.py:237` → `include_deleted=True`.
- `jobs.py:30` — `track_id=null`, если резолвнутый трек удалён.
- `VoiceRepository.get_active_profile(profile_id)` — новый фильтруемый метод; использовать в
  `generation_service._resolve_target_voice` и в owner-check `DELETE /v1/voices/{id}`.
- Все GET-листинги/ownership (`library`, `voices`, `tracks/{id}`, `videos/{id}`) — фильтр
  `deleted_at IS NULL`; `_resolve_track_audio` — фильтр (уже зафиксирован).

### 5. Медиа провайдера (fal CDN) не удаляем

Реальные файлы (`*.fal.media`, результаты генераций) хостятся у провайдера; управляемого API
удаления у нас нет. Soft-delete **скрывает нашу запись** о ресурсе — сам файл на CDN остаётся
доступным по прямой ссылке. Это принятое ограничение; фиксируется как известное (см. открытый
вопрос Q-DEL-2).

### 6. Монеты не возвращаются

Удаление результата **не возвращает** списанные монеты: генерация уже выполнена и оплачена
(ADR-005). Рефанда при удалении нет.

### 7. Uploads

Отдельный `DELETE /v1/uploads/{asset_id}` в этой итерации **не вводится**. Загруженные ассеты
(`audio`/`voice_sample`/`source_video`/`image`) — входные данные, снапшотятся в `job.input_payload`
и **не листаются** в library (утечки в UI нет). Каскадного удаления входных ассетов при удалении
результата тоже нет. См. Q-DEL-1.

## Миграция 0015 (design)

`revision = "0015_soft_delete"`, `down_revision = "0014_seed_preset_voice_previews"` (текущий head).
Backend создаёт `migrations/versions/0015_soft_delete.py` по этому спецу.

```
upgrade():
  op.add_column("voice_profiles", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
  op.add_column("tracks",         sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
  op.add_column("assets",         sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

  # Партиал-индексы под горячие пользовательские листинги (только «живые» строки).
  op.create_index(
    "ix_tracks_user_created_active", "tracks", ["user_id", "created_at"],
    postgresql_where=sa.text("deleted_at IS NULL"),
  )
  op.create_index(
    "ix_assets_user_kind_active", "assets", ["user_id", "kind", "created_at"],
    postgresql_where=sa.text("deleted_at IS NULL"),
  )
  op.create_index(
    "ix_voice_profiles_user_active", "voice_profiles", ["user_id"],
    postgresql_where=sa.text("deleted_at IS NULL"),
  )

downgrade():
  drop_index(ix_voice_profiles_user_active); drop_index(ix_assets_user_kind_active);
  drop_index(ix_tracks_user_created_active)
  drop_column(assets.deleted_at); drop_column(tracks.deleted_at); drop_column(voice_profiles.deleted_at)
```

Данные не переносим — все существующие строки остаются `deleted_at = NULL` (активны).

Существующие полные индексы (`user_id`/`job_id`-пути) **сохраняются** — они нужны internal
write/finalize и job-путям (`get_by_job_id`, дозапись вариантов/ассетов, поллер), которые
работают с полными строками без фильтра `deleted_at`. Новые partial-индексы
(`ix_tracks_user_created_active` и др.) добавляются **дополнительно** под горячие user-листинги
(`WHERE deleted_at IS NULL`); частичное дублирование с полными индексами осознанное — разные
классы запросов (user-read с фильтром vs internal без фильтра).

## Последствия

Плюсы:
- Закрывает требование App Store; безопасно (восстановимо, идемпотентно).
- Не рвёт FK/историю, снапшот-модель уже развязала прошлые covers/videos.
- Минимальная поверхность изменений: 3 эндпоинта + колонка + фильтр на read-путях.

Минусы / долг:
- Мягко-удалённые строки накапливаются (нужен будущий retention/purge — TD).
- Реальные медиа на CDN не удаляются (Q-DEL-2) — не соответствует «полному» удалению в строгом
  смысле приватности.
- Backend обязан аккуратно развести read-фильтр и внутренний путь записи (риск регрессии, если
  фильтр применить глобально): `get_by_job_id` обслуживает и user-read (`jobs.py`), и
  finalize-дедуп (`cover.py`/`song.py`) — глобальный фильтр породил бы дубли треков; резолв
  голоса (`_resolve_target_voice`) и трека (`_resolve_track_audio`) фильтруют удалённые источники,
  а write-пути (`update_profile`, дозапись вариантов) — нет.

## Альтернативы

- **Hard-delete + каскад.** Отвергнут: невосстановимо, требует явной чистки вариантов/ассетов и
  осиротляет/усложняет связи с job/ledger, при этом медиа на CDN всё равно остаётся.
- **Статус `deleted` в enum вместо `deleted_at`.** Для `voice_profiles` возможно (есть
  `VoiceProfileStatus`), но у `tracks`/`assets` статуса нет. Единый механизм `deleted_at` на трёх
  таблицах проще и однороднее; timestamp даёт основу для будущего retention.
- **`403` для чужого ресурса.** Отвергнут: раскрывает существование; текущие GET уже отдают `404`.

## Открытые вопросы

- **Q-DEL-1:** Нужен ли `DELETE /v1/uploads/{asset_id}` и/или каскадное удаление входных ассетов
  при удалении результата? (Сейчас — нет; входные ассеты не видны в UI.)
- **Q-DEL-2:** Требуется ли фактическое удаление медиа у провайдера / retention-purge
  soft-deleted строк для соответствия privacy-требованиям (GDPR-подобным)? Если да — отдельный ADR.

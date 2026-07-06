# ADR-006 — Каталог пресет-голосов (AI Voices) + превью профилей

- Статус: Accepted
- Дата: 2026-07-06
- Контекст: генерация каверов и клоны голоса (`app/domain/models/voice.py`,
  `app/domain/schemas/voices.py`, `app/domain/schemas/tracks.py` (PresetView),
  `app/api/v1/presets.py`, `app/api/v1/voices.py`,
  `app/domain/services/generation_service.py`, миграции — голова `0011_reseed_coin_products`)

## Context

iOS-дизайн вкладки «AI Voices» (экран Create Cover) ждёт **каталог готовых пресет-голосов**
(Aria / Max / Luna / Kai и др.: имя, жанр/стиль, пол, кнопка ▶️ превью). Текущий API этого
не даёт:

1. Эндпоинта `GET /v1/presets/voices` не существует — `app/api/v1/presets.py` отдаёт только
   `genres` / `moods` / `prompts` (`PresetKind`), таблица `prompt_presets`.
2. Поле `cover.targetVoice` в контракте кавера — **freeform-строка** без справочника: клиент
   волен прислать любое значение, backend его никак не валидирует и передаёт как есть в fal
   voice-changer (`FAL_VOICE_CHANGER_MODEL=fal-ai/elevenlabs/voice-changer`).
3. Вкладка «My Clones» (собственные голоса, `VoiceProfile`) отображается, но
   `VoiceProfileResponse` не содержит `previewUrl` / `sampleDurationSeconds`, поэтому ▶️
   для клонов нечем проигрывать — хотя URL образца уже хранится в
   `voice_profiles.sample_asset_url`.

Владелец утвердил: список пресетов и превью — на усмотрение исполнителя (стартовый набор
Aria / Max / Luna / Kai + ещё несколько); интеграция iOS не завершена — контракт `targetVoice`
можно ужесточать.

Ключевое ограничение: реальные `voice`-идентификаторы модели ElevenLabs voice-changer —
внутренняя провайдерская деталь. Их нельзя раскрывать клиенту (риск обхода нашего биллинга и
модерации, привязка контракта к провайдеру). Наружу клиент должен оперировать нашим стабильным
**ключом пресета** (`key`), а не провайдерским значением.

## Decision

Вводим **справочник пресет-голосов** по образцу `prompt_presets`
(model → migration create+seed → repository → CamelModel view → endpoint) плюс серверный резолв
`targetVoice`.

### 1. Таблица `preset_voices`

Новая ORM-модель `PresetVoice` (`app/domain/models/preset_voice.py`), таблица `preset_voices`:

| Колонка | Тип | Ограничения | Наружу |
|---|---|---|---|
| `id` | uuid | PK, `gen_random_uuid()` | нет |
| `key` | String(64) | **unique** (`uq_preset_voices_key`) | да |
| `title` | String(128) | not null | да |
| `subtitle` | String(255) | null | да |
| `provider_voice` | String(255) | not null | **НЕТ (внутреннее)** |
| `preview_url` | String(1024) | null | да → `previewUrl` |
| `sample_duration_seconds` | Integer | null | да → `sampleDurationSeconds` |
| `gender` | String(16) | null | да |
| `style` | String(64) | null | да |
| `language` | String(16) | null | да |
| `sort_order` | Integer | default 0 | нет (порядок) |
| `active` | Boolean | default true | нет (фильтр) |
| `meta` | JSONB | null | нет |
| `created_at` / `updated_at` | timestamptz | TimestampMixin | нет |

Индексы: `uq_preset_voices_key` (unique по `key`), `ix_preset_voices_active_sort`
(`active`, `sort_order`). Новых enum нет — `gender` / `style` / `language` хранятся строками
(во избежание миграций enum при расширении каталога). `PresetVoice` регистрируется в
`app/domain/models/__init__.py` для автогенерации alembic.

### 2. Эндпоинт `GET /v1/presets/voices`

`app/api/v1/presets.py` → новый роут `GET /voices` (префикс `/presets` уже смонтирован),
итоговый путь `/v1/presets/voices`. Возвращает `list[PresetVoiceView]` — только активные,
отсортированные `sort_order, title`. `PresetVoiceView(CamelModel)` в
`app/domain/schemas/presets.py` содержит **исключительно публичные поля**:
`key, title, subtitle, previewUrl, sampleDurationSeconds, gender, style` (плюс `language`
допустимо). **`provider_voice` в схему не входит** — гарантия невыдачи провайдерского id.
Repository `PresetVoicesRepository` (`app/domain/repositories/preset_voices.py`):
`list_active()` и `get_by_key(key)` (для резолва).

### 3. Резолв и валидация `cover.targetVoice`

В `generation_service.create_job` для `JobType.cover` значение `target_voice` валидно, если
оно попадает ровно в один из случаев:

1. **пустое / отсутствует** — кавер без смены голоса (как сейчас);
2. **UUID собственного `voice_profiles.id`** пользователя в статусе `ready` (клон «My Clones»);
3. **`key` активного `preset_voices`** — пресет из каталога.

Иначе → `ValidationFailed(reason="unknown_voice")` (HTTP 422).

**Критичный инвариант резолва.** При совпадении с ключом пресета backend **переписывает**
`payload["target_voice"]` на резолвнутое `provider_voice` **до сохранения job** (и до submit в
fal). Пайплайн и провайдер получают провайдерское значение, а не публичный `key`. Для случая
собственного клона в payload остаётся его `provider_voice_id` (существующее поведение). Таким
образом наружу ходит `key`, внутрь fal — `provider_voice`; клиент никогда не видит и не задаёт
провайдерский id.

### 4. Превью-сэмплы каталога (▶️)

`preview_url` / `sample_duration_seconds` для пресетов заполняются **оффлайн, один раз**, а не
в request-флоу. Скрипт (вне API) берёт один эталонный вокал-клип, прогоняет его через
voice-changer по каждому `provider_voice` (существующие `upload_to_storage` +
`submit_voice_changer` + `probe_duration_seconds`), перезаливает результат в fal storage и
пишет пары `(key → preview_url, sample_duration_seconds)` в **отдельную бэкфилл-миграцию**
(`UPDATE preset_voices ...`). Альтернатива — вручную подготовленные curated-mp3, URL которых
так же вписываются в эту миграцию. URL хранится строкой и отдаётся напрямую (как
`TrackVariant.audio_url`). Эндпоинт терпит `NULL` в `preview_url` (до бэкфилла ▶️ просто
неактивна) — сид каталога (`0012`) и генерация превью разнесены и не блокируют друг друга.

> **Факт на дату ревизии:** бэкфилл-миграция ещё **не создана** и отложена. Изначально за ней
> резервировался номер `0013`, но этот слот занят Feature B (`0013_video_stages`, ADR-007) —
> голова цепочки миграций сейчас `0013_video_stages`. Бэкфилл выполняется **следующим
> свободным номером** (напр. `0014_seed_preset_voice_previews`). Пока не создан → все пресет-
> голоса живут с `preview_url = NULL`, ▶️ на вкладке AI Voices неактивна. Отложенная работа
> зафиксирована как [TD-006](../100-known-tech-debt.md#td-006).

### 5. Превью в профиле клона (My Clones)

`VoiceProfileResponse` расширяется полями `previewUrl` (из уже существующего
`voice_profiles.sample_asset_url`) и `sampleDurationSeconds`. Длительность хранится в новой
колонке `voice_profiles.sample_duration_seconds` (Integer, null) — добавляется в миграции
`0012`, замеряется best-effort через `probe_duration_seconds` в финале пайплайна клонирования
(null допустим). Поля заполняются во всех местах сборки ответа (create + list).

### 6. Миграции (линейная голова после `0011`)

- **`0012_preset_voices`** (`down_revision="0011_reseed_coin_products"`): `create_table`
  `preset_voices` + seed стартового каталога (`preview_url` / `sample_duration_seconds` = NULL)
  + `op.add_column("voice_profiles", sample_duration_seconds)`. **Реализована.**
- **Бэкфилл превью-сэмплов** — **отдельная будущая миграция** следующим свободным номером (напр.
  `0014_seed_preset_voice_previews`, т.к. слот `0013` занят `0013_video_stages` из ADR-007):
  `UPDATE preset_voices SET preview_url / sample_duration_seconds`. **Ещё не создана, отложена**
  → [TD-006](../100-known-tech-debt.md#td-006).

Стартовый seed (`language="en"`, `sort_order` по порядку, `preview_url` / `sample_duration`
= NULL на шаге `0012`): Aria (pop, female), Max (rnb, male), Luna (indie, female),
Kai (hip_hop, male), Nova (electronic, female), Leo (rock, male), Sage (acoustic, female),
Rex (cinematic, male).

## Consequences

- (+) iOS получает готовый каталог AI Voices с превью и ▶️ для клонов — без хардкода на клиенте.
- (+) `targetVoice` из freeform превращается в валидируемый контракт: неизвестное значение → 422
  `unknown_voice` вместо тихого проброса мусора в fal.
- (+) Провайдерские voice-id инкапсулированы в `provider_voice`; смена провайдера/id — это
  data-миграция без слома клиента (наружу стабильный `key`).
- (+) Расширение каталога — строка в `preset_voices` + запись превью, без изменения кода и без
  новых enum (gender/style — строки).
- (−) **Ломающее изменение контракта `cover.targetVoice`:** ранее принималось любое значение,
  теперь — только пустое / UUID своего `ready`-клона / активный `key` пресета. Клиенты,
  славшие произвольные строки, получат 422. Допустимо: интеграция iOS не завершена.
- (−) Двухшаговый сид (каталог `0012` → отдельная бэкфилл-миграция превью): между шагами ▶️
  пресетов недоступна. Осознанно — сид не блокируется генерацией превью. Бэкфилл-миграция пока
  **не создана** → [TD-006](../100-known-tech-debt.md#td-006).
- (−) `VoiceProfileResponse` расширяется (аддитивно, не ломающе) — старые клиенты игнорируют
  новые поля.
- (−/[RISK-A1]) Реальные `provider_voice` (voice-id ElevenLabs voice-changer) на шаге сида —
  best-guess; backend обязан сверить по fal-доке до доверия seed. Ошибочный id правится
  data-миграцией (`provider_voice` внутренний, контракт клиента не меняется).

## Alternatives

- **Отдавать `provider_voice` клиенту напрямую (без резолва).** Отклонено: раскрывает
  провайдерскую деталь, привязывает контракт iOS к id ElevenLabs, позволяет обойти наш слой
  (модерация/биллинг), ломает клиента при смене провайдера.
- **Захардкодить каталог в коде (`dict`/enum).** Отклонено: расширение/правка превью требуют
  передеплоя; эталон проекта (`prompt_presets`, `generation_prices`) — справочник в БД как
  данные, доступные data-миграции/admin.
- **Хранить пол/стиль как enum.** Отклонено: любое новое значение потребует
  `ALTER TYPE ... ADD VALUE`; строки дешевле для справочника, значения дискретны на уровне сида.
- **Генерировать превью на лету при первом запросе.** Отклонено: дорого (реальный вызов fal в
  request-флоу), недетерминировано, усложняет кеш. Оффлайн-бэкфилл отдельной миграцией проще и
  стабильнее.
- **Единая колонка для превью-URL клона без длительности.** Отклонено: iOS ▶️ показывает
  длительность; `sampleDurationSeconds` замеряется тем же `probe_duration_seconds`, что и медиа.

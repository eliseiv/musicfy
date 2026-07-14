# ADR-016 — Фикс параметров видео-генерации: style→prompt, generate_audio, resolution/размер

- Статус: Accepted
- Дата: 2026-07-14
- Дополняет / уточняет: [ADR-007](./ADR-007-video-three-modes.md) (режимы video, seedance-модели,
  `video_mux`). ADR-007 остаётся в силе; ADR-016 меняет только контракт fal-submit видео-моделей,
  сборку промпта и параметры ffmpeg-re-encode. Инварианты ADR-007 (§3a async, provider_model==модель,
  reserve/capture/release, мукс трека) — **не затрагиваются**.
- Контекст кода: `app/domain/services/pipelines/video.py` (`_start_visual`, `_start_lyrics`,
  `_lyrics_bg_prompt`, `_resolve_prompt`), `app/domain/providers/fal/{base,client,stub}.py`
  (`submit_text_to_video`, `submit_image_to_video`, `submit_lyrics_background`),
  `app/domain/services/video_mux.py` (`_ffmpeg_mux_loop`, `_ffmpeg_render_lyrics`), `app/config.py`.

## Context

Баг-репорт (iOS + тестер, подтверждён диагностикой кода и **реальной** входной схемой
fal `bytedance/seedance-2.0/text-to-video`, сверенной на `fal.ai/models/.../api`):

1. `style` (realistic/cartoon/anime/cinematic) и `aspectRatio` из `POST /v1/videos` «на выходе
   ничего не меняют».
2. Готовый клип весит ~0.5 ГБ — неприемлемо для сохранения в галерею iOS и шаринга в Telegram.

**Реальная схема seedance-2.0 text-to-video** (source of truth для допущений и фикстур,
SHARED-правило v2):

| Поле | Тип | Дефолт | Примечание |
|---|---|---|---|
| `prompt` | str | — | required |
| `resolution` | enum {480p,720p,1080p,4k} | **720p** | |
| `duration` | enum {auto,4..15} | **auto** | секунды |
| `aspect_ratio` | enum {auto,21:9,16:9,4:3,1:1,3:4,9:16} | **auto** | |
| `generate_audio` | bool | **true** | seedance генерит СВОЮ аудиодорожку |
| `bitrate_mode` | enum {standard,high} | standard | |

**Диагноз причин:**

- **style игнорируется:** seedance **не имеет поля `style`**. Код (`_start_visual` →
  `submit_text_to_video`/`submit_image_to_video`) стиль не передаёт и не кладёт в промпт. Частичный
  путь есть только у lyrics-фона (`_lyrics_bg_prompt` добавляет `, {style} style`, и то лишь когда
  явный `prompt` пуст). Итог: для visual_clip стиль не влияет ни на что.
- **generate_audio=true (дефолт seedance):** модель отдаёт клип со **своей** аудиодорожкой, а
  пайплайн затем муксит поверх трек пользователя (`_mux_audio`) → двойное/лишнее аудио + вес.
- **размер ~0.5 ГБ:** `resolution`/`duration` не задаются явно; re-encode в `_ffmpeg_mux_loop` /
  `_ffmpeg_render_lyrics` идёт `libx264` **без каппинга битрейта** (дефолтный CRF) и **без**
  `+faststart` → размер не детерминирован и moov-атом в конце файла (плохо для прогрессивного
  воспроизведения в галерее iOS / Telegram).
- **aspect_ratio:** в t2v-ветке передаётся корректно (значения `VideoAspect` совпадают с enum
  seedance) — **работает**. Для i2v (`referenceImageUrl`) и avatar/lipsync соотношение по природе
  диктуется исходником — aspect там не гарантируется.

## Decision

### D1. style → детерминированный суффикс промпта (t2v, i2v, lyrics-фон)

Так как поля `style` у seedance нет, стиль подмешивается в **prompt** фиксированным
server-side маппингом `VideoStyle → фраза`. Маппинг (константа в `pipelines/video.py`):

| VideoStyle | Суффикс промпта |
|---|---|
| `realistic` | `photorealistic, natural lighting, realistic textures, lifelike detail` |
| `cartoon` | `cartoon style, vibrant flat colors, bold clean outlines, playful animation` |
| `anime` | `anime style, cel-shaded, expressive characters, detailed anime scenery` |
| `cinematic` | `cinematic, film grain, dramatic lighting, shallow depth of field, color-graded` |

**Правило сборки** (хелпер `_apply_style(prompt: str, style: str | None) -> str`):
`final = f"{prompt}, {SUFFIX[style]}"` если `style` задан и валиден; иначе `prompt` без изменений.
Стиль добавляется **всегда**, в т.ч. когда `prompt` задан явно или получен из `surprise_me`
(seedance стиль иначе не увидит).

Точки применения:
- **visual_clip:** результат `_resolve_prompt` пропускается через `_apply_style` перед
  `submit_text_to_video` / `submit_image_to_video`.
- **lyrics_video:** `_lyrics_bg_prompt` переписан — вместо текущего `, {style} style` (только при
  пустом prompt) применять `_apply_style` к итоговому фон-промпту **всегда** (и при явном prompt).

**Модерация:** пользовательская часть промпта (`payload['prompt']`, `surprise_me`) модерируется как
и сейчас (`generation_service` `screen_text` в `create_job`; `surprise_me` — `screen_text` в
`_pick_surprise_prompt`). Стилевой суффикс — **фиксированная server-side константа**, не пользовательский
ввод, безопасен по построению → повторной модерации не требует. Инвариант «промпт проходит модерацию
как и сейчас» сохранён (модерируется та же пользовательская часть).

### D2. generate_audio=false для всех наших t2v/i2v-сабмитов

Мы всегда муксим аудиодорожку пользователя сами (`_mux_audio` / `render_lyrics_video`), поэтому
собственная аудиодорожка seedance **не нужна** и вредна (двойное аудио + вес). `generate_audio`
передаётся **явным** полем со значением **`false`** в `submit_text_to_video`,
`submit_image_to_video`, `submit_lyrics_background`. Это **инвариант нашего пайплайна**, не
env-настройка (муксинг трека — константа архитектуры ADR-007).

### D3. resolution=720p + детерминированный размер через re-encode

- **resolution:** передаётся явным полем; дефолт **`720p`** (env `FAL_VIDEO_RESOLUTION`).
  Обоснование: `1080p`/`4k` раздувают вес и стоимость без выигрыша на мобильном музыкальном видео;
  `480p` заметно мылит. `720p` — баланс качество/размер/стоимость, совпадает с дефолтом самой
  seedance (но фиксируем явно, чтобы не зависеть от смены дефолта провайдером).
- **duration:** опционально. Дефолт — **не слать** (seedance `auto`). Для visual_clip клип всё
  равно зацикливается под длину трека (ADR-007 §3, MAJOR-3), поэтому короткий source достаточен и
  дешевле; при желании кап задаётся env `FAL_VIDEO_MAX_DURATION` (enum-значение seedance, напр. `"5"`).
  Кап — опция контроля стоимости, не требование фикса.
- **Детерминированный размер и совместимость — в существующем re-encode** (`_ffmpeg_mux_loop`,
  `_ffmpeg_render_lyrics`): visual_clip и lyrics_video **уже** переэнкодят `libx264/aac`, поэтому
  **отдельная транскод-стадия не нужна** — параметры каппинга/совместимости вшиваются в этот же
  ffmpeg-вызов (нулевая доп. стоимость по стадиям). Обязательные параметры вывода mp4:
  - `-c:v libx264 -profile:v high -level 4.0 -pix_fmt yuv420p`
  - `-crf 26 -maxrate 2500k -bufsize 5000k` (жёсткий кап битрейта → размер детерминирован)
  - `-vf scale='min(1280,iw)':'-2'` (или эквивалент по aspect) — **не** апскейлить выше 720p-бокса
  - `-c:a aac -b:a 128k`
  - `-movflags +faststart` (moov-атом в начало — обязательно для прогрессивного воспроизведения в
    галерее iOS и Telegram)
  Точная реализация фильтра/скейла — задача backend; параметры выше обязательны.

**Целевой размер:** **< 80 МБ** на клип (типично 40–60 МБ для трека 3–4 мин). При `maxrate=2500k`:
`2.5 Мбит/с × 180 с / 8 ≈ 56 МБ`. Кап `maxrate` гарантирует цель независимо от битрейта source
(включая аномальный 0.5 ГБ).

**Avatar-режимы (lipsync/avatar-image):** сейчас отдают **сырой** выход fal без re-encode →
`+faststart`/кап размера не применяются. Bug-репорт относится к seedance (visual), поэтому в рамках
этого фикса avatar не трогаем; нормализация avatar-выхода (faststart + кап) вынесена в
[TD-012](../100-known-tech-debt.md#td-012).

### D4. aspect_ratio: t2v — как есть; i2v/avatar — best-effort, кроп отложен

- **t2v (visual_clip без референса):** работает, оставить (`aspect_ratio` из `VideoAspect`
  совпадает с enum seedance).
- **i2v (visual_clip с referenceImageUrl):** `aspect_ratio` **продолжаем** слать (схема seedance
  i2v поле принимает; harmless best-effort), но соотношение по природе диктуется картинкой —
  **не гарантируется**. Документируем как не-гарантию.
- **avatar/lipsync:** соотношение диктуется исходным видео/фото — `aspect_ratio` не применяется
  (модели kling/sync-lipsync поле не принимают, не шлём).
- **Best-effort ffmpeg кроп/пад под запрошенный aspect для i2v/avatar** — отложен в
  [TD-013](../100-known-tech-debt.md#td-013) (стоимость/сложность vs выгода). В V1 фикса —
  молча best-effort (что отдала модель), без кропа.

### D5. Контракт fal-client (только поддерживаемые поля, сверено с реальной схемой)

Расширяются три submit-метода (`base.py` Protocol + `client.py` + `stub.py`). Инвариант «шлём
только поддерживаемые моделью поля» сохранён — `resolution`/`generate_audio`/`duration`/`bitrate_mode`
подтверждены реальной схемой seedance-2.0 t2v (см. Context). `None`-поля не отправляются.

| Метод | Новые поля | Значения |
|---|---|---|
| `submit_text_to_video` | `resolution: str \| None`, `generate_audio: bool`, `duration: str \| None` | resolution=`FAL_VIDEO_RESOLUTION` (720p); generate_audio=`False`; duration — из env-капа или не слать |
| `submit_image_to_video` | те же | так же |
| `submit_lyrics_background` | те же | так же |

`aspect_ratio` уже присутствует — без изменений. `submit_lipsync_video` /
`submit_avatar_image_video` — **без изменений** (kling/sync-lipsync эти поля не принимают → 422).

Пайплайн (`_start_visual`, `_start_lyrics`) прокидывает `resolution`/`generate_audio`/`duration` из
`Settings` (генерация значений — в pipeline/config, не в client).

### D6. Инварианты (не ломать)

async job-модель (§3a), `job.provider_model` == реально вызванной модели, `reserve` в `create_job`
/ `capture` в `_finalize` / `release` в `_mark_failed`, мукс трека пользователя, per-stage
`idempotency_key`, `advance()` guard по `current_stage`, ветвление visual/lyrics по
`input_payload['mode']` — **сохраняются без изменений**.

## Consequences

- (+) style теперь реально влияет на visual_clip и lyrics-фон (детерминированный суффикс промпта);
  единый маппинг переиспользуется всеми t2v/i2v-ветками.
- (+) Устранено двойное аудио (`generate_audio=false`) — единственная дорожка = трек пользователя.
- (+) Детерминированный размер (< 80 МБ) и `+faststart` → корректное сохранение в галерею iOS и
  шаринг в Telegram. Без новой стадии — параметры вшиты в существующий re-encode.
- (+) Контракт fal-client расширен только полями, подтверждёнными реальной схемой seedance
  (SHARED-правило v2); фикстуры QA обязаны утверждать эти имена/значения полей.
- (−) style — приближение через промпт, не гарантированный «переключатель» (у seedance нет
  style-поля); визуальный эффект зависит от модели.
- (−) resolution фиксирован 720p; клиент пока не выбирает разрешение (не в scope фикса).
- (−/TD-012) avatar-выход не нормализуется (нет faststart/капа) — вынесено в tech-debt.
- (−/TD-013) точный aspect для i2v/avatar (кроп/пад) отложен — best-effort в V1.

## Alternatives

- **Отдельная транскод-стадия после upload_cdn.** Отклонено: visual_clip/lyrics **уже** переэнкодят
  в mux/lyrics-render — добавление второй стадии = лишнее скачивание+encode+upload и новый
  `JobStage`. Каппинг вшивается в существующий re-encode бесплатно.
- **Слать style как поле сабмита.** Невозможно: у seedance-2.0 t2v/i2v поля `style` нет (сверено с
  реальной схемой) → 422 на лишнем поле. Единственный путь — промпт.
- **generate_audio=true + удалять аудиодорожку в ffmpeg.** Отклонено: тратит генерацию (вес,
  стоимость) на дорожку, которую мы отбрасываем; `generate_audio=false` дешевле и проще.
- **resolution=1080p с агрессивным CRF.** Отклонено: 1080p дороже у провайдера и не нужен для
  мобильного музыкального видео; 720p + кап битрейта достигает цели по размеру дешевле.
- **Нормализовать avatar-выход в этом же фиксе.** Отклонено для V1: avatar (kling/sync-lipsync) не
  в bug-репорте; re-encode avatar = тяжёлая новая стадия (download+encode+upload в `advance()`).
  Вынесено в TD-012 до подтверждения проблемы на avatar-выходе.

# ADR-008 — Разбор demucs-стемов (верхнеуровневые ключи) + метаданные трека (промпт и автозаголовок)

- **Статус:** Accepted
- **Дата:** 2026-07-07
- **Контекст-триггер:** фидбек iOS-разработчика (2 правки backend): (1) любой cover загруженного трека падает с `PROVIDER_FAILED "no cover audio"`; (2) сгенерированные треки без явного `title` показываются как «Untitled», а промпт генерации нигде не сохранён.

Две независимые правки объединены в один ADR, т.к. отгружаются одним batch'ем и обе меняют контракт (fal-разбор / API трека).

---

## Часть A — Разбор стемов demucs

### Контекст (баг, срочно)

Реальная модель `fal-ai/demucs` возвращает стемы **верхнеуровневыми ключами** результата, без обёртки `"stems"`:

```json
{"vocals":{"url":"...vocals.mp3"},"drums":{"url":"..."},"bass":{"url":"..."},
 "other":{"url":"..."},"guitar":{"url":"..."},"piano":{"url":"..."}}
```

Единый парсер `parse_fal_webhook_event` (`app/domain/providers/fal/parsing.py:133`) извлекает стемы **только** как `result.get("stems")`. У demucs ключа `"stems"` НЕТ → `stems=None`. Далее в `cover.py:74` `vocal_url = _pick_stem(stems, ("vocals","vocal"))` = `None` → стадия `voice_conversion` помечается `skipped` (`cover.py:82-83`) → в `_finalize` нет вокала → `_mark_failed("PROVIDER_FAILED","no cover audio")` (`cover.py:136-138`). **Итог: любой cover падает.**

Баг не ловился юнит-тестами: тест-фикстура `emit_fal_completed` (`tests/helpers.py:40-41`) кладёт стемы под `result["stems"]`, а `test_cover.py:60` подаёт нереалистичные ключи `{"vocals","accompaniment"}`. Это тот же класс дефекта, что [TD-002](../100-known-tech-debt.md#td-002)/[TD-003](../100-known-tech-debt.md#td-003): стаб/фикстура отдавали формат, которого реальный fal не отдаёт.

### Решение A1 — где и как извлекать стемы

Чинить в **`parsing.py`** (единый источник истины контракта fal, оба провайдера обязаны звать один парсер — инвариант из [ARCHITECTURE.md → Контракт интеграции fal.ai](../ARCHITECTURE.md#контракт-интеграции-falai-форматы-результата)). Не в `cover.py` — это распространило бы знание про формат fal в пайплайн и разошлось бы со стабом.

Ввести helper `extract_stems(result: dict) -> dict | None` (заменяет строку `stems = result.get("stems") if isinstance(...) else None`):

```python
# Известный словарь имён стемов (demucs + другие сепараторы).
STEM_NAMES = {
    "vocals", "vocal", "drums", "bass", "other",
    "guitar", "piano", "accompaniment", "instrumental", "backing",
}

def extract_stems(result: dict) -> dict | None:
    # 1) Приоритет — явная обёртка "stems" (обратная совместимость, wrapped-модели).
    explicit = result.get("stems")
    if isinstance(explicit, dict) and explicit:
        return explicit
    # 2) demucs-путь: собрать верхнеуровневые ключи из известного словаря,
    #    у которых значение даёт url ({url:..} или строка).
    top: dict = {}
    for k, v in result.items():
        url = v.get("url") if isinstance(v, dict) else (v if isinstance(v, str) else None)
        if k in STEM_NAMES and url:
            top[k] = v          # сохраняем исходную форму — _pick_stem ест и {url:..}, и строку
    # 3) Порог >=2 защищает от коллизии одиночного stem-именованного ключа
    #    у не-сепараторных моделей; demucs всегда отдаёт 4-6 стемов.
    return top if len(top) >= 2 else None
```

**Почему это минимально-инвазивно и не ломает остальное:**

- **`extract_media` не трогается.** Для demucs-payload ни один медиа-ключ (`audio`/`video`/`output`/`result`/`audio_url`/`video_url`) не присутствует → `media_url=None` (корректно: cover не использует `media_url` со стадии `stem_separation`). media_url других моделей не затрагивается — это отдельная функция.
- **song-стемы не ломаются.** Payload песни — `{"audio":{"url":...}}`. Ключ `audio` **не** входит в `STEM_NAMES` → верхнеуровневый путь ничего не соберёт → `stems=None` (как и было). Fallback-модели song (`stable-audio`/`ace-step`) — тоже `{"audio":{...}}` → не затронуты.
- **Явная обёртка `"stems"` — приоритетна.** Любая будущая модель, кладущая `result["stems"]`, работает как раньше; существующие тесты `test_fal_webhook_parse.py` (стемы под `"stems"`) остаются зелёными.
- **Порог `>=2`** исключает ложное срабатывание на одиночном ключе-омониме (напр. гипотетический `{"other":{...}}` в не-сепараторной модели). demucs всегда ≥4 стемов.

### Решение A2 — инструментал = микс не-вокальных стемов

demucs **не отдаёт** `accompaniment`/`instrumental` — только `drums/bass/other/guitar/piano`. Текущий `_pick_stem(stems, ("accompaniment","instrumental","other","backing"))` на реальном demucs вернул бы в лучшем случае одиночный `"other"` — это **потеря drums/bass** (самые слышимые партии) → плохой инструментал.

**Решение:** инструментал = ffmpeg-микс **всех не-вокальных стемов** (`drums + bass + other + guitar + piano`), а не одиночный `other`.

- В `cover.advance` (ветка `stem_separation`): собрать список url всех стемов кроме `vocals`/`vocal` → сохранить в payload как `_instrumental_stems: list[str]` (вместо одиночного `_instrumental_stem`).
- В `_finalize`/`_mix`: если `>=1` не-вокальный стем и ffmpeg доступен → сначала свести стемы в один инструментал (`ffmpeg amix inputs=N:normalize=0`), затем существующий `mix_music_and_vocal(music_url=<инструментал>, vocal_url=<converted>)`. Нужен новый helper в `audio_mixer.py`, напр. `build_instrumental(stem_urls: list[str], upload_fn) -> tuple[str|None, float|None]` (скачать N, `amix`, upload) — переиспользует паттерн `_download`/`_ffmpeg_mix`/`upload_fn`.

**Fallback-цепочка (деградация):**
1. `>=1` не-вокальный стем + ffmpeg → инструментал из микса стемов → mix с конвертированным вокалом. **Основной путь для реального demucs.**
2. Нет не-вокальных стемов **или** ffmpeg недоступен → **отдать конвертированный вокал в одиночку** (существующий деградированный путь `_mix`, `cover.py:175-182`).
3. **УБРАТЬ** fallback `instrumental = ... or payload.get("source_audio_url")` (`cover.py:127`). Микс конвертированного вокала поверх **исходного трека** даёт **двойной вокал** (в источнике всё ещё оригинальный вокал) → аудио-брак. Лучше отдать чистый конвертированный вокал (п.2), чем источник с удвоенным вокалом.

С фиксом A1 стемы для реального demucs всегда присутствуют, поэтому п.2/п.3 — только для отказа сепарации.

### Решение A3 — фикстура/стаб отдают реальный формат demucs

Чтобы регрессия ловилась тестами, событие завершения demucs в тестах должно эмитить стемы **верхнеуровневыми ключами реального формата**:

```json
{"request_id":"...","status":"OK","error":null,
 "payload":{"vocals":{"url":"..."},"drums":{"url":"..."},"bass":{"url":"..."},"other":{"url":"..."}}}
```

- Добавить в `tests/helpers.py` вариант эмиссии (напр. параметр `top_level_stems: dict | None` в `emit_fal_completed`, кладущий стемы в `payload` верхним уровнем, а не под `result["stems"]`), либо отдельный `emit_fal_demucs_completed`.
- `test_cover.py` перевести на реальные ключи demucs (`vocals`/`drums`/`bass`/`other`), убрав `accompaniment`.
- Обёртка `emit_fal_completed` под `"stems"` остаётся для моделей с явной обёрткой (обратная совместимость) — но demucs-путь теперь покрыт реальной формой.

> **Инвариант (обязателен к соблюдению):** стаб и тест-фикстуры fal обязаны эмитить те же формы payload, что и реальный fal. Расхождение формы уже дважды приводило к «зелёным тестам / сломанному проду» ([TD-002](../100-known-tech-debt.md#td-002), [TD-003](../100-known-tech-debt.md#td-003), и теперь demucs).

### Последствия A

- (+) Cover загруженного трека снова работает на реальном demucs.
- (+) Контракт разбора стемов централизован в `parsing.py`; media_url и song-стемы не затронуты.
- (+) Инструментал полноценный (все не-вокальные партии), а не одиночный `other`.
- (−) Появляется доп. ffmpeg-шаг (свод стемов) — учтено в деградационной цепочке (без ffmpeg → чистый конвертированный вокал). Приемлемое V1-качество зафиксировано как [TD-007](../100-known-tech-debt.md#td-007).

---

## Часть B — Промпт и автозаголовок трека

### Контекст (фича)

`Track.title` берётся как `(job.input_payload or {}).get("title")` (`song.py:242`, `cover.py:156`) — пусто, если пользователь не задал → клиент показывает «Untitled». Сам промпт генерации (`input_payload["prompt"]` / результат `_compose_song_prompt`) на треке **не сохраняется**. `Track` уже имеет `title` (String255, nullable) и `meta` (JSONB) — миграция не нужна.

### Решение B1 — хранить промпт в `Track.meta["prompt"]`

Без миграции (минимализм): при создании трека в `_finalize` писать `meta["prompt"]`:
- **song:** сырой пользовательский промпт `input_payload.get("prompt")` (то, что ввёл пользователь; при пустом — `None`). Композитный `_compose_song_prompt` НЕ храним как `prompt` (это внутренняя строка для fal с genre/mood-хвостами).
- **cover:** текстового промпта нет → `meta["prompt"] = None` (cover определяется источником+голосом).

`meta` расширяется аддитивно: song — `{"runtime": ..., "prompt": <str|None>}`; cover — `{"runtime": ..., "quality_flag": ..., "prompt": None}`.

### Решение B2 — детерминированный автозаголовок

Ввести helper (напр. `app/domain/services/track_title.py`): `derive_track_title(kind, input_payload) -> str`. **Без** доп. генерации/сетевых вызовов — чисто детерминированно.

**Приоритет для song:**
1. `input_payload["title"]` (если непустой после strip).
2. Иначе из `input_payload["prompt"]` — усечение до **40 симв.** по границе слова + `…` при обрезке.
3. Иначе из `input_payload["custom_lyrics"]` (та же усечка).
4. Иначе из `input_payload["lyrics_prompt"]` (та же усечка).
5. Иначе `"New Song"` (детерминированный fallback, не «Untitled»).

**Для cover:**
1. `input_payload["title"]` если задан.
2. Иначе `"Cover"` c опциональным суффиксом из имени файла источника: `Cover • <basename(source_audio_url) без расширения, усечён до 40>` при выводимости; иначе просто `"Cover"`. (Имя пресет-голоса НЕ резолвим — избегаем доп. запроса к `preset_voices`; детерминированность важнее.)

Усечка: обрезать по последней границе слова в пределах 40 симв., добавить `…`, если строка была длиннее.

Применение: в обоих `_finalize` заменить `title=(job.input_payload or {}).get("title")` на `title=derive_track_title(kind, job.input_payload)`.

### Решение B3 — отдавать промпт/заголовок в API

- **`TrackResponse`** (`app/domain/schemas/tracks.py`): добавить поле `prompt: str | None = None` (аддитивно). `title` уже есть — теперь всегда непустой благодаря B2.
- **`GET /v1/tracks/{id}`** (`app/api/v1/tracks.py`): передавать `prompt=(track.meta or {}).get("prompt")`.
- **`GET /v1/library`** (`app/api/v1/library.py`): `LibraryItem.title` уже отдаётся и теперь непустой (автозаголовок на этапе создания). Дополнительно — аддитивно добавить `prompt: str | None = None` в `LibraryItem` и заполнять его для треков (`prompt=(t.meta or {}).get("prompt")`); для video/voice остаётся `None`. Это опционально для клиента, но даёт паритет с `TrackResponse`.
- **`TrackSummary`** — `title` уже есть, автозаголовок покрывает; менять не требуется.

### Последствия B

- (+) Клиент больше не показывает «Untitled» — всегда есть детерминированный заголовок.
- (+) Промпт генерации доступен клиенту (`TrackResponse.prompt`, `LibraryItem.prompt`).
- (+) Без миграции БД (используется существующий `Track.meta` JSONB).
- (−) Автозаголовок из промпта — эвристика (усечение), не «умное» название. Достаточно для V1; при желании — отдельная короткая генерация в будущем (не в scope).
- Существующие треки без `meta["prompt"]` → `prompt=None`, `title` уже сохранён в колонке (для старых — как был). Обратная совместимость сохранена.

---

## Альтернативы (отклонены)

- **A: чинить demucs-разбор в `cover.py`** — разносит знание формата fal вне единого парсера, расходится со стабом (тот же класс, что TD-002/TD-003). Отклонено.
- **A: инструментал = одиночный `other`** — теряет drums/bass, аудио-брак. Отклонено.
- **A: инструментал = микс поверх исходного трека** — двойной вокал. Отклонено.
- **B: новое поле-колонка `Track.prompt`** — требует миграции ради строки, которую `meta` уже вмещает. Отклонено в пользу `meta["prompt"]`.
- **B: генерировать заголовок отдельной LLM/fal-моделью** — стоимость, недетерминизм, доп. задержка. Отклонено для V1 в пользу усечения промпта.

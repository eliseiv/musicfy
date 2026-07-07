# ADR-009 — Cover с клонированным голосом: совместимый провайдер конвертации

- Статус: Accepted
- Дата: 2026-07-07
- Контекст: генерация каверов и клоны голоса
  (`app/domain/services/pipelines/cover.py`, `app/domain/services/pipelines/voice_clone.py`,
  `app/domain/services/generation_service.py` (`_resolve_target_voice`),
  `app/domain/models/voice.py`, `app/domain/providers/fal/{base,client,stub}.py`,
  `app/config.py`; связано с [ADR-006](./ADR-006-preset-voices-catalog.md))

## Context

На проде подтверждён баг: **cover с собственным клонированным голосом падает**
`voice_conversion: failed, provider 422`. Пресет-голоса работают, клоны — нет.

Причинно-следственная цепочка:

1. **Клонирование** голоса идёт через `FAL_VOICE_CLONE_MODEL = "fal-ai/minimax/voice-clone"`
   (`voice_clone.py:58` → `custom_voice_id`). Результат сохраняется как
   `VoiceProfile.provider_voice_id` (формат minimax, напр. реальное значение прод
   `"Voicea84eeb811783404139"`).
2. **Конвертация** голоса в cover идёт через `FAL_VOICE_CHANGER_MODEL =
   "fal-ai/elevenlabs/voice-changer"` (`cover.py:_submit_voice_conversion` →
   `submit_voice_changer(voice=target_voice)`).
3. Резолв `cover.targetVoice` для собственного `ready`-клона переписывает
   `payload["target_voice"]` на `profile.provider_voice_id` — **minimax-id**
   ([ADR-006](./ADR-006-preset-voices-catalog.md), `generation_service.py:86`).
4. minimax-id уходит в поле `voice` ElevenLabs voice-changer. ElevenLabs не знает этот
   идентификатор → **fal 422**. Пресеты работают, т.к. их `provider_voice` — реальные имена
   голосов ElevenLabs (Aria/Brian/…).

**Корневая несовместимость — не только формат id, а сам класс модели.**
`minimax/voice-clone` возвращает `custom_voice_id`, пригодный **только для minimax TTS**
(text-to-speech, генерация речи из текста). Он **не** предназначен для audio-to-audio конверсии
уже спетого вокала. А cover — это именно **audio-to-audio voice conversion** вокального стема.
Поэтому «просто исправить id» нельзя: клон-провайдер (minimax TTS-clone) и cover-конвертер
(ElevenLabs audio-to-audio) относятся к разным классам задач.

**Исследование возможностей fal.ai** (web, 2026-07):

| Модель fal | Класс | `voice`/target | Пригодность для cover-клона |
|---|---|---|---|
| `fal-ai/elevenlabs/voice-changer` | audio→audio | **имя/id голоса из аккаунта fal-ElevenLabs** (поля: `audio_url`, `voice`, `remove_background_noise`, `seed`, `output_format`) | Только пресеты. Кастомный голос добавить нельзя |
| `fal-ai/minimax/voice-clone` | text→speech clone | `custom_voice_id` (для minimax TTS) | Нет: не конвертирует существующий вокал |
| `fal-ai/qwen-3-tts/clone-voice`, `fal-ai/f5-tts`, `fal-ai/dia-tts/voice-clone` | text→speech (zero-shot) | reference audio → эмбеддинг для TTS | Нет: генерируют речь из текста, не конвертируют вокал |
| **`fal-ai/chatterbox/speech-to-speech`** | **audio→audio** | **`target_voice_audio_url` — референс-аудио (zero-shot, без pre-registered id)** (поля: `source_audio_url` required, `target_voice_audio_url` optional, `temperature`, `seed`) | **Да: принимает произвольный голос как аудио-референс** |

Ключевые факты, определившие решение:

- fal **не хостит** ElevenLabs IVC / create-voice endpoint — на fal есть только TTS, voice-changer,
  music, dubbing, speech-to-text. Значит нельзя завести пользовательский голос в аккаунте
  fal-ElevenLabs и получить совместимый с voice-changer `voice_id`. **Вариант «единый провайдер
  ElevenLabs» на fal нереализуем.**
- ElevenLabs voice-changer **не принимает** референс-аудио — только имя/id голоса из своего
  аккаунта. Кастомный клон туда не подать.
- **`chatterbox/speech-to-speech`** — единственная на fal audio-to-audio модель, принимающая
  **произвольный целевой голос как аудио-референс** (`target_voice_audio_url`), zero-shot, без
  предварительной регистрации id. У нас уже есть нужный референс — `VoiceProfile.sample_asset_url`
  (образец голоса, сохраняется при `create_profile`, `voice.py` repo).

## Decision

**Вариант B — ветвление cover-конвертации по источнику голоса**, клон-ветка через
`fal-ai/chatterbox/speech-to-speech` с образцом голоса как аудио-референсом. Пресет-ветка — без
изменений (ElevenLabs voice-changer).

### 1. Две ветки стадии `voice_conversion`

| Источник targetVoice | Модель | Целевой голос |
|---|---|---|
| **пресет** (`preset_voices.key`) или пусто | `FAL_VOICE_CHANGER_MODEL` (ElevenLabs, как сейчас) | `voice` = `preset.provider_voice` (имя ElevenLabs); пусто → дефолт |
| **собственный `ready`-клон** (UUID профиля) | `FAL_VOICE_CONVERSION_MODEL` (**новый env** = `fal-ai/chatterbox/speech-to-speech`) | `target_voice_audio_url` = `VoiceProfile.sample_asset_url` |

Клон-ветка **не использует** `provider_voice_id` (minimax-id) вообще — cover опирается на
**образец голоса** (`sample_asset_url`), а не на minimax-clone-id. Тем самым устраняется
кросс-провайдерская несовместимость: голос подаётся как аудио, а не как чужой id.

### 2. Дискриминатор — источник голоса, переносится в payload (без изменения схемы БД)

`_resolve_target_voice` (`generation_service.py`) уже различает три случая (пусто / UUID
своего `ready`-профиля / `key` пресета). Меняется то, что он кладёт в `job.input_payload`
(внутренние `_`-префиксные ключи, как `_vocal_stem` / `_fal_status_url`):

- **пусто** → `_voice_kind = "preset"` (дефолтная ветка), `target_voice` не задан.
- **пресет** → `_voice_kind = "preset"`, `target_voice = preset.provider_voice` (как сейчас).
- **свой `ready`-клон** → `_voice_kind = "clone"`,
  `_target_voice_sample_url = profile.sample_asset_url`. **Defensive:** если у `ready`-профиля
  `sample_asset_url` пуст → `422 { reason: "unknown_voice" }` (аналогично текущей проверке
  `provider_voice_id`). `target_voice` для клон-ветки в fal **не** уходит.

`cover.py:_submit_voice_conversion` читает `_voice_kind`:
- `"clone"` → `submit_speech_to_speech(source_audio_url=vocal_url,
  target_voice_audio_url=payload["_target_voice_sample_url"], …)`;
- иначе → `submit_voice_changer(...)` (как сейчас).

Внешний контракт `POST /v1/covers` **не меняется**: клиент по-прежнему шлёт `targetVoice` = `key`
пресета **или** UUID своего клона (ADR-006). Изменение — только во внутреннем резолве и пайплайне.

### 3. Схема `VoiceProfile` НЕ меняется; колонка `provider`/`clone_model` НЕ вводится

Осознанное решение (принцип простоты). Дискриминатор ветки — **источник голоса** (UUID своего
профиля vs `key` пресета), он уже вычисляется в `_resolve_target_voice`; провайдер клона хранить
не нужно, потому что cover больше не зависит от клон-провайдера — он использует **аудио-образец**.
Добавление колонки `provider` понадобилось бы только при пер-клон роутинге на разные
voice-changer'ы; мы этого избегаем.

### 4. Провайдер fal — новый метод

`submit_speech_to_speech(*, source_audio_url, target_voice_audio_url, webhook_url,
idempotency_key)` в ABC (`base.py`), реализация (`client.py`, модель из
`FAL_VOICE_CONVERSION_MODEL`, payload `{"source_audio_url", "target_voice_audio_url"}`), стаб
(`stub.py`). **Инвариант стаба:** эмитить ту же форму результата, что реальный fal (`{"audio":
{"url": …}}`), — расхождение формы уже дважды давало «зелёные тесты / сломанный прод»
([TD-002](../100-known-tech-debt.md#td-002), [TD-003](../100-known-tech-debt.md#td-003)).

### 5. Poller/webhook — без изменений

Cover-стадия сохраняет `submit.status_url`/`response_url` в payload
(`_fal_status_url`/`_fal_response_url`), poller опрашивает их напрямую (`base.py:104-112`),
`job.provider_model` для cover-конвертации не используется. Поэтому смена модели на chatterbox
polling не ломает. Идемпотентность (`{job.id}:vc`), 2-фазный webhook, подпись — как есть.

### 6. Существующие клоны — миграция НЕ нужна

`sample_asset_url` сохраняется у **каждого** профиля при `create_profile` (request-time,
`api/v1/voices.py:56-58`). Значит все существующие `ready`-клоны сразу работают в cover через
chatterbox — их (ныне неиспользуемый для cover) minimax `provider_voice_id` просто игнорируется.
**Переклон/сброс/бэкфилл не требуются.**

## Consequences

**Плюсы**

- Cover с собственным клоном работает: голос подаётся аудио-референсом, кросс-провайдерского
  422 больше нет.
- Zero-migration: существующие клоны совместимы немедленно (используем уже сохранённый образец).
- Минимум изменений схемы: без новых колонок; дискриминатор — уже вычисляемый источник голоса.
- Пресет-ветка и её контракт (ADR-006) не затронуты.

**Минусы / риски**

- **Качество пения chatterbox** для cover не подтверждено (модель ориентирована на речь;
  singing-conversion в доке не специфицирован) — см. **Q-COVER-1** (владельцу/продукту).
  Это единственный fal-путь для клон-cover — альтернативы (ElevenLabs с кастомным голосом на fal)
  не существует.
- `minimax/voice-clone` в `voice_clone.py` становится **вестигиальным** для cover: его
  `custom_voice_id` больше нигде не используется, а «готовность» клона по-прежнему гейтится
  успехом minimax-вызова (сбой minimax → профиль `failed`, хотя для cover достаточно образца).
  См. **TD-008** (осознанный долг, вне scope этого фикса).
- Стоимость: cover-клон теперь оплачивает вызов chatterbox (наш fal-cost) вместо ElevenLabs
  voice-changer; для пользователя цена cover неизменна (5 монет, ADR-005).

## Alternatives (отклонены)

- **A. Единый провайдер ElevenLabs (клон через ElevenLabs IVC).** На fal **нет** endpoint
  создания IVC-голоса; voice-changer работает с голосами аккаунта fal-ElevenLabs, кастомный
  добавить нельзя. Прямая интеграция с ElevenLabs API мимо fal (свой ключ/аккаунт, cloning +
  changer) — крупная новая провайдерская интеграция и биллинг; несоразмерно фиксу. Отклонено.
- **B-alt. minimax-совместимый путь конвертации.** У minimax на fal нет audio-to-audio
  voice-conversion, принимающего произвольное аудио + minimax `custom_voice_id`; voice-clone
  применяется только через minimax **TTS** (из текста). Конвертировать спетый вокал в minimax-клон
  на fal невозможно. Отклонено.
- **C. Другая единая voice-changer модель для обоих.** Нет модели, принимающей И имя-пресета,
  И произвольный кастомный голос. chatterbox мог бы обслуживать и пресеты (через образцы пресетов),
  но это ломает ADR-006 (пресеты завязаны на ElevenLabs `provider_voice`) и ухудшает проверенное
  качество пресетов — отклонено в пользу ветвления.
- **Колонка `VoiceProfile.provider`.** Не нужна (см. Decision §3). Отклонено как усложнение.

## Указания для backend

1. `config.py`: добавить `FAL_VOICE_CONVERSION_MODEL: str = "fal-ai/chatterbox/speech-to-speech"`;
   пробросить в конструктор `FalAiProvider` (рядом с `voice_changer_model`).
2. `providers/fal/base.py`: объявить `submit_speech_to_speech(*, source_audio_url,
   target_voice_audio_url, webhook_url, idempotency_key) -> FalSubmitResult` в ABC.
3. `providers/fal/client.py`: реализовать через `self._submit(model=self._voice_conversion_model,
   payload={"source_audio_url": …, "target_voice_audio_url": …}, …)`.
4. `providers/fal/stub.py`: реализовать, **эмитить `{"audio": {"url": …}}`** (та же форма, что
   voice-changer стаб).
5. `generation_service._resolve_target_voice`: вместо перезаписи `target_voice` на minimax-id для
   клона — выставлять `_voice_kind="clone"` и `_target_voice_sample_url=profile.sample_asset_url`;
   пресет/пусто → `_voice_kind="preset"` (+ `provider_voice` как сейчас). Пустой
   `sample_asset_url` у ready-клона → `422 unknown_voice`.
6. `cover.py:_submit_voice_conversion`: ветвить по `_voice_kind` (`clone` → chatterbox,
   иначе → ElevenLabs voice-changer). Idempotency-key, запись стадий, обработка ошибок —
   как в текущей ветке.
7. `voice_clone.py` — **не трогать** в рамках фикса (minimax-clone остаётся; вопрос его удаления —
   TD-008).

## Открытые вопросы

- **Q-COVER-1** (владельцу/продукту): приемлемо ли качество `chatterbox/speech-to-speech` для
  **пения** в cover? Это единственный fal-путь для клон-cover; при неприемлемом качестве —
  продуктовое решение (отключить cover для клонов / искать нового провайдера / прямая ElevenLabs
  IVC-интеграция мимо fal). Рекомендация: пометить клон-cover результат `quality_flag` и провести
  продуктовую валидацию на реальных образцах.

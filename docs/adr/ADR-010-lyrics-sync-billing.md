# ADR-010 — Биллинг синхронной генерации lyrics: атомарный charge + refund

- Статус: Accepted
- Дата: 2026-07-08
- Контекст: биллинг musicfy — синхронный эндпоинт `POST /v1/lyrics`
  (`app/api/v1/lyrics.py` → `LyricsService.generate`,
  `app/domain/services/lyrics_service.py`), единый кошелёк монет
  (`CoinWalletService`, `app/domain/services/credits.py`), прайс-лист
  `generation_prices` (`PricingRepository.get_active_price`), аудит `credit_ledger`.
  Дополняет [ADR-005](./ADR-005-coin-wallet-billing.md) (единый кошелёк монет).

## Context

По [ADR-005](./ADR-005-coin-wallet-billing.md) списание монет реализовано схемой
**reserve → capture / release** и подключено ТОЛЬКО в `GenerationService.create_job`
(`generation_service.py:178-227`): `create_job` резервирует цену
(`_credits.reserve(price_of(job_type))`), создаёт `Job`, а `capture`/`release`
доводят расчёт по завершении **асинхронной** fal-задачи (webhook/`FalPoller` →
`advance()`). Так корректно биллятся `song` / `cover` / `video`.

`POST /v1/lyrics` устроен иначе: он **синхронный** и **не проходит через `create_job`**.
`LyricsService.generate` напрямую вызывает fal (`self._fal.generate_lyrics(...)`) и
создаёт `LyricsDraft` — **без `Job`** и без единой строчки биллинга. Изначально
`lyrics` был бесплатным типом (ADR-005 §2: «`lyrics` и `voice_clone` — бесплатны,
нет строки в прайс-листе → цена 0»).

Владелец через admin-эндпоинт (`PATCH`, `PricingRepository.upsert_price`) завёл строку
`generation_prices(job_type="lyrics", price_coins=10, active=true)`. ADR-005 §2 явно
разрешает вводить новый платный тип строкой в прайс-листе без нового ADR. Но цена
**не списывается**: путь `/v1/lyrics` не вызывает кошелёк вовсе. Итог — цена в таблице
есть, гость с балансом 0 получает текст (200). Нужен механизм списания на **синхронном**
пути.

Ключевое препятствие переиспользования существующего API: `CoinWalletService.capture`/
`release` жёстко завязаны на объект `Job` (читают `job.id`, `job.user_id`,
`job.reserved_credits`; идемпотентность по `capture:{job.id}` / `release:{job.id}`).
У lyrics `Job` нет. `reserve(user_id, job_type)` — уже Job-free.

Смежное наблюдение: `voice_clone` тоже бесплатен, но он **асинхронный `Job`** через
`create_job` (пайплайн `pipelines/voice_clone.py`, `_provider_model` → `FAL_VOICE_CLONE_MODEL`),
поэтому при появлении цены он биллится штатным reserve→capture без изменений. Другого
**синхронного** оплачиваемого пути в системе нет. Редактирование (`PATCH /v1/lyrics/{id}`,
`update_content`) fal не вызывает и не биллится; `get`/`list` — read-only.

## Decision

Биллим синхронный `POST /v1/lyrics` схемой **атомарного одношагового списания (charge)
до генерации + компенсирующего возврата (refund) при любом сбое после списания** —
паттерн saga для синхронной операции. Резервный «reserved»-бакет (two-phase
reserve→capture) для sync НЕ используется (обоснование — §Alternatives).

### 1. Новые методы `CoinWalletService` (Job-free)

Существующие `capture(job)`/`release(job)` не трогаем (сохраняем идемпотентность
job-пути `capture:{job.id}` байт-в-байт). Добавляем два публичных метода, не зависящих
от `Job`:

- **`charge(user_id, job_type, *, idempotency_key, ref_type=None, ref_id=None) -> int`** —
  атомарное списание цены типа. **Порядок dedup-first — как в `capture`** (проверка средств
  идёт ПОСЛЕ дедупа ledger, иначе идемпотентный ретрай ложно падает 402):
  1. Цена типа из `PricingRepository.get_active_price`. Цена `<= 0` (нет строки /
     `price_coins=0`) → ранний `return 0`, **без записи в ledger** (обратная совместимость:
     бесплатный lyrics; при нулевой цене ledger бессмыслен). Проверка цены стоит ДО
     `append_ledger`.
  2. `ensure_wallet(user_id)` — строка `coin_wallets` под `SELECT ... FOR UPDATE`
     (сериализация конкурентных charge).
  3. `append_ledger(kind=debit_capture, amount=-price, idempotency_key, ref_type, ref_id,
     category=NULL)`.
  4. **Если запись НЕ новая** (`append_ledger` вернул `False` → дубликат по
     `idempotency_key`) → `return price` как **идемпотентный no-op**: без повторной проверки
     средств, без декремента кошелька. Это симметрично `refund(charged)` — при дубликате
     charge возвращает списанную цену, чтобы вызывающий видел ту же сумму, что была списана.
  5. Иначе (запись новая) проверить `coins_available >= price`; при нехватке —
     `raise InsufficientCredits(details={"required": price, "available": coins_available})`
     (402). Запись ledger откатится вместе с транзакцией `session.begin()`.
  6. Декремент `coins_available -= price`. Вернуть `price`.

  Ключевое отличие от прежнего порядка: проверка `coins_available < price` больше НЕ
  предшествует `append_ledger`. Иначе сценарий `available=10, price=10`: первый charge
  проходит (`available→0`, ledger записан), а ретрай с тем же `Idempotency-Key` видел бы
  `0 < 10` и падал ложным 402 ещё до того, как `append_ledger` сообщит о дубликате —
  прямое нарушение обещания §3 (идемпотентный ретрай не должен падать).
- **`refund(user_id, units, *, idempotency_key, ref_type=None, ref_id=None) -> None`** —
  компенсирующий возврат `units` монет в `coins_available`. Порядок dedup-first, зеркально
  `charge`/`release`: `units <= 0` → no-op (без записи ledger); иначе
  `append_ledger(kind=credit_refund, amount=+units, idempotency_key, ref_type, ref_id,
  category=NULL)`; **если запись НЕ новая → no-op** (`return`, дубликат уже возвращён);
  иначе — инкремент `coins_available += units` под `get_wallet_for_update`. НЕ трогает
  `coins_reserved` (sync-путь резерв не создаёт).

`reserve` для будущих sync-типов не требуется — charge самодостаточен (совмещает
проверку средств и списание в одной транзакции).

Оба метода используют существующие `CreditsRepository.append_ledger` (уже поддерживает
произвольные `kind`/`amount`/`ref_type`/`ref_id`/`idempotency_key`, дедуп по
`uq_credit_ledger_idempotency_key`) и `get_wallet_for_update`/`ensure_wallet` — новых
репозиторных методов и миграций НЕ требуется. Значения `CreditLedgerKind.debit_capture`
и `credit_refund` уже есть в enum (`app/domain/enums.py:84,86`).

### 2. Встраивание — в `LyricsService.generate` (доменный слой)

Биллинг живёт в доменном сервисе рядом с генерацией, которую он гейтит (симметрично
`create_job`), эндпоинт остаётся тонким. `LyricsService` получает инъекцию
`CoinWalletService` (сейчас у него только `sessionmaker` + `fal`). Порядок (saga):

```
op_id = idempotency_key заголовка запроса ИЛИ uuid4()   # идентификатор операции
charged = credits.charge(user_id, "lyrics",
                         idempotency_key=f"charge:lyrics:{op_id}",
                         ref_type="lyrics", ref_id=str(op_id))  # 402 до fal; 0 если бесплатно;
                                                                # дубль ключа → списанная цена (no-op)
try:
    content = fal.generate_lyrics(...)                    # платный внешний вызов
    draft   = LyricsRepository.create(...)                # запись драфта
except Exception:
    credits.refund(user_id, charged,
                   idempotency_key=f"refund:lyrics:{op_id}",
                   ref_type="lyrics", ref_id=str(op_id))  # компенсация; no-op если charged==0
    raise
return draft
```

- **charge ДО fal** — fail-fast 402 без траты денег на fal для неплатёжеспособного
  пользователя (402 возникает только на **первом** вызове с данным `op_id`; ретрай с тем
  же `Idempotency-Key` дедуп-first возвращает уже списанную цену, а не 402 — см. §3).
- **`ref_id=str(op_id)` и в charge, и в refund** — `draft_id` на момент charge ещё нет,
  а `op_id` служит естественным коррелятором «списание ↔ возврат» в `credit_ledger`
  (симметрия аудита).
- **refund при ЛЮБОМ исключении после charge** — покрывает и сбой fal, и сбой создания
  драфта. Утечки нет ни в одну сторону.
- Только `generate` (POST) биллится. `update_content` (PATCH-редактирование, fal не
  вызывается), `get`, `list` — бесплатны.

### 3. Идемпотентность

`POST /v1/lyrics` принимает опциональный заголовок `Idempotency-Key` (тот же паттерн,
что `client_idempotency_key` в `create_job`). Если задан — `op_id` = его значение, и
сетевой ретрай с тем же ключом **не приводит к повторному списанию**: dedup-first порядок
`charge` (append_ledger → дубликат → no-op) гейтит мутацию кошелька. Критично — ретрай
**не падает ложным 402**, даже если баланс уже обнулён первым списанием: проверка средств
выполняется только на новой записи ledger, а на дубликате charge просто возвращает ранее
списанную цену (см. §1). Если не задан — `op_id = uuid4()`, каждый POST = отдельная
генерация (новый драфт, новое списание): для творческого эндпоинта, где каждый вызов
даёт другой текст, это корректное поведение (пользователь нажал дважды = две генерации).

Дедуп **самого драфта** (возврат того же `LyricsDraft` на ретрай с тем же ключом) в этой
итерации НЕ реализуется — при повторе с `Idempotency-Key` списания не будет, но fal
вызовется снова и создастся новый драфт. Пробел зафиксирован как
[TD-009](../100-known-tech-debt.md#td-009) (severity low).

### 4. Контракт `POST /v1/lyrics`

- Новый исход **402 `INSUFFICIENT_CREDITS`**, `details={"required": <price>,
  "available": <coins>}` — тот же конверт ошибки, что у `create_job` (song/cover/video).
  iOS обязан обрабатывать 402 на lyrics (paywall) наравне с прочими генерациями.
- Новый опциональный заголовок запроса `Idempotency-Key` (§3).
- Успешный ответ 200 (`LyricsDraftResponse`) не меняется.

### 5. Цена и ledger

Цена всегда из `generation_prices` по `job_type="lyrics"`
(`PricingRepository.get_active_price` через `CoinWalletService.price_of`); **10 не
хардкодится**. Нет строки / `price_coins=0` → бесплатно (charge вернёт 0, строка в ledger
не пишется). Списание/возврат пишутся в `credit_ledger` едиными `kind`
(`debit_capture` / `credit_refund`), `amount` в монетах, `category=NULL` (монетная модель
ADR-005). Обе строки несут `ref_type="lyrics", ref_id=str(op_id)` — `op_id` коррелирует
списание с его возвратом (симметрия, аналог `ref_id=job.id` на job-пути). Аудит однороден
с job-путём (тот же `debit_capture` на списание).

## Consequences

- (+) Синхронный lyrics корректно списывает монеты; цена управляется admin-эндпоинтом без
  передеплоя (наследуется от ADR-005). Гость с балансом 0 получает 402, а не текст.
- (+) Минимальный blast radius: job-путь (`capture(job)`/`release(job)`, song/cover/video)
  не изменяется; новых таблиц/миграций/enum-значений нет — только два метода сервиса и
  инъекция в `LyricsService`.
- (+) Реиспользуемый примитив: `charge`/`refund` подойдёт любому будущему синхронному
  платному действию (напр. платный `voice_clone`, если станет sync).
- (+) `charge` следует dedup-first порядку `capture` (append_ledger → проверка средств
  только на новой записи): ретрай с тем же `Idempotency-Key` возвращает списанную цену
  как no-op и **не даёт ложного 402** даже при уже обнулённом балансе.
- (+) Для sync нет «зависшего резерва»: `coins_reserved` для lyrics всегда 0, стейт
  кошелька проще, чем при two-phase.
- (−) При падении процесса между commit'ом charge и refund пользователь **переплачивает
  одну генерацию** (монеты списаны, драфта нет). Восстановление — admin-грант; авто-recovery
  для sync-пути нет (у lyrics нет `Job`, `recover_orphan_jobs` его не видит). Риск низкий:
  окно между charge и refund — один синхронный fal-вызов; операция дешёвая (10 монет).
- (−) Без `Idempotency-Key` сетевой ретрай POST даёт двойное списание + двойную генерацию
  ([TD-009](../100-known-tech-debt.md#td-009), low): смягчение — клиент шлёт `Idempotency-Key`.
- (−) Ломающего изменения response нет, но добавляется исход 402 — iOS должен добавить его
  обработку на экране lyrics (допустимо, интеграция не завершена — как в ADR-005).

## Alternatives

- **Two-phase reserve → capture/release с синтетическим ключом** (обернуть lyrics в тот же
  цикл, что job, с `idempotency_key = lyrics:{op_id}`) — **отклонено**. Резервный бакет
  нужен для **асинхронного** окна расчёта (fal-задача завершается спустя минуты, capture
  идёт из webhook/поллера) и опирается на `recover_orphan_jobs` для возврата резервов
  упавших задач. У синхронного lyrics окна расчёта нет (весь цикл — один запрос),
  а recovery-раннера для не-`Job` резервов не существует. Значит reserve→capture добавил бы
  только новый режим отказа — **зависший резерв** в `coins_reserved` при падении процесса
  между `reserve` и `capture`/`release`, без авто-восстановления — не давая ничего взамен.
  Two-phase — для async/распределённого расчёта; для синхронной операции корректен
  одношаговый debit.
- **Списывать в эндпоинте, а не в `LyricsService`** — отклонено: биллинг-логика размазалась
  бы между роутом и доменом; `create_job` держит резерв в доменном сервисе — сохраняем
  симметрию, роут остаётся тонким.
- **Списывать ПОСЛЕ успешной генерации (без предоплаты)** — отклонено: пользователь с
  балансом 0 всё равно оплатил бы fal-вызов до отказа; charge-до-fal даёт fail-fast 402 без
  траты на провайдера.
- **Полный дедуп драфта по `Idempotency-Key`** (хранить `op_id → draft_id`, возвращать тот же
  драфт на ретрай) — отложено как [TD-009](../100-known-tech-debt.md#td-009): требует
  хранения маппинга; в текущей итерации достаточно дедупа списания.
- **Оставить lyrics бесплатным / хардкодить цену** — отклонено: противоречит явному
  требованию владельца (цена в `generation_prices`) и принципу ADR-005 (цена — данные, не код).

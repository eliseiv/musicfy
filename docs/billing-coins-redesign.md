# Переработка биллинга: единый кошелёк монет + прайс-лист

Статус: финализировано (architect). Открытых вопросов нет (Q-BILL-1, Q-BILL-2 закрыты решениями владельца). Реализация — backend по указаниям §8. Решение — [ADR-005](./adr/ADR-005-coin-wallet-billing.md).

Документ описывает переход с мультивалютной кредитной модели (раздельные балансы
`song`/`cover`/`video`) на модель «единый баланс монет + цена за тип генерации».

---

## 1. Оплачиваемые типы генерации и прайс-лист

### Инвентаризация из кода

`JobType` (`app/domain/enums.py`) содержит 5 типов. Сейчас списание задаётся
`JOB_TYPE_TO_CATEGORY`:

| JobType        | Сейчас (кредит) | Платный? | Обоснование |
|----------------|-----------------|----------|-------------|
| `song`         | song            | да       | основная генерация, самый частый платный запрос |
| `cover`        | cover           | да       | stem_separation → voice_conversion → mix_master, отдельная стоимость |
| `video`        | video           | да       | kling lipsync, самый дорогой и долгий пайплайн |
| `lyrics`       | — (None)        | **нет**  | дешёвый LLM-шаг, часто прелюдия к song; оставляем бесплатным |
| `voice_clone`  | — (None)        | **нет**  | подготовительный шаг (создание voice profile), не конечный продукт |

`voice_conversion`, `vocal_tts`, `lipsync`, `stem_separation` — это **внутренние стадии**
(`JobStage`, см. `app/domain/enums.py`) пайплайнов song/cover/video (`services/pipelines/*.py`),
а не самостоятельные `JobType`. Отдельной цены они не получают: их стоимость включена в цену
родительской генерации. Отдельно оплачиваемых стадий (`voice_conversion` / `vocal_tts` /
`lipsync` / `stem_separation`) как публичных `JobType` в коде нет.

### Прайс-лист (утверждённые начальные значения)

Владелец утвердил дефолты «на моё усмотрение, потом поменяем» (Q-BILL-2, закрыт). Значения —
начальные, изменяются через admin `PATCH /v1/admin/pricing/{jobType}` без передеплоя:

| Тип генерации | Цена, монет |
|---------------|-------------|
| `song`        | 10          |
| `cover`       | 5           |
| `video`       | 30          |
| `lyrics`      | 0           |
| `voice_clone` | 0           |

`lyrics` и `voice_clone` имеют цену 0 (в справочник `generation_prices` не заносятся) → резерв
не выполняется.

### Где хранится прайс-лист

Таблица в БД `generation_prices` (см. §2), засеивается миграцией, меняется admin-эндпоинтом
`PATCH /v1/admin/pricing/{jobType}` без передеплоя. Обоснование выбора (vs хардкод / settings)
— в [ADR-005](./adr/ADR-005-coin-wallet-billing.md) §Decision п.2.

---

## 2. Модель данных

### Новые таблицы

**`coin_wallets`** — единый баланс, одна строка на пользователя:

| Колонка          | Тип      | Прим. |
|------------------|----------|-------|
| id               | UUID PK  | `gen_random_uuid()` |
| user_id          | UUID FK users(id) ON DELETE CASCADE, **UNIQUE** | один кошелёк на юзера |
| coins_available  | BIGINT NOT NULL DEFAULT 0 | доступно к списанию |
| coins_reserved   | BIGINT NOT NULL DEFAULT 0 | зарезервировано под активные джобы |
| created_at / updated_at | timestamptz | |

Инвариант: `coins_available >= 0`, `coins_reserved >= 0`.

**`generation_prices`** — прайс-лист:

| Колонка     | Тип      | Прим. |
|-------------|----------|-------|
| id          | UUID PK  | |
| job_type    | credit-независимый `String(32)` **UNIQUE** (значение из `JobType`: `song`/`cover`/`video`) | |
| price_coins | INT NOT NULL, CHECK (price_coins >= 0) | |
| active      | BOOL NOT NULL DEFAULT true | |
| created_at / updated_at | timestamptz | |

Seed: `('song',10)`, `('cover',5)`, `('video',30)`.

### Переиспользуемые таблицы

- **`credit_ledger`** — остаётся как аудит-журнал монетных операций. `amount` теперь в монетах.
  Колонка `category` перестаёт заполняться (пишется `NULL`); менять схему не обязательно, но
  рекомендуется в дизайне пометить её deprecated. `kind`/`source` сохраняются (provenance:
  `credit_purchase`, `credit_subscription_grant`, `credit_promo`, `debit_reserve`,
  `debit_capture`, `credit_release`, `credit_refund`, `*_adjustment`).
- **`products`** — таблица остаётся; смысл `grants` меняется на `{"coins": N}` (см. §4).
- **`purchases`** — без изменений.
- **`subscription_state`** — остаётся для трекинга статуса/срока подписки и обработки
  renewal/revoke; **больше не порождает entitlements**, при renewal начисляет монеты в кошелёк.

### Удаляемые таблицы

- **`entitlements`** — удаляется (подписка теперь просто начисляет монеты).
- **`credit_balances`** — удаляется (заменяется `coin_wallets`).

### Модель `Job`

`app/domain/models/job.py`:
- `reserved_credits` — теперь хранит **цену в монетах**, зарезервированную под джобу (например 10).
- `credit_category` — становится неиспользуемой; оставить nullable для совместимости миграции,
  писать `NULL`. Списание не зависит от категории.
- Payload-флаг `_credit_source` (`input_payload["_credit_source"]`) — **удаляется** (единый
  источник, ветвление subscription/purchase не нужно).

---

## 3. Механика reserve → capture → release (в монетах)

Сервис `EntitlementService` переименовывается в `CoinWalletService` (файл
`services/credits.py`). Все три операции работают с единственной строкой `coin_wallets`
пользователя под `SELECT ... FOR UPDATE` (атомарность резерва).

```
price_of(job_type):
    row = generation_prices WHERE job_type = job_type AND active
    return row.price_coins if row else 0        # неизвестный/бесплатный тип → 0

reserve(user_id, job_type) -> price:
    price = price_of(job_type)
    if price == 0: return 0                      # бесплатно, резерва нет
    with tx:
        w = SELECT * FROM coin_wallets WHERE user_id=? FOR UPDATE   # создать при отсутствии
        if w.coins_available < price: raise InsufficientCredits(required=price, available=w.coins_available)
        w.coins_available -= price
        w.coins_reserved  += price
        ledger(kind=debit_reserve, amount=-price, ref=job?)
    return price

capture(job):                                    # идемпотентно по job (idempotency_key=capture:{job.id})
    units = job.reserved_credits
    if units <= 0: return
    with tx:
        newly = ledger(kind=debit_capture, amount=-units, idem=f"capture:{job.id}")
        if not newly: return
        w = SELECT ... FOR UPDATE
        w.coins_reserved = max(0, w.coins_reserved - units)

release(job):                                    # refund при провале, idempotency_key=release:{job.id}
    units = job.reserved_credits
    if units <= 0: return
    with tx:
        newly = ledger(kind=credit_release, amount=units, idem=f"release:{job.id}")
        if not newly: return
        w = SELECT ... FOR UPDATE
        w.coins_reserved  = max(0, w.coins_reserved - units)
        w.coins_available += units
```

`generation_service.create_job` (`services/generation_service.py`): вместо
`reserve(user_id, category, units=1)` вызывает `reserve(user_id, job_type=job_type)`,
сохраняет вернувшуюся цену в `reserved_credits`, не пишет `_credit_source`. Для бесплатных
типов `reserve` вернёт 0 → `reserved_credits=0`, поведение как сейчас для `lyrics`/`voice_clone`.
`release` при ошибке старта пайплайна и `capture` при успехе — как сейчас, но по монетам.

`capture` и `release` зависят **только** от `job.reserved_credits` (монеты): условие вида
`job.credit_category is None` из обеих операций **удаляется** — при `credit_category = NULL`
(теперь это норма) списание/возврат резерва не должны прекращаться. `capture` списывает
**полную** зарезервированную цену `reserved_credits` — параметр `used_units` из сигнатуры
`capture` **удаляется** (частичного списания нет).

Инварианты для backend: (1) резерв атомарен через `FOR UPDATE`; (2) capture/release
идемпотентны по ledger `idempotency_key`; (3) refund выполняется при любом терминальном
`failed`/`canceled`; (4) `coins_available`/`coins_reserved` не уходят в минус; (5) capture/release
управляются исключительно `reserved_credits`, а не `credit_category`.

---

## 4. Каталог продуктов (пакеты монет)

`products.grants` = `{"coins": N}`. Каталог утверждён владельцем как начальные значения
(Q-BILL-2, закрыт: «на моё усмотрение, потом поменяем»); изменяется пересидом каталога
(миграция) и/или admin-грантами. Те же `product_id` заводятся в App Store Connect:

| external_product_id          | kind         | title        | grants          | period_days |
|------------------------------|--------------|--------------|-----------------|-------------|
| `com.musicfy.coins.small`    | `coin_pack`  | 100 Coins    | `{"coins":100}` | —           |
| `com.musicfy.coins.medium`   | `coin_pack`  | 550 Coins    | `{"coins":550}` | —           |
| `com.musicfy.coins.large`    | `coin_pack`  | 1200 Coins   | `{"coins":1200}`| —           |
| `com.musicfy.coins.xl`       | `coin_pack`  | 3000 Coins   | `{"coins":3000}`| —           |
| `com.musicfy.sub.weekly`     | `subscription` | Weekly     | `{"coins":150}` | 7           |
| `com.musicfy.sub.yearly`     | `subscription` | Yearly     | `{"coins":8000}`| 365         |

Паки монет — consumable (начисляют монеты один раз за транзакцию, идемпотентно). Подписки —
auto-renewable: начисляют `coins` при каждом подтверждённом периоде (renewal → повторный
грант монет). **Монеты копятся бессрочно и не сгорают** (Q-BILL-1, закрыт решением владельца):
подписка = регулярное пополнение общего кошелька `coin_wallets`. Подписочные монеты **не
требуют** отдельного учёта срока годности, партий или порядка списания — они неотличимы от
покупных монет в едином балансе.

Enum `product_kind` (PG): добавляется значение `coin_pack`. Значения
`song_pack`/`cover_pack`/`video_pack`/`mixed_pack` остаются в enum (PG не удаляет значения
легко), но в каталоге не используются.

`BillingService._apply` (`services/billing_service.py`): и подписка, и пак начисляют
`grants["coins"]` в `coin_wallets` единым merged-грантом. Грант **обязан быть идемпотентным по
транзакции**: `idempotency_key = purchase:{transaction_id}` (без суффикса `:{category}`).
Инкремент `coins_available` выполняется **только** если запись ledger создана впервые
(проверка `newly` ПЕРЕД инкрементом). Подписочная ветка **не выполняется безусловно** — она
проходит ту же проверку `newly`, иначе повторный verify/notification даёт двойное начисление
(прежняя set-семантика upsert entitlement, защищавшая подписку, устранена вместе с таблицей
`entitlements`). Подписка дополнительно апдейтит `subscription_state` (upsert по статусу/сроку,
без начисления монет вне идемпотентного грант-шага). Ledger `kind` — `credit_purchase` для
паков, `credit_subscription_grant` для подписок.

---

## 5. Изменения API-контракта

**Все перечисленные изменения — ломающие** (iOS переинтегрируется; интеграция не завершена).
`openapi.json` генерируется FastAPI из кода — backend перегенерирует артефакт после реализации
(architect не правит его вручную).

### `GET /v1/billing/balance` — BREAKING

Было: `{ "balances": [ {category, subscriptionRemaining, subscriptionGranted, periodEnd, purchasedAvailable}, ... ] }`

Стало:
```json
{ "coinsAvailable": 120, "coinsReserved": 10 }
```

### `GET /v1/billing/pricing` — НОВЫЙ

```json
{ "prices": [
  { "jobType": "song",  "priceCoins": 10 },
  { "jobType": "cover", "priceCoins": 5 },
  { "jobType": "video", "priceCoins": 30 }
] }
```
Возвращаются только активные платные типы; отсутствующий тип iOS трактует как бесплатный.

### `GET /v1/billing/products` — BREAKING (смысл `grants`)

`grants` теперь `{"coins": N}` вместо per-category. Форма ответа:
```json
[ { "productId": "com.musicfy.coins.small", "kind": "coin_pack", "title": "100 Coins",
    "grants": {"coins": 100}, "periodDays": null }, ... ]
```

### `POST /v1/billing/purchases/verify`, `POST /v1/billing/restore` — контракт запроса/ответа без изменений

`ApplyResultResponse {status, deduplicated}` сохраняется. Внутренне начисляют монеты в кошелёк.

### `GET /v1/billing/ledger` — минорно

`amount` теперь в монетах; `category` всегда `null`. Форма ответа сохраняется.

### Ошибка `INSUFFICIENT_CREDITS` (402) — BREAKING (details)

Было: `details: {"category": "song", "required": 1}`.
Стало: `details: {"required": 10, "available": 3}`. Код ошибки и роль paywall-триггера сохраняются.

### Admin — BREAKING

- `GET /v1/admin/users/{id}/balance` → `{ "userId", "coinsAvailable", "coinsReserved" }`.
- `POST /v1/admin/users/{id}/credits` → body `{ "coins": N, "reason": str }` (убрать `category`);
  начисляет монеты в кошелёк, ledger `credit_adjustment`/`credit_promo`.
- `POST /v1/admin/users/{id}/subscription` → body `{ "coins": N, "periodDays": int, "label": str }`
  (убрать `song`/`cover`/`video`); начисляет монеты + апдейт `subscription_state`.
- `DELETE /v1/admin/users/{id}/subscription` → без изменений семантики (revoke), ответ — кошелёк.
- `PATCH /v1/admin/pricing/{jobType}` → **новый**, body `{ "priceCoins": int, "active": bool }`;
  обновляет строку `generation_prices`.

---

## 6. Стратегия миграции данных

Реальных платящих пользователей нет → **reset, без конвертации** (обоснование — ADR-005).

Порядок (**три** миграции, head сейчас `0008`). Миграции нумеруются строго `0009 → 0010 → 0011`;
эта же нумерация обязательна в §8 п.8:

**`0009_coin_wallet.py`**
1. `CREATE TABLE coin_wallets` (см. §2).
2. `CREATE TABLE generation_prices` + seed `song=10, cover=5, video=30`.
3. `DROP TABLE entitlements`; `DROP TABLE credit_balances`.
4. (кошельки не пересоздаются из старых балансов — reset). `credit_ledger` не трогаем.
5. `downgrade`: воссоздать `entitlements`/`credit_balances`, удалить новые таблицы (симметрично 0004).

**`0010_add_coin_pack_enum.py`** — только расширение enum, без использования значения:
1. `ALTER TYPE product_kind ADD VALUE IF NOT EXISTS 'coin_pack'`.
2. `downgrade`: no-op (PostgreSQL не удаляет значения enum; задокументировать).

**`0011_reseed_coin_products.py`** — пересид каталога, использует `coin_pack`:
1. `DELETE FROM products;` затем insert каталога §4 с `kind = 'coin_pack'` и `grants = {"coins": N}`.
2. `downgrade`: восстановить каталог `0005` (per-category grants).

Примечание backend/devops (критично): `ALTER TYPE ... ADD VALUE` в PostgreSQL (PG12+) нельзя
использовать в той же транзакции, где значение вставляется — иначе миграция падает. Поэтому
`ADD VALUE` вынесено в **отдельную** миграцию `0010`, а использование (`INSERT ... 'coin_pack'`)
— в `0011`; так значение enum гарантированно закоммичено раньше использования. Если по каким-то
причинам объединять шаги в одной миграции — `ADD VALUE` обязан идти в `op.get_context().autocommit_block()`
строго ДО `INSERT`'ов. Предпочтителен раздельный вариант `0010`/`0011`. TD-001 (нет авто-отката
миграций) остаётся в силе — downgrade-скрипты обязательны, но применяются вручную.

**Альтернатива (не применяем): конвертация по курсу**
`coins_available = song_bal*10 + cover_bal*5 + video_bal*30 + Σ(active entitlement remaining × цена категории)`.
Задокументирована на случай появления реальных балансов до релиза.

---

## 7. Влияние на существующее

- **Admin** (`api/v1/admin.py`, `services/admin_service.py`, `schemas/admin.py`): per-category
  гранты → монеты (§5). `_parse_category` удаляется.
- **openapi.json**: перегенерировать после реализации (артефакт из FastAPI).
- **ARCHITECTURE.md**: разделы «Кредиты и лимиты» и «Биллинг» обновлены под монетную модель.
- **Тесты** (`tests/test_billing.py` и связанные) — переписывает qa под монеты/прайс-лист.
- **TD-002 / TD-003** — не связаны с биллингом (стадии/webhook fal), не затрагиваются.
- **iOS** — ломающее изменение контракта баланса/продуктов/админки; интеграция ещё идёт,
  поэтому допустимо; новые `product_id` завести в App Store Connect.

---

## 8. Указания для backend (реализация; НЕ выполнено architect)

Менять по слоям:

1. **enums** (`app/domain/enums.py`):
   - `ProductKind`: добавить `coin_pack`.
   - `JOB_TYPE_TO_CATEGORY` — удалить (списание теперь по `generation_prices`, не по категории);
     бесплатность `lyrics`/`voice_clone` определяется отсутствием строки в прайс-листе.
   - `CreditCategory` — оставить тип (используется в старом ledger/enum БД), но новые записи
     категорию не пишут.

2. **models** (`app/domain/models/billing.py`):
   - Новые модели `CoinWallet`, `GenerationPrice`.
   - Удалить модели `Entitlement`, `CreditBalance`.
   - `Product.grants` — семантика `{"coins": N}` (тип JSONB не меняется).
   - `Job` (`models/job.py`): `reserved_credits` = цена в монетах; `credit_category` писать NULL.

3. **repositories**:
   - `repositories/credits.py` → работа с `coin_wallets` (`get_wallet_for_update`,
     `ensure_wallet`, `list_ledger`), убрать entitlement/balance методы.
   - Новый `repositories/pricing.py` (или расширить products) — чтение/обновление `generation_prices`.

4. **services**:
   - `services/credits.py`: `EntitlementService` → `CoinWalletService`; `price_of`,
     `reserve(job_type)`, `capture`, `release` в монетах (§3). Убрать `_source_of`,
     `_entitlement_active`, `BalanceView` по категориям → единый `WalletView{available, reserved}`.
     - **[Правка 1]** В `capture()` и `release()` **удалить** условие `job.credit_category is None`
       из guard'а. Оставить только проверку по резерву: `if units <= 0: return` (где
       `units = job.reserved_credits`). При `credit_category = NULL` (норма новой модели) списание
       и возврат резерва обязаны продолжать работать; зависимость от категории недопустима.
     - **[Правка 5]** Из сигнатуры `capture` **удалить** параметр `used_units`: становится
       `async def capture(self, *, job: Job) -> int`. Списывается **полная** зарезервированная цена
       `job.reserved_credits` (частичного списания нет). Обновить всех вызывающих (`generation_service`).
   - `services/generation_service.py`: `reserve(user_id, job_type=...)`, `reserved_credits=price`,
     убрать `_credit_source` из payload. Вызов `capture` — без `used_units` (см. Правка 5).
   - `services/billing_service.py`: `_grant_subscription`/`_grant_pack` → единый merged-грант монет
     в `coin_wallets`; подписка дополнительно апдейтит `subscription_state`.
     - **[Правка 2]** Merged-грант **обязан быть идемпотентным по транзакции**:
       `idempotency_key = purchase:{transaction_id}` (в `_grant_pack` **убрать** суффикс
       `:{category.value}`). Инкремент `coins_available` выполнять **только** при `newly is True`
       (ledger-запись создана впервые) — проверка `newly` идёт ПЕРЕД инкрементом. Подписочную ветку
       **не выполнять безусловно**: `_grant_subscription` проходит ту же проверку `newly`, иначе
       повторный verify/notification даёт двойное начисление (защита прежнего upsert entitlement
       устранена вместе с таблицей). `subscription_state` апдейтить upsert'ом (idempotent по статусу/сроку).
   - `services/admin_service.py`: `grant_credits(coins)`, `grant_subscription(coins, period_days)`,
     новый `set_price(job_type, price_coins, active)`.

5. **schemas** (`app/domain/schemas/billing.py`, `schemas/admin.py`):
   - `BalanceResponse{coinsAvailable, coinsReserved}`; удалить `CategoryBalance`.
   - Новый `PricingResponse{prices:[{jobType, priceCoins}]}`.
   - `ProductView.grants` = `{"coins": N}`.
   - `GrantCreditsRequest{coins, reason}`; `GrantSubscriptionRequest{coins, periodDays, label}`;
     новый `SetPriceRequest{priceCoins, active}`; admin balance response в монетах.

6. **api** (`app/api/v1/billing.py`, `api/v1/admin.py`):
   - `GET /billing/balance` — монеты; новый `GET /billing/pricing`; `GET /billing/products` —
     новый grants; ledger — как есть.
   - admin: credits/subscription/balance в монетах; новый `PATCH /admin/pricing/{jobType}`.

7. **errors** (`app/api/errors.py`): `InsufficientCredits.details` = `{required, available}`.

8. **migrations** (§6, ровно три, нумерация строгая):
   - `0009_coin_wallet.py` — таблицы `coin_wallets`/`generation_prices`, seed цен, drop
     `entitlements`/`credit_balances`.
   - **[Правка 3]** `0010_add_coin_pack_enum.py` — **только** `ALTER TYPE product_kind ADD VALUE
     IF NOT EXISTS 'coin_pack'`, без использования значения. Значение enum обязано быть закоммичено
     **до** его использования (PG12+ падает при использовании в той же транзакции).
   - **[Правка 3]** `0011_reseed_coin_products.py` — `DELETE FROM products` + insert каталога §4
     c `kind='coin_pack'`. Использование `coin_pack` только здесь, после коммита `0010`.
   - Если (вопреки рекомендации) объединять `ADD VALUE` и reseed в одной миграции — `ADD VALUE`
     обязан идти в `op.get_context().autocommit_block()` строго ДО `INSERT`'ов. Предпочтителен
     раздельный вариант `0010`/`0011`.

Инварианты (обязательны): атомарность резерва (`FOR UPDATE` на `coin_wallets`); идемпотентность
покупок (`purchase:{transaction_id}`, инкремент только при `newly`) и capture/release по
`credit_ledger.idempotency_key`; capture/release управляются только `reserved_credits` (не
`credit_category`); refund монет при `failed`/`canceled`; баланс не отрицателен.

---

## Открытые вопросы

Открытых вопросов нет — оба закрыты решениями владельца при финализации:

- **Q-BILL-1 (закрыт).** Монеты, начисленные подпиской, **копятся бессрочно и не сгорают** в
  конце периода. Подписка = регулярное пополнение единого кошелька `coin_wallets`. Отдельного
  учёта срока годности / партий монет не требуется (см. §4, ADR-005 §Decision п.1 и §Consequences).
- **Q-BILL-2 (закрыт).** Дефолтные цены и каталог утверждены владельцем («на моё усмотрение,
  потом поменяем») как начальные значения: прайс-лист `song=10, cover=5, video=30, lyrics=0,
  voice_clone=0` (§1); каталог `coins.small=100, coins.medium=550, coins.large=1200,
  coins.xl=3000, sub.weekly=+150/7дн, sub.yearly=+8000/365дн` (§4). Изменяются через admin
  `PATCH /v1/admin/pricing` и пересид каталога без нового ADR.

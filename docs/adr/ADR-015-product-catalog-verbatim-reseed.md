# ADR-015. Замена каталога продуктов биллинга на вербатим-каталог App Store Connect

- Статус: Accepted
- Дата: 2026-07-14
- Дополняет: [ADR-005](./ADR-005-coin-wallet-billing.md) (единый кошелёк монет), [ADR-013](./ADR-013-storekit-dedup-environment-scoping.md) (дедуп), [ADR-014](./ADR-014-storekit-cn-trust-xcode-flag.md) (CN-trust)
- Не заменяет: механику начисления/дедупа (ADR-005/013/014) — меняется только состав каталога.

## Контекст

Прежний каталог продуктов (`external_product_id` вида `com.musicfy.coins.*`, `com.musicfy.sub.*`,
засеянный миграцией [0011](../../migrations/versions/0011_reseed_coin_products.py)) не совпадает с
идентификаторами, заведёнными владельцем в **App Store Connect** и в локальном `.storekit`. StoreKit
матчит покупку по `product_id` **вербатим**: если `external_product_id` в Б.Д. не байт-в-байт равен
`product_id` из App Store, то `BillingService.apply` получит `product is None` →
`{"status":"ignored","reason":"unknown_product"}` → монеты не начислятся при валидной оплате.

Владелец зафиксировал финальный список `product_id` (вербатим, включая точки и суффиксы цены). Нужно
привести каталог в БД к этому списку, **не потеряв** историю прошлых покупок (`Purchase.product_external_id`,
`SubscriptionState.product_external_id` ссылаются на `products.external_product_id`).

## Решение

### 1. Новый каталог (`active=true`)

Coin-паки — `kind=coin_pack`, `period_days=NULL`, `grants={"coins":N}`:

| external_product_id (вербатим) | title | grants |
|---|---|---|
| `100_tokens_9.99` | `100 Tokens` | `{"coins":100}` |
| `250_tokens_19.99` | `250 Tokens` | `{"coins":250}` |
| `500_tokens_34.99` | `500 Tokens` | `{"coins":500}` |
| `1000_tokens_59.99` | `1000 Tokens` | `{"coins":1000}` |
| `2000_tokens_99.99` | `2000 Tokens` | `{"coins":2000}` |

Подписки — `kind=subscription`, `grants={"coins":N}` за период:

| external_product_id (вербатим) | title | grants | period_days |
|---|---|---|---|
| `week_6.99_not_trial` | `Weekly` | `{"coins":100}` | 7 |
| `yearly_49.99_not_trial` | `Yearly` | `{"coins":1000}` | 365 |

Стиль `title`: coin-паки — `"{N} Tokens"`; подписки — `"Weekly"` / `"Yearly"` (человекочитаемо,
только для UI/отладки; для клиента цену держит App Store).

### 2. Деактивация прежнего каталога (`active=false`, строки НЕ удаляются)

`com.musicfy.coins.small`, `com.musicfy.coins.medium`, `com.musicfy.coins.large`,
`com.musicfy.coins.xl`, `com.musicfy.sub.weekly`, `com.musicfy.sub.yearly`.

Деактивация ≠ удаление: строки сохраняются, чтобы FK из `purchases` / `subscription_state` и резолв
уже совершённых покупок продолжали работать.

### 3. Инвариант резолва продукта: `get_by_external_id` НЕ фильтрует по `active`

`ProductsRepository.get_by_external_id` (`app/domain/repositories/products.py`) резолвит продукт
**без** условия `active` — и это обязано так остаться. Применение покупки/продления
(`BillingService.apply` → `get_by_external_id(tx["product_id"])`) должно находить и **неактивный**
продукт, иначе:

- продление старой подписки (`com.musicfy.sub.weekly/yearly`) после деактивации → `product is None`
  → `ignored/unknown_product` → у уже подписанного пользователя перестают начисляться монеты;
- повторный `verify`/restore старого чека перестаёт резолвиться.

Только клиентский каталог (`GET /v1/billing/products` → `list_active`) фильтрует `active=true` —
и отдаёт ровно 7 новых продуктов.

### 4. Что НЕ меняется

- `generation_prices` (цены генераций) — отдельная таблица, не трогается.
- У `products` нет и не вводится колонка `price` — цену держит App Store.
- Дедуп/грант (ADR-013/014), enum `product_kind` (`coin_pack` есть с 0010, `subscription` с 0004 —
  миграция enum не нужна).

## Миграция 0017 (контракт для backend)

Ревизия `0017_*`, `down_revision = "0016_purchase_dedup_key"`. Паттерн — как в
[0011](../../migrations/versions/0011_reseed_coin_products.py) (raw SQL, `CAST(:kind AS product_kind)`,
`CAST(:grants AS jsonb)`), но **идемпотентно через upsert**, без `DELETE FROM products`.

**upgrade():**
1. Upsert 7 новых продуктов:
   `INSERT INTO products (external_product_id, kind, title, grants, period_days, active)
   VALUES (:pid, CAST(:kind AS product_kind), :title, CAST(:grants AS jsonb), :period, true)
   ON CONFLICT ON CONSTRAINT uq_products_external_product_id
   DO UPDATE SET kind=EXCLUDED.kind, title=EXCLUDED.title, grants=EXCLUDED.grants,
   period_days=EXCLUDED.period_days, active=true, updated_at=now()`.
2. `UPDATE products SET active=false, updated_at=now() WHERE external_product_id IN
   ('com.musicfy.coins.small','com.musicfy.coins.medium','com.musicfy.coins.large',
   'com.musicfy.coins.xl','com.musicfy.sub.weekly','com.musicfy.sub.yearly')`.

Оба шага идемпотентны (повторный прогон даёт тот же результат).

**downgrade() (обратимо, FK-safe):**
1. `UPDATE products SET active=true WHERE external_product_id IN (6 старых)`.
2. `UPDATE products SET active=false WHERE external_product_id IN (7 новых)`.

Downgrade **деактивирует** новые (а не `DELETE`), чтобы не упасть на FK, если за окно жизни ревизии
по новому продукту успели пройти покупки. Симметрично upgrade и полностью обратимо.

## Последствия

Положительные:
- `product_id` из App Store Connect / `.storekit` резолвятся вербатим → покупки начисляются.
- История и продления старого каталога не ломаются (деактивация + резолв без `active`).
- Идемпотентная миграция безопасна к повторному прогону; downgrade без потери данных.

Риски / обязательства для backend:
- **Не добавлять** фильтр `active` в `get_by_external_id` (регресс сломает продления старых подписок).
  Требуется регресс-тест: продукт `active=false` резолвится и начисляет монеты через `apply`.
- Вербатим-строки (`100_tokens_9.99`, `week_6.99_not_trial`, …) — источник истины ADR §1; любое
  расхождение с App Store Connect воспроизводит исходный баг `unknown_product`.

## Альтернативы

- **`DELETE` старых продуктов** — отклонено: рвёт FK из `purchases`/`subscription_state`, теряет аудит.
- **`DELETE` новых в downgrade** — отклонено в пользу деактивации: FK-риск при покупках за окно ревизии.
- **Фильтровать `active` в резолве покупки** — отклонено: ломает продления/restore старого каталога.
- **Хранить `product_id` в конфиге, а не в БД** — отклонено: каталог — данные, паттерн проекта — seed
  миграцией + таблица `products` как источник для `GET /v1/billing/products`.

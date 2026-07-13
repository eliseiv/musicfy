# ADR-014 — CN-trust Xcode StoreKit Test сертификатов за флагом (для Testing-режима)

- Статус: Accepted
- Дата: 2026-07-13
- Контекст: [ADR-013](./ADR-013-storekit-dedup-environment-scoping.md) §D3 доверяет Xcode-ветке
  только по **пину DER** конкретного тестового корня (`APPLE_STOREKIT_TEST_ROOT_CERTS`). У каждой
  машины Xcode свой уникальный self-signed сертификат `CN="StoreKit Testing in Xcode"`,
  перевыпускаемый ~раз в год, поэтому пин по DER не масштабируется на нескольких тестеров.
- Связанные: [ADR-013](./ADR-013-storekit-dedup-environment-scoping.md) (trust anchor + дедуп),
  [ADR-001](./ADR-001-fail-fast-db-password.md) (прецедент fail-fast на небезопасный prod-конфиг),
  [Q-BILL-1](../99-open-questions.md#q-bill-1), [TD-010](../100-known-tech-debt.md#td-010).

## Контекст

### Симптом

Приложение ещё в Testing-режиме (не опубликовано в App Store). iOS-разработчики тестируют
покупки против **прод**-бэкенда через Xcode StoreKit local testing. По ADR-013 §D3 корневой
self-signed сертификат машины разработчика пинится в `APPLE_STOREKIT_TEST_ROOT_CERTS` по точному
DER. Подтверждено вживую: у второго тестера сертификат с другим сроком (`not_valid_after`
2026-04-09 против 2026-05-05 у первого) → **другой DER** → `_verify_chain` не находит корень ни в
Apple Root CA - G3, ни в пине → `WebhookPayloadInvalid(untrusted_root)` → HTTP 400. Покупка не
проходит.

### Природа Xcode-сертификата

Xcode StoreKit local testing подписывает транзакцию **одним** self-signed сертификатом (x5c
длины 1), у которого:

- `subject == issuer` (self-signed);
- `CN == "StoreKit Testing in Xcode"` (константа Xcode);
- открытый ключ — EC (ES256), тот же, которым подписан JWS;
- `not_valid_before / not_valid_after` — локально сгенерированный срок (обычно ~год), уникальный
  для каждой машины/перевыпуска.

DER этого сертификата уникален для каждой машины и каждого перевыпуска. Пин по DER (ADR-013)
корректен и **безопасен**, но требует поштучного enrollment каждого тестера (ops-нагрузка,
[TD-010](../100-known-tech-debt.md#td-010)). Для команды из нескольких тестеров это не
масштабируется.

### Решение владельца

Владелец продукта принял **осознанное** решение: пока приложение в Testing-режиме, доверять
**любому** сертификату с признаками Xcode-теста (по CN + self-signed EC), а не по пину DER —
чтобы покупки работали у любого тестера без ручного пина каждой машины. Риск (см. ниже) принят
явно и ограничен Testing-режимом.

## Решение

Вводится **CN-trust** для Xcode-ветки — доверие Xcode-тест-сертификату по его признакам, а не по
пину DER. CN-trust **гейтится отдельным флагом** и по умолчанию **выключен** (fail-safe).

### D1. Новый конфиг-флаг `APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS` (дефолт `false`)

- Тип `bool`, дефолт **`false`** → прод по умолчанию строгий (поведение ADR-013 без изменений).
- Включается **явно** на проде на время Testing-режима осознанным аудируемым актом.
- В отличие от `APPLE_STOREKIT_VERIFY_SIGNATURE`, этот флаг **легален при `APP_ENV=prod`**
  (в этом весь смысл: тестеры бьют по прод-бэкенду). **Fail-fast на него не вешается** — иначе
  прод не поднялся бы с включённым флагом, ради которого он и вводится. Единственная защита от
  забытого флага в бою — дефолт `false` + обязательный pre-launch чеклист (D4, DEPLOYMENT.md).

### D2. CN-trust активен только при `verify_signature=true` и флаге `true`

CN-trust вычисляется **внутри** `_verify_chain`, который выполняется только когда
`verify_signature=true`. Поэтому CN-trust **не ослабляет** fail-fast ADR-013:
`prod + verify_signature=false` по-прежнему не поднимает приложение. Подпись JWS проверяется
всегда; CN-trust лишь расширяет множество допустимых **корней** для форс-окружения `Xcode`.

Резолв корня цепочки (порядок branch'ей в `_verify_chain`, дополняет ADR-013 §D3):

1. корень == закреплённый **Apple Root CA - G3** → `forced_env = None`, claim `environment`
   (`Production`/`Sandbox`) доверенный. **Боевой путь Apple не меняется.**
2. корень ∈ `APPLE_STOREKIT_TEST_ROOT_CERTS` (пин по DER) → `forced_env = Xcode`. **Не меняется.**
3. **NEW** — флаг `APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS=true` **И** корень цепочки (`certs[-1]`)
   удовлетворяет признакам Xcode-тест-сертификата (D3) → `forced_env = Xcode`, **без** требования
   совпадения DER с пином. Логируется `WARNING` (CN-trust сработал без пина).
4. иначе (флаг выкл **или** признаки не совпали) → `WebhookPayloadInvalid(untrusted_root)` — как
   в ADR-013.

Branch 1 и 2 проверяются **раньше** branch 3, поэтому CN-trust — только fallback для корней, не
совпавших с Apple G3 / DER-пином. CN-trust форсит `environment = Xcode` **всегда** → такие
транзакции никогда не попадают в глобальный namespace `Production`/`Sandbox`.

### D3. Признаки Xcode-тест-сертификата (что именно проверять)

Функция-предикат `_is_xcode_test_root(cert) -> bool` над корнем цепочки `certs[-1]`. Все условия
обязательны (AND):

| Признак | Проверка |
|---|---|
| self-signed | `cert.subject == cert.issuer` |
| CN | `CN` в `cert.subject` == `"StoreKit Testing in Xcode"` (точное совпадение) |
| EC-ключ | `isinstance(cert.public_key(), ec.EllipticCurvePublicKey)` |
| **криптографически** self-signed | `cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))` — не бросил `InvalidSignature` |
| срок валиден | уже проверено циклом дат в `_verify_chain` для всех `certs` (переиспользуется) |
| JWS подписан ключом этого cert | уже гарантировано финальным `jwt.decode(signed, leaf_pub_pem, ES256)`; для x5c длины 1 `leaf == certs[-1]`, т.е. JWS верифицируется ключом того же сертификата |

`subject == issuer` — лишь **заявление** о self-signed; поэтому обязателен и криптографический
шаг (verify собственной подписи), доказывающий, что это действительно self-signed EC-сертификат,
а не подставленный subject. Только совпадение всех признаков даёт `Xcode`.

### D4. Дедуп Xcode-ветки не меняется

CN-trust даёт ровно тот же `forced_env = Xcode`, что и DER-пин. Дедуп-ключ Xcode-ветки —
`Xcode:{user_id}:{transaction_id}:{purchase_date_ms}` (per-user + момент покупки, ADR-013 §D1) —
**без изменений**. Боевые пути `Production`/`Sandbox` (глобальный namespace, replay-защита) и пин
по DER не затрагиваются. `BillingService._dedup_key`, миграция `0016`, схема БД — не меняются.

## Риск (принят явно) и blast radius

При **включённом** флаге на боевом контуре **любой** может сгенерировать self-signed EC-сертификат
с `CN="StoreKit Testing in Xcode"`, подписать им произвольный JWS
(`{"transactionId": ..., "productId": "com.musicfy.coins.large", "environment": "Xcode", ...}`) и
намайнить коины **бесплатно**.

**Blast radius ограничен и известен:**

- Коины начисляются **только на собственный аккаунт атакующего** — Xcode-ветка форсит дедуп-ключ
  `Xcode:{user_id}:...` (per-user). Атакующий **не может** повлиять на чужой баланс, погасить/
  подделать чужой боевой чек или тронуть глобальный namespace `Production`/`Sandbox`.
- Боевые (`Production`) и `Sandbox`-покупки **не затрагиваются**: их путь (Apple G3 → claim
  `environment`) проверяется раньше и остаётся строгим. Реальная монетизация не ослаблена.
- Тестовые начисления отделимы: `purchases.environment = 'Xcode'` (аудит, зачистка перед
  финансовой отчётностью — [TD-010](../100-known-tech-debt.md#td-010)).

**Приемлемо ТОЛЬКО в Testing-режиме до публичного релиза.** Экономического смысла для атакующего
почти нет (коины только себе, продукт ещё не в App Store), а выигрыш — работающее тестирование
покупок у любого тестера без поштучного пина.

### Обязательный pre-launch чеклист (перед публичным релизом)

Зафиксирован в [DEPLOYMENT.md §8](../DEPLOYMENT.md). Перед публикацией в App Store **ОБЯЗАТЕЛЬНО**:

1. `APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS=false` (или удалить из `.env`) — CN-trust выключить.
2. `APPLE_STOREKIT_TEST_ROOT_CERTS=` — убрать DER-пины тестовых корней (ADR-013).
3. Убедиться `APPLE_STOREKIT_VERIFY_SIGNATURE=true` (инвариант ADR-013, защищён fail-fast).
4. Зачистить тестовые начисления `purchases.environment != 'Production'` перед финотчётностью
   (TD-010).

После шагов 1-2 Xcode-ветка на проде полностью выключена: принимаются только Apple-подписанные
`Production`/`Sandbox` (branch 1). Это возврат к строгому боевому поведению.

## Влияние на безопасность в бою

| Вектор | ADR-013 (пин DER) | ADR-014, флаг **OFF** (дефолт) | ADR-014, флаг **ON** (Testing) |
|---|---|---|---|
| Боевой Apple-путь (`Production`/`Sandbox`, global namespace) | строгий | **строгий (без изменений)** | **строгий (без изменений)** |
| Fail-fast `prod + verify_signature=false` | не поднимается | **не поднимается** | **не поднимается** |
| Xcode по пину DER | принят | принят | принят |
| Xcode по CN-признакам (без пина) | `untrusted_root` | **`untrusted_root`** | **принят → `Xcode` (per-user)** |
| Подделка `Xcode`-чека → бесплатные коины **себе** | закрыт | закрыт | **открыт (принято, per-user)** |
| Подделка на **чужой** аккаунт / боевой namespace | закрыт | закрыт | **закрыт** (per-user dedup) |

Ключевое: при **OFF** поведение **идентично ADR-013**. Риск появляется **только** при явном
включении флага и ограничен собственным аккаунтом атакующего.

## Контракт для backend (точные точки изменения)

Изменения минимальны и локальны; дедуп/БД/миграции/боевой путь не трогаются.

### 1. `app/config.py` — новый флаг

Добавить поле в секцию `--- App Store (StoreKit 2) ---`, после
`APPLE_STOREKIT_TEST_ROOT_CERTS`:

```python
# CN-trust Xcode тест-сертификатов (ADR-014): ГЕЙТ, дефолт false (fail-safe, прод строгий).
# true + verify_signature=true → любой self-signed EC cert с CN="StoreKit Testing in Xcode"
# (subject==issuer, срок валиден, self-signature + JWS верны) даёт environment=Xcode БЕЗ DER-пина.
# РИСК: при true на проде любой намайнит коины СЕБЕ (per-user). Только Testing-режим; перед
# публичным релизом выключить (DEPLOYMENT.md §8). Флаг легален в prod — fail-fast НЕ вешать.
APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS: bool = False
```

**Fail-fast не добавлять.** В отличие от `_forbid_storekit_bypass_in_prod`, этот флаг разрешён
при `APP_ENV=prod` осознанно (D1).

### 2. `app/main.py` — прокидывание в verifier

В конструкторе `AppleStoreKitVerifier(...)` (сейчас строки ~113-117) добавить аргумент:

```python
verifier=AppleStoreKitVerifier(
    bundle_id=settings.APPLE_STOREKIT_BUNDLE_ID or settings.APPLE_BUNDLE_ID,
    verify_signature=settings.APPLE_STOREKIT_VERIFY_SIGNATURE,
    test_root_certs_pem=settings.apple_storekit_test_root_certs,
    trust_xcode_test_certs=settings.APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS,  # ADR-014
),
```

### 3. `app/domain/providers/billing/apple.py` — CN-trust в `_verify_chain`

- `__init__`: новый параметр `trust_xcode_test_certs: bool = False` →
  `self._trust_xcode_test_certs = trust_xcode_test_certs`.
- В `_verify_chain`, в блоке резолва `forced_env` (сейчас `if root_der == self._root_der / elif
  root_der in self._test_root_ders / else raise`), вставить **третий** branch перед `else`:

```python
if root_der == self._root_der:
    forced_env = None
elif root_der in self._test_root_ders:
    forced_env = StoreKitEnvironment.xcode
elif self._trust_xcode_test_certs and _is_xcode_test_root(certs[-1]):
    forced_env = StoreKitEnvironment.xcode
    logger.warning(
        "storekit: CN-trust — self-signed Xcode test cert без DER-пина (флаг ON) → Xcode"
    )
else:
    raise WebhookPayloadInvalid(details={"reason": "untrusted_root"})
```

- Новая module-level функция + константа:

```python
_XCODE_TEST_CN = "StoreKit Testing in Xcode"

def _is_xcode_test_root(cert: x509.Certificate) -> bool:
    """Признаки self-signed Xcode StoreKit Test root (ADR-014 §D3)."""
    if cert.subject != cert.issuer:                       # self-signed (заявление)
        return False
    cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not cns or cns[0].value != _XCODE_TEST_CN:         # CN
        return False
    pub = cert.public_key()
    if not isinstance(pub, ec.EllipticCurvePublicKey):    # EC
        return False
    try:                                                  # криптографически self-signed
        pub.verify(
            cert.signature, cert.tbs_certificate_bytes,
            ec.ECDSA(cert.signature_hash_algorithm),
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
```

Импорт: `from cryptography.x509.oid import NameOID`. Срок валидности cert проверять **не нужно**
(цикл дат в начале `_verify_chain` уже проверил все `certs`, включая корень). JWS-подпись ключом
cert проверять **не нужно** отдельно — финальный `jwt.decode(..., leaf_pub_pem, ["ES256"])` для
x5c длины 1 верифицирует JWS ключом `certs[0] == certs[-1]`.

## Альтернативы (отклонены)

- **Оставить только пин DER (ADR-013 as-is).** Корректно и безопасно, но не масштабируется на
  нескольких тестеров: каждый новый сертификат/машина/перевыпуск = ручной enrollment
  ([TD-010](../100-known-tech-debt.md#td-010)). Не решает задачу владельца.
- **CN-trust без флага (всегда включён).** Открывает риск бесплатного майнинга себе на проде
  **навсегда**, в т.ч. после публичного релиза, без явного управляющего сигнала. Флаг +
  дефолт-off + pre-launch чеклист локализуют риск во времени.
- **CN-trust только при `APP_ENV != prod`.** Не решает задачу: тестеры бьют именно по **проду**.
  Привязка к `APP_ENV` сделала бы фичу бесполезной.
- **Автоматический self-service enrollment DER-пинов (таблица + admin-эндпоинт).** Более
  безопасно (пин остаётся точечным), но дороже в реализации и всё равно требует действия на
  каждого тестера. Отложено ([TD-010](../100-known-tech-debt.md#td-010) — путь закрытия). CN-trust
  — временное решение для Testing-режима, дешёвое и снимаемое одним флагом.
- **Доверять по сроку/entropy вместо CN.** CN — стабильный, документированный Apple признак Xcode
  local testing; срок уникален по машине (в этом и проблема). CN + self-signed EC — самый узкий
  устойчивый набор признаков.

## Последствия

**Плюсы:** покупки тестируются у **любого** тестера без поштучного пина DER; ops-нагрузка
[TD-010](../100-known-tech-debt.md#td-010) при включённом флаге снимается; изменение локально
(config + wiring + один предикат), боевой Apple-путь, дедуп и миграции не тронуты; риск гейтится
флагом с fail-safe дефолтом и снимается одним переключением.

**Минусы / цена:** при включённом флаге на проде — **сознательная** дыра «намайнить коины себе»
(per-user, приемлемо только в Testing-режиме); безопасность зависит от **дисциплины выключения
перед релизом** (в отличие от `verify_signature`, тут нет fail-fast — по необходимости). Митигация
— дефолт `false` + обязательный pre-launch чеклист ([DEPLOYMENT.md §8](../DEPLOYMENT.md)).

**Обязательства:** флаг `false` по умолчанию — инвариант дефолта; перед публичным релизом флаг
**обязан** быть выключен и DER-пины убраны (pre-launch чеклист); формат Xcode-признаков (CN +
self-signed EC) меняется только новым ADR.

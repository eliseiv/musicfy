# Open Questions — musicfy

Cross-cutting вопросы, требующие решения продукта/владельца. Каждый имеет ID `Q-<AREA>-N`, на
который ссылаются другие документы. Запись остаётся, пока вопрос не закрыт (решением или ADR).

| ID | Вопрос | Область | Статус |
|---|---|---|---|
| [Q-BILL-1](#q-bill-1) | Разрешаем ли Xcode-транзакции (тестовые монеты) на **проде**, или iOS тестирует StoreKit только на staging? | Биллинг | **решён: CN-trust за флагом (ADR-014)** |
| [Q-BILL-2](#q-bill-2) | Валидная Apple **Sandbox**-транзакция под боевым корнем на проде начисляет **реальные** монеты — нужно ли reject при `APP_ENV=prod`? | Биллинг | open |
| [Q-BILL-3](#q-bill-3) | `APPLE_STOREKIT_ENVIRONMENT` после ADR-013 — dead-config? Подтвердить нужность (server-to-server клиенту) или удалить. | Биллинг | open |

---

## Q-BILL-1 — Xcode-транзакции на проде: разрешаем или только staging? {#q-bill-1}

> **РЕШЕНО (2026-07-13, владелец продукта): Xcode-покупки на проде НУЖНЫ.** iOS-разработчики
> тестируют покупки против прод-бэкенда; начисления помечаются `purchases.environment='Xcode'` и
> отделимы от боевых. **Перед реальным (публичным) запуском Xcode-ветка на проде выключается.**
>
> **ОБНОВЛЕНО (2026-07-13, [ADR-014](./adr/ADR-014-storekit-cn-trust-xcode-flag.md)): пин по DER
> заменён на CN-trust за флагом.** Пин `APPLE_STOREKIT_TEST_ROOT_CERTS` (ADR-013 §D3) не
> масштабировался: у каждой машины Xcode свой уникальный self-signed сертификат (подтверждено — у
> второго тестера другой срок → другой DER → `untrusted_root`). Владелец осознанно решил доверять
> **любому** сертификату с признаками Xcode-теста (`CN="StoreKit Testing in Xcode"`, self-signed
> EC) по признакам, а не по DER — за отдельным флагом **`APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS`**
> (дефолт `false`, легален в prod, без fail-fast). Риск (per-user майнинг коинов себе) принят
> **только** на время Testing-режима; перед публичным релизом флаг выключается + пины убираются —
> обязательный pre-launch чеклист [DEPLOYMENT.md §8](./DEPLOYMENT.md). Ops-нагрузка пина и зачистки
> — [TD-010](./100-known-tech-debt.md#td-010). Криптобезопасность ветки — ADR-013 §D3 + ADR-014.

- **Контекст:** [ADR-013](./adr/ADR-013-storekit-dedup-environment-scoping.md) §D3 делает
  Xcode-ветку криптографически безопасной (пин StoreKit Test root-сертификата,
  `APPLE_STOREKIT_TEST_ROOT_CERTS`), но не отвечал на продуктовый вопрос: **нужно ли** вообще
  начислять реальные монеты в боевом кошельке по тестовым транзакциям.
- **Решение:** да, разрешаем Xcode-покупки на проде для отладки. С [ADR-014](./adr/ADR-014-storekit-cn-trust-xcode-flag.md)
  доверие даётся по признакам сертификата за флагом `APPLE_STOREKIT_TRUST_XCODE_TEST_CERTS`
  (дефолт `false` = ветка выключена), а не поштучным пином DER. Включение — явный аудируемый акт;
  флаг выключается перед публичным релизом (pre-launch чеклист DEPLOYMENT.md §8). DER-пин
  `APPLE_STOREKIT_TEST_ROOT_CERTS` остаётся легальным как более узкая (точечная) альтернатива.
  Регламент зачистки тестовых начислений (фильтр `purchases.environment != 'Production'` +
  компенсирующие ledger-записи перед финансовой отчётностью) — [TD-010](./100-known-tech-debt.md#td-010).

## Q-BILL-2 — Sandbox-транзакция под боевым корнем на проде начисляет реальные монеты? {#q-bill-2}

- **Контекст:** [ADR-013](./adr/ADR-013-storekit-dedup-environment-scoping.md) §D1 доверяет
  claim `environment` из верифицированного payload. Payload, подписанный **боевым** Apple Root CA - G3
  с `environment='Sandbox'`, проходит доверие и начисляет **реальные** монеты в боевом кошельке
  (`purchases.environment='Sandbox'`, глобальный namespace). Xcode-ветка гейтится пином, а
  Sandbox — нет: любая валидная Apple-Sandbox-транзакция на проде даёт реальный грант.
- **Вопрос:** нужно ли при `APP_ENV=prod` **отклонять** (`rejected`/`ignored`) транзакции с
  `environment != 'Production'`, чтобы боевой кошелёк начислялся только боевыми покупками?
- **Находка backend-reviewer** к ADR-013 (approve); требует отдельного продуктового/security
  решения и, при «да», — **отдельного ADR** (меняет контракт применения: сейчас Sandbox на проде
  принимается). Пока открыт — поведение по ADR-013 (Sandbox на проде начисляет).
- **Что нужно для закрытия:** решение владельца + ADR, если политика меняется.

## Q-BILL-3 — `APPLE_STOREKIT_ENVIRONMENT` — dead-config после ADR-013? {#q-bill-3}

- **Контекст:** после [ADR-013](./adr/ADR-013-storekit-dedup-environment-scoping.md) окружение
  транзакции берётся из **верифицированного payload** (claim `environment`), а не из настройки
  сервера. `APPLE_STOREKIT_ENVIRONMENT` (`config.py:98`, `Literal["Sandbox","Production"]`)
  определена, но по grep нигде не читается — кандидат в мёртвую конфигурацию.
- **Вопрос:** нужна ли она App Store **Server API** клиенту (server-to-server: выбор
  base URL Sandbox/Production для запросов истории транзакций/нотификаций), или это остаток
  до-ADR-013 логики, который надо удалить?
- **Находка backend-reviewer** к ADR-013. Требует подтверждения от backend (проверить, использует
  ли server-to-server клиент эту настройку для выбора endpoint Apple). Зафиксировано также как
  [TD-011](./100-known-tech-debt.md#td-011).
- **Что нужно для закрытия:** backend подтверждает нужность → закрыть как «используется»; иначе —
  удалить переменную (правка кода + `.env.example`).

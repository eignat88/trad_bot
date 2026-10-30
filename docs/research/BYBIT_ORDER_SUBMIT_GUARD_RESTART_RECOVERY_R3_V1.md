# BYBIT_ORDER_SUBMIT_GUARD_RESTART_RECOVERY_R3_V1 — восстановление после
# аварийного завершения (Review #5710398920, Issue #229, PR #233)

**Статус:** `R3_FIX_PREPARED / READY_FOR_INDEPENDENT_REVIEW` — локальные
изменения в рабочем дереве; commit/push выполняет оператор отдельно.
Merge/deploy/activation НЕ выполнялись. `MERGE_READY` / `DEPLOY_READY` /
`PRODUCTION_SAFE` НЕ объявляются (требуется независимая проверка и
отдельный operator GO).
**Ветка:** `research/issue229-retry-budget-hardening-v1`.
**Baseline HEAD:** `8daa96f30aa31e6e244788628fbe332a0ccd9a18`.
**Режим:** LOCAL IMPLEMENTATION / REVIEW REMEDIATION R3. Никаких реальных
запросов Bybit и торговых операций — только fake transport.

---

## 1. Дефект (Code Review #5710398920)

В `OrderSubmitGuard.configure()` выполнялся `_read_journal(convert_in_flight=True)`:
записи `in_flight` переводились в `unknown` **только в памяти**. Следующая
же транзакция вызывала `_reload()` → `_read_journal(convert_in_flight=False)`,
и запись снова читалась с состоянием `in_flight` из файла. Дополнительно
`pending_unknown()` выбирает только записи `unknown`, поэтому запись
умершего процесса:

* продолжала блокировать повторную отправку (fail-closed — это правильно),
* но **не попадала** в штатный механизм `reconcile_order_submissions()`,
* и восстановление не было durable — контракт recovery/reconciliation
  нарушался при каждом перезапуске.

## 2. Модель ownership / liveness (без TTL и без эвристик по возрасту)

Владелец записи — guard-инстанд, создавший резервацию. Каждая запись журнала
теперь несёт audit-блок `owner`:

```json
"owner": {"owner_id": "<uuid4 hex>", "pid": 1234, "witness": "<journal-basename>.owner.<owner_id>.lock", "started_at": "<UTC ISO>"}
```

Liveness-вердикт выносится **только** по OS-локу witness-файла:

| Вердикт | Условие | Следствие |
|---|---|---|
| `alive` | witness-лок **удерживается** живым владельцем (или владелец зарегистрирован в текущем процессе) | запись `in_flight` НЕ трогается; `OWNER_ALIVE_BLOCKER` |
| `orphaned` | witness-файл существует, его OS-лок **свободен** (ОС сняла блокировку при смерти процесса/владельца) | durable-переход `in_flight → unknown` (блок сохраняется) |
| `unverified` | witness-файл отсутствует/недоступен, метаданные владельца отсутствуют или некорректны | запись остаётся блокирующей; `OWNER_UNVERIFIED_BLOCKER` |

Почему это защищено от повторного использования PID:

* witness-лок удерживается **OS-объектом** (byte-range lock на файле:
  Windows `msvcrt.locking`, POSIX `fcntl.flock`), а не парой «PID + время»;
  ОС снимает блокировку при смерти процесса/закрытии handle, и **никакой
  переиспользованный PID не может повторно удержать снятый лок**;
* PID в записи — только forensic-поле, в решениях не участвует;
* `msvcrt.locking`/`flock` отклоняют захват диапазона, удерживаемого другим
  handle — даже в том же процессе (проверено эмпирически), плюс реестр
  живых witness текущего процесса (`weakref`) исключает самопроклейку;
* **запрещено** считать запись orphaned по возрасту/TTL — только вердикт
  OS-лока; при недостоверном вердикте блок сохраняется.

Witness-файлы **никогда не удаляются** (unlink сломал бы взаимное исключение
и детекцию), создаются до появления записи владельца в журнале, освобождаются
при уничтожении guard-инстанса (`reset()` / GC / смерть процесса). В
production guard — process-lifetime синглтон, т.е. liveness guard-инстанса
эквивалентна liveness процесса.

## 3. Атомарное восстановление (durable, идемпотентное)

Любое изменение журнала — общая межпроцессная транзакция
(отдельный `<journal>.lock` файл, схема 2):

`LOCK → RELOAD → VALIDATE → RECOVERY TRANSITION → DURABLE WRITE → UNLOCK`

* шаг **RECOVERY TRANSITION** выполняется в **каждой** транзакции
  (`begin`/`mark_unknown`/`mark_resolved`/`resolve_reconciled`/
  `recover_orphaned_in_flight`), а не только в памяти при загрузке;
* `configure()` запускает полную recovery-транзакцию при привязке пути —
  восстановленное состояние сразу durable и переживает дальнейшие рестарты;
* переход: `in_flight → unknown` c reason «outcome UNKNOWN» (если reason
  пуст) и audit-блоком
  `recovery: {"from_state": "in_flight", "to_state": "unknown", "ts_utc": ..., "detail": "owner not alive (witness lock released) ..."}`
  — `orderLinkId`, `fingerprint`, `owner`, `ts_utc` и все audit-поля
  сохраняются, checksum пересчитывается;
* **не происходит** ничего из: автоматического `resolved`, снятия блокировки,
  перезаписи чужих записей, повторной записи восстановленных `unknown`
  (переход идемпотентен: `unknown` не трогается повторно);
* durable write — только при фактическом изменении (recovery без находок —
  чистое чтение, ничего не создаёт и не переписывает);
* fail-closed: ошибка lock / checksum / durable write → ни восстановления,
  ни отправки (0 HTTP attempts); отчёт о recovery при недолговечной записи
  не публикуется;
* если ключ записи уже занят — предыдущая запись сохраняется в `history`
  (никакая запись не теряется и не перезаписывается молча).

## 4. Reconciliation (контракт сохранён + явные блокеры владельца)

`BybitClient.reconcile_order_submissions()`:

1. сначала выполняет durable recovery (`guard.recover_orphaned_in_flight()`);
2. для записей `in_flight` возвращает **явные** вердикты владельца:
   * `OWNER_UNVERIFIED_BLOCKER` — liveness владельца недостоверен: блок
     сохраняется, без recovery-перехода, без POST (fail-closed);
   * `OWNER_ALIVE_BLOCKER` — владелец жив: операция **не** объявлена
     orphaned, состояние не тронуто, повторный POST запрещён;
3. для каждой записи `unknown` (включая восстановленные) — неизменные
   правила строгой идентичности: точный непустой `orderLinkId` + ожидаемый
   symbol + категория `linear` + поддерживаемый `orderStatus`
   (`New/PartiallyFilled/Filled/Cancelled/Rejected`); malformed ответ →
   BLOCKED; сбой API → BLOCKED; не найдено → `UNRESOLVED_BLOCKER`;
   подтверждение снимает блок **только** после достоверного exchange
   confirmation с сохранением проверяемого evidence в журнале.

Не используются как основание для разблокировки: TTL, произвольный текст
оператора (`MANUAL_RESOLUTION_NOT_IMPLEMENTED` сохранён), отсутствие ордера
в ответе API.

## 5. Тесты (fake transport; A–G обязательны)

| Тест | Где | Проверяет | Ожидание |
|---|---|---|---|
| A crash before POST | unit + multiproc | резерв `in_flight`, crash до POST, рестарт B: без повторного POST, операция видна reconciliation, состояние переживает рестарт | 0 duplicate POST / recovery visible / journal valid |
| B crash after POST, before response | unit + multiproc | crash после отправки POST; повторный POST заблокирован; reconciliation по точному `orderLinkId` подтверждает исходный ордер | 1 initial POST, 0 repeat POST; `resolved` + audit evidence |
| C owner still alive | unit + multiproc | живой владелец с заблокированным POST; чужой recovery/reconciliation | не orphaned, повторный POST запрещён, состояние владельца не тронуто |
| D owner cannot be verified | unit | witness недоступен / запись без `owner` | `OWNER_UNVERIFIED_BLOCKER`, без auto-`resolved`, без POST |
| E recovery idempotency | unit | многократные рестарты и повторный recovery одной операции | нет дубликатов, история корректна, checksum цел, блок стабилен |
| F journal failure | unit + multiproc | lock failure, invalid checksum, malformed journal, durable write failure, crash во время recovery-перехода (до/после записи) | fail-closed, 0 новых POST, журнал не повреждён |
| G concurrent recovery | multiproc | два процесса одновременно выполняют recovery/reconciliation | нет lost updates, порчи, повторного POST, ошибочной разблокировки; каждый переход ровно один раз |

Состав файлов: `tests/test_bybit_order_submit_guard_v1.py`,
`tests/test_bybit_order_submit_guard_multiproc_v1.py` (новые файлы не
добавлялись — CI-workflow не требует изменения состава).

## 6. Неприкосновенные контракты (сохранены)

`HARD_MAX_ATTEMPTS ≤ 3`; запрет автоматического повторного POST; telemetry
parity; breaker/storm guard; HTF/OOS causality; frozen experiment
definitions; trading gate / scoring / signal generation; production DB, VPS,
systemd. Сохранены: `MANUAL_RESOLUTION_NOT_IMPLEMENTED`, `API_BUDGET_NOT_CLEARED`,
`V2_NO_GO`, PR #233 Draft.

## 7. Rollback

Изменения ограничены `app/exchange/retry_budget.py`,
`app/exchange/bybit_client.py` (только `reconcile_order_submissions`),
тестами и документацией. Журнал schema-2 совместим в обе стороны: новые
поля (`owner`, `recovery`, `history`) опциональны и строго валидируются;
старый код читает журнал как раньше (лишние поля игнорируются), записи без
`owner` остаются fail-closed блокирующими. При откате guard'а witness-файлы
можно оставить (это lock-файлы без данных).

# BYBIT_RETRY_BUDGET_HARDENING_V1 — реализация (Issue #229)

**Статус:** LOCAL CHANGES READY — регрессия локальная; merge/deploy/activation НЕ выполнялись.
**Ветка:** `research/issue229-retry-budget-hardening-v1` (база `ad73530`, #228 telemetry).
**Реестры/гейты:** `API_BUDGET_NOT_CLEARED`, `V2_NO_GO` сохранены; PR #227 остаётся Draft
(`DO NOT MERGE`); Draft PR #232 не изменялся. Frozen checkout / VPS / production / DB /
systemd / timers / OOS registry не тронуты.

---

## 1. Цель (из Issue #229)

Убрать вложенные ретраи (худший случай до **18 HTTP-попыток на логический запрос** через
L3 × (L1+L2)) и обеспечить **единый бюджет ≤ 3 HTTP-попыток на логическую операцию**,
explicit error classification, exponential backoff + jitter, breaker-aware retry gating и
retry-storm protection.

## 2. Архитектура

### 2.1 Новый модуль `app/exchange/retry_budget.py`

Губернанс-примитивы, общие для всех Bybit-потребителей:

| Примитив | Назначение |
|---|---|
| `HARD_MAX_ATTEMPTS = 3` | Жёсткий бюджет: ≤3 HTTP-попыток на логический запрос (`bybit_max_attempts` может только уменьшить) |
| `classify_api_result()` | Классификация: `RATE_LIMIT` (10006 / «too many visits» / «exceeded the api rate limit»), `ACCESS_DENIED` («access too frequent», приоритет над RL), `API_ERROR` (прочие retCode), `OK` |
| `RL_RETRY_CAPS` | Per-class RL retry caps: P0 3 / P1 2 / P2 2 / P3 1 (plan §4.4), усечённые бюджетом и `bybit_rate_limit_max_retries` |
| `BackoffSchedule` | Экспоненциальный backoff, bounded, non-decreasing; jitter (до 50%) на RL-классе |
| `RateLimitBreaker` | Process-local breaker с cooldown: P3 — defer цикла, P0/P1/P2 — bounded wait («P0 retries only after cooldown») |
| `RetryStormGuard` | Глобальная доля ретраев на скользящее окно: абсолютный кап + доля от попыток (после `min_sample`), `allow_retry()` → fail-closed |
| `OrderSubmitGuard` | Защита POST `/v5/order/create` от повторной отправки (double-submit) |

### 2.2 Единый state machine `BybitClient._public_get`

Один цикл попыток вместо L1 (timeout, 3 × linear) × L2 (rate-limit, 5 × exponential):

1. classification попытки: `TIMEOUT` / `RATE_LIMIT` — единственные retryable-классы;
2. fail-closed без ретраев: non-RL retCode, `ACCESS_DENIED` / HTTP 403, прочие HTTP-ошибки,
   битый payload, необработанные transport-исключения (идентичный exception-object
   пробрасывается наружу — контракт #228);
3. HTTP 429 = rate limit (retryable в капах), HTTP 403 = fail-closed;
4. ретрай-решение в одной точке: бюджет (`≤3`) + per-class RL cap + storm guard + deadline;
5. backoff: экспоненциальный, bounded (`30s`), non-decreasing (floor по последнему delay);
   jitter — на RL-классе (десинхронизация троттл-бёрстов);
6. breaker: после RL-события открывается cooldown (`bybit_rate_limit_cooldown_s`);
   перед каждой попыткой P3 defer'ится (fail-closed), P0/P1/P2 ждут (bounded,
   `bybit_breaker_max_wait_s`), при непокрытом cooldown — fail-closed;
7. deadline (`bybit_retry_deadline_s`) — fail-closed на ретрае за пределами дедлайна.

**Решение по backoff-расписанию (AC3 vs AC6):** старый linear `base*attempt` и экспоненциальный
`base*2^(n-1)` совпадают в пределах ≤3 попыток (не более двух ретраев: `[b, 2b]`), поэтому
замороженный контракт регрессии `tests/test_scanner_runtime.py` (sleeps `[1, 2]`, логи
«Bybit timeout, retry N/3») сохранён бит-в-бит, а расписание формально экспоненциальное.
Jitter не применяется к timeout-классу именно из-за этого контракта (тест трактует sleeps
как точные значения); цель jitter — десинхронизация RL-ретраев в троттл-окнах.

**Решение по per-class caps (plan §4.4 «P0 3 / P1 2 / P2 2 / P3 1»):** числа читаются как
RL retry caps (дословно «Per-class RL retry caps»), всегда усечённые жёстким бюджетом ≤3
(AC1 — «total HTTP attempts per logical request ≤ 3» — главный инвариант: P0-номинал «3»
обрезается до 2 ретраев в рамках одного запроса). Timeout-ретраи управляются тем же общим
бюджетом (`bybit_max_attempts`, дефолт 3 — регрессия scanner сохранена). Приоритет
выводится из endpoint (`/v5/order`,`/v5/position` → P0; kline/tickers/time → P1; прочий
market → P2) и может быть явно передан как `retry_priority=` (HTF source и candle_sync
передают `"P3"`).

### 2.3 Удалённые независимые retry-слои (L3 / hidden nesting)

| Слой | Было (худший случай) | Стало |
|---|---|---|
| `app/shadow/runner.py` `_scan_symbol_with_retry` | 3 × 18 = 54 попыток | удалён; `_scan_symbol_fail_closed` — одна логическая операция, RL-дроп считается (`rate_limit_drops`) и никогда не ретраится |
| `app/shadow/v2d_runner.py` `_scan_symbol_with_retry` | 3 × 18 = 54 | аналогично |
| HTF candle source `_fetch_server_time_ms` / `_fetch_page` | 3 × 18 = 54 | одна логическая операция на fetch; fail-closed карантин `REASON_RATE_LIMITED` / `REASON_API_ERROR` сохранён дословно; gap-recovery (полнота данных, не ретраи ошибок) оставлен и ограничен `gap_recovery_attempts` |
| `app/analytics/candle_sync.py` `_fetch_candles_batch` | 3 × 18 = 54 (не был в списке code points Issue #229, но покрыт AC1 «no L3×L2 nesting anywhere») | одна логическая операция; дропы считаются в `failed_ranges` (никогда не молча) |
| `isolated_oos.py` | per-symbol fail-and-continue | без изменений — наследует бюджет клиента (подтверждено static-контрактом) |

### 2.4 Защита POST `create_order` (AC «Приоритет 5»)

* одна HTTP-попытка, **никогда** не ретраится (static-контракт: в источнике нет циклов);
* `OrderSubmitGuard`: идентичная повторная отправка блокируется fail-closed
  (`BybitOrderUncertainError`), если предыдущая идентичная отправка имеет
  **неопределённый исход** (timeout / transport / 5xx / битый payload — ордер может
  существовать на сервере); окно блокировки `bybit_order_resubmit_block_s`;
* дефинитивные исходы (API-отклонение, 4xx) снимают блокировку: повторная отправка
  отклонённого ордера — новое решение вызывающего, а не слепой ретрай;
* защита по fingerprint `(symbol, side, qty, order_type)`; in-flight блокировка.

### 2.5 Telemetry parity (BYBIT_API_TELEMETRY_V1, #228)

* ровно **одна запись `BYBIT_API_CALL` на HTTP-попытку** — во всех путях (OK / TIMEOUT /
  RATE_LIMIT / ERROR), включая новые (HTTP 429 → `result=RATE_LIMIT`);
* `logical_request_id` общий для всех попыток логического запроса, `attempt_index`
  строго возрастает, `retry_reason ∈ {none, timeout, rate_limit}`;
* `total_attempts` теперь равен объединённому бюджету (`3`) — семантика поля из #228
  («max HTTP attempts the logical request may make») теперь честная;
* безопасность секретов не изменена: в записи попадают только allowlist-скаляры.

### 2.6 Настройки (совместимость отката)

Существующие имена/дефолты `bybit_*` сохранены (config-compatible revert). Добавлены:

| Поле | Дефолт | Назначение |
|---|---|---|
| `bybit_rate_limit_cooldown_s` | 2.0 | cooldown процессного breaker после RL-события |
| `bybit_breaker_max_wait_s` | 30.0 | ограниченный ожидаемый cooldown (иначе fail-closed) |
| `bybit_retry_deadline_s` | 60.0 | fail-closed дедлайн логического запроса |
| `bybit_retry_storm_window_s` | 60.0 | окно storm guard |
| `bybit_retry_storm_max_retries` | 30 | абсолютный кап ретраев на окно |
| `bybit_retry_storm_max_share` | 0.5 | доля ретраев на окно |
| `bybit_order_resubmit_block_s` | 60.0 | окно блокировки повторной отправки ордера |

## 3. Изменения в тестах (санкционированы test plan Issue #229)

| Тест | Изменение | Основание |
|---|---|---|
| `test_bybit_api_telemetry_v1.py::test_one_log_line_per_http_attempt_success` | `total_attempts` «9» → «3» | семантика бюджета — ядро #229 |
| `test_shadow_cli.py::test_runner_has_rate_limit_retry` | заменён на `test_runner_has_no_independent_retry_loop` (L3 gone) | test plan #229: «Shadow static contract tests (L3 gone / budgeted)» |
| `test_shadow_cli.py::test_run_cycle_returns_rate_limit_retries` | → `test_run_cycle_reports_rate_limit_drops` (честное имя счётчика дропов) | L3-ретраи удалены; дропы должны считаться |
| `test_shadow_cli.py::test_runner_main_once` | ключ summary `rate_limit_retries` → `rate_limit_drops` | то же |
| `test_htf_..._bybit_candle_source_v1.py::test_api_error_fails_closed_with_bounded_attempts` | → `..._with_single_logical_request` (`client.calls == 1`) | hidden retry layer коллапсирован (AC1) |

`test_rate_limit_fails_closed_and_creates_nothing`, `test_bybit_timeout_retries_with_linear_backoff`,
`test_bybit_timeout_raises_concise_error_after_last_attempt` и весь набор #228-телеметрии
остались **без изменений и зелёными**.

## 4. Новый тестовый модуль `tests/test_bybit_retry_budget_v1.py`

* property-тест: для **любой** последовательности из 9 классов ошибок × длины 1–3
  (819 случаев) — ≤3 HTTP-попыток; fail-closed первый исход → ровно 1 попытка;
* classification matrix: 10006 → retryable; иной retCode → без ретрая; 403 и
  «access too frequent» → fail-closed (ACCESS_DENIED приоритетнее 10006); 429 → RL;
* backoff: timeout-расписание `[1, 2]` бит-в-бит; RL — exponential+jitter, bounded,
  non-decreasing; смешанная последовательность — non-decreasing;
* storm guard: абсолютный кап, доля на окно, истечение окна, интеграция с Settings;
* breaker: P3 defer без HTTP; P1/P2 bounded wait; fail-closed при слишком долгом
  cooldown; «P0 retries only after cooldown»;
* telemetry parity: `attempt_index`/`retry_reason`/`total_attempts`/`logical_request_id`;
* create_order: одна попытка, запрет повторной отправки после неопределённого исхода,
  разрешение после дефинитивного отклонения, per-fingerprint, истечение окна;
* static contracts: `bybit_client`, shadow runners, HTF source, candle_sync,
  `isolated_oos` — независимых вложенных retry-циклов нет нигде.

## 5. Регрессия (локально, Windows; Python 3.13.14, pytest 9.1.1)

### 5.1 Итоговый статус: **PASS (no regression)**

| Набор | Результат |
|---|---|
| `tests/test_bybit_retry_budget_v1.py` (новый, 36 тестов) | **36 passed** |
| Целевой регресс: scanner, shadow, HTF (candle source / runner / backfill / dry_run / close_location), telemetry #228, candle_sync, candle_pit_safety, fvg_filter_oos, exhaustion_shadow, core, live_gate, prospective_oos_v3 | **399 passed / 7 failed** — те же 7 pre-existing падений `test_prospective_oos_v3.py` (БД/registry), что и на baseline до изменений |
| Полный прогон (355 файлов, минус 4 pre-existing битых модуля) | **5211 passed / 79 skipped / 81 failed / 53 errors** |
| Атрибуция: 25 файлов с падениями прогнаны на чистой baseline-копии `ad73530` | **81 failed / 53 errors / 128 unique — множества падений ИДЕНТИЧНЫ** (`Compare-Object`: 0 новых, 0 исправленных) → **регрессий нет** |
| Ключевой набор после всех манипуляций с git | 198 passed |

### 5.2 Pre-existing (не связаны с изменениями; не тронуты)

* 4 модуля не собираются на Windows ещё до изменений: `test_me_r_v3_isolated_oos_collector.py`,
  `test_me_r_v3_isolated_pg_collector_issue154.py`, `test_me_r_v3_atomic_pg_e2e_issue154.py`
  (`import pwd` — Unix-only), `test_srr_short_r_expansion_candle_path_integrity_audit_v1.py`
  (отсутствует модуль `tools.research.…`);
* 128 падений/ошибок в 25 файлах (Postgres недоступен локально, registry/state-зависимые
  тесты, исторические fixtures) — идентичны на baseline `ad73530`;
* `test_prospective_oos_v3.py` (7 падений, включая реестр `READY_TO_START` vs `CANCELLED`) —
  воспроизводится на baseline; ожидает Postgres/VPS-окружения.

### 5.3 Метод атрибуции

Из-за параллельных git-сессий в общем `.git` (index.lock) baseline строился не через
`git stash`, а как копия рабочего дерева с восстановлением 10 изменённых файлов из
`git show ad73530:<path>` и удалением новых файлов; наборы `FAILED`/`ERROR` сравнивались
попарно (`Compare-Object`) — совпадение полное.

## 6. Rollback

Config-compatible revert (имена/дефолты env сохранены); release revert для
`/opt/trad_bot`. No fingerprint change. После исправлений ревью #5709376849 появилось
персистентное состояние — журнал отправки ордеров (JSON-файл
`bybit_order_submit_journal_path`): при rollback файл можно оставить (он лишь хранит
audit-записи и блокировки fail-closed; безопаснее удалить только вместе с полным
откатом изменений guard'а).

## 7. Hold gates (сохранены)

`API_BUDGET_NOT_CLEARED`, `V2_NO_GO`, PR #227 Draft (`DO NOT MERGE`), PR #232 Draft — не
объявлять готовым до прохождения регрессии; deploy — только operator GO (вне этой работы).

---

# Ревью #5709376849 (PR #233) — исправления

**База исправлений:** `268344b`; ветка `research/issue229-retry-budget-hardening-v1`.

## R1 (P1) — Неопределённый результат create_order: таймер больше не снимает блокировку

Состояние отправки ордера теперь ведёт **персистентный журнал**
(`bybit_order_submit_journal_path`, по умолчанию
`var/reports/bybit_order_submit_journal_v1.json`, схема 1; записи ключуются парой
fingerprint + orderLinkId — audit trail не перезаписывается):

| Состояние | Переходы | Снятие блокировки |
|---|---|---|
| `in_flight` | ставится ПЕРЕД отправкой POST | только фактическим завершением операции (никакого TTL) |
| `unknown` | timeout / transport / 5xx / битое тело ответа — исход НЕОПРЕДЕЛЁН | **только** явным reconciliation (подтверждение состояния на бирже) или документированным операторским разрешением |
| `resolved` | дефинитивный исход (parsed retCode, HTTP 4xx) либо reconciliation с доказательством | не блокирует; запись сохраняется как audit trail |

* легаси-настройка `bybit_order_resubmit_block_s` сохранена для rollback-совместимости,
  но **больше ничего не разблокирует**;
* неразрешимый журнал (не читается / не пишется) = fail-closed: отправка ордера
  блокируется (`BybitOrderUncertainError`), а не молча теряет защиту.

**Безопасная идентичность операции:** каждая отправка несёт уникальный клиентский
`orderLinkId` (`rb<uuid32>` либо явный параметр `create_order(..., order_link_id=...)`),
который уходит в подписанное тело POST и фиксируется в журнале ДО отправки.

**Восстановление после рестарта:** журнал загружается при старте; запись `in_flight`,
оставшаяся от упавшего процесса, перечитывается как `unknown` (fail-closed).

## R2 (P1) — Конкурентные POST

`in_flight` сохраняется до фактического завершения операции независимо от какого-либо
TTL; идентичный повторный вызов во время летящего HTTP-запроса отказывает
(`BybitOrderUncertainError`, `state=in_flight`) и не порождает второго POST. Тест с
двумя одновременными вызовами и заблокированным первым HTTP-запросом
(`test_concurrent_identical_post_blocked_while_in_flight`) подтверждает: до завершения
первого запроса сеть покинул ровно один POST.

## R3 (P1) — Явная процедура reconciliation

Никакой таймер не подменяет проверку состояния ордера:

* `BybitClient.get_order_by_link_id(symbol, order_link_id)` — read-only
  `GET /v5/order/realtime` + `GET /v5/order/history` (по одному бюджетированному
  логическому запросу на endpoint);
* `BybitClient.reconcile_order_submissions()` — для каждой записи `unknown` журнала:
  найден дефинитивный ответ биржи (open/filled/cancelled/rejected) → блок снимается с
  доказательством в журнале (`exchange-confirmed orderStatus=...`); записи НЕТ →
  **явный `UNRESOLVED BLOCKER`**, блокировка остаётся (fail-closed), требуется
  подтверждение оператора; сбой запроса reconciliation также оставляет блок;
* CLI `tools/bybit_order_submit_reconcile_v1.py`:
  * `--list` — офлайн-список блокирующих записей;
  * `--reconcile` — процедура выше (exit code 2 при любом UNRESOLVED BLOCKER);
  * `--mark-resolved <orderLinkId> --confirm-manual "<доказательство>"` —
    документированное операторское разрешение; `--confirm-manual` **обязателен**,
    доказательство сохраняется в журнале.

## R4 (P2) — Retry deadline

* deadline перепроверяется **после каждого блокирующего ожидания** (breaker-gate,
  limiter, backoff-сон) **перед** HTTP-отправкой; истёкший deadline → fail-closed
  без попытки (0 HTTP-запросов);
* сетевой timeout попытки ограничен оставшимся временем операции:
  `min(bybit_timeout, deadline_at - now)` (при комфортном deadline значение timeout
  не меняется — замороженные контракты сохранены);
* тесты на управляемых часах (`FakeClock`): `test_deadline_rechecked_after_breaker_wait`,
  `test_deadline_rechecked_after_backoff_sleep`,
  `test_network_timeout_clamped_to_remaining_deadline`,
  `test_network_timeout_unchanged_when_deadline_comfortable`.

## Тесты (fake transport, без реальных ордеров и запросов к Bybit)

Новый модуль `tests/test_bybit_order_submit_guard_v1.py` (11 тестов): конкурентные POST,
in_flight без TTL, unknown без TTL, orderLinkId в теле POST и в журнале, restart
recovery, crash-mid-send → unknown, reconciliation (found / not-found / query failure),
CLI (--list, обязательный --confirm-manual). Обновлён
`tests/test_bybit_retry_budget_v1.py`: TTL-тест заменён на
`test_create_order_unknown_state_never_timer_released` (санкционировано P1: старая
семантика авто-снятия через 60 c прямо противоречила замечанию — тест фиксировал баг),
добавлена секция deadline с FakeClock (4 теста). Итог: 51 passed по двум модулям.

Сохранены и зелёные: HARD_MAX_ATTEMPTS ≤ 3 (property 819 последовательностей),
telemetry parity (suite #228), отсутствие nested retries (static contracts), frozen
`test_scanner_runtime` (sleeps `[1, 2]`, логи `retry N/3`), торговая логика, OOS/registry
не тронуты, PR #233 Draft, `API_BUDGET_NOT_CLEARED` / `V2_NO_GO`.

## Регрессия round-2 (локально, Windows)

| Набор | Результат |
|---|---|
| `test_bybit_order_submit_guard_v1.py` + `test_bybit_retry_budget_v1.py` | **51 passed** |
| CI-набор (7 файлов) | **213 passed** |
| Затронутая регрессия (16 файлов: scanner/shadow/HTF/candle/core/live_gate/analysis/paper) | 309 passed / 7 failed — ровно pre-existing падения `test_analysis_runner` (6) + `test_paper_runner` (1), атрибутированы baseline-сравнением ранее |
| Полный прогон (минус 4 pre-existing битых модуля) | **5226 passed / 79 skipped / 81 failed / 53 errors** |
| Baseline comparison vs `268344b` (25 файлов с падениями прогнаны на копии HEAD) | **128 unique == 128 unique, `Compare-Object` пуст** → **регрессий нет** |

---

# Ревью #5709889450 (PR #233) — исправления R2

**База:** `d8fbfd2`; ветка `research/issue229-retry-budget-hardening-v1`.
Принцип: **ни один POST `/v5/order/create` не отправляется до гарантированного
durable-сохранения идентичности операции и состояния `in_flight` в журнале**.

## R2-P1 — Межпроцессная синхронизация журнала

* Межпроцессная блокировка на **отдельном lock-файле** (`<journal>.lock`),
  совместимая с Windows (`msvcrt.locking`, byte-range) и Linux
  (`fcntl.flock`); lock-файл **никогда не удаляется** (unlink сломал бы
  взаимное исключение на POSIX); OS-лок снимается автоматически при смерти
  процесса.
* Каждый переход выполняется атомарно как
  **`LOCK → RELOAD → VALIDATE → RESERVE → DURABLE WRITE → UNLOCK`**;
  HTTP POST разрешён только после подтверждённой durable-записи. Переходы
  `in_flight → unknown/resolved` используют ту же транзакцию (перезагрузка
  актуального состояния перед мутацией — lost updates исключены).
* Защищено: lost updates, перезапись записей другого процесса, два
  одновременных POST одного fingerprint, повторное использование активного
  `orderLinkId`, разблокировка при повреждённом/недоступном журнале.
* Lock/journal failure → **0 HTTP attempts** (fail-closed,
  `BybitOrderUncertainError`). Несколько конфигураций на одном journal path
  сериализуются тем же lock-файлом (покрыто тестом двух guard-инстансов и
  мультипроцессными тестами).
* Crash/restart: незавершённый `in_flight` перечитывается при старте как
  `unknown` и остаётся блокирующим.

## R2-P1 — Строгий reconciliation (`orderLinkId`)

Условие `recorded in ("", orderLinkId)` удалено. Принимается **только точное
непустое совпадение** `orderLinkId` **плюс** ожидаемый `symbol` **плюс**
категория `linear`; пустое/отсутствующее/иное значение — не подтверждение.
Посторонние записи не используются. Некорректная структура ответа —
fail-closed (`BybitError` → остаётся `UNRESOLVED_BLOCKER`). Если realtime не
подтвердил операцию — проверяется history; без достоверного подтверждения из
обоих источников блок сохраняется. Неизвестный `orderStatus` — BLOCKED до
определения допустимого статуса (допустимые:
`New/PartiallyFilled/Filled/Cancelled/Rejected` — любой известный статус
доказывает исход операции). Параметризованные тесты покрывают всю таблицу
ревью (точное совпадение, пустой/отсутствующий/иной orderLinkId, иной
symbol, иная категория, неизвестный/отсутствующий статус, только посторонние
записи, malformed, API error/timeout, not found, подтверждение из history).

## R2-P2 — Durable journal persistence

Порядок записи: уникальный temporary-файл в каталоге журнала
(`.tmp.<pid>.<uuid>`, `O_EXCL`) → контролируемые права (файл `0600`, каталог
`0700` на POSIX) → сериализация → `flush()` → `os.fsync()` → атомарный
`os.replace()` → `fsync()` каталога (POSIX). **Windows**: directory-fsync
недоступен — формальная crash-durability гарантия НЕ заявляется; остаются
атомарная замена namespace + file-level fsync (задокументировано).

Чтение валидирует: schema (только v2), структуру, допустимые состояния,
обязательные поля и **checksum** (sha256 канонического JSON записей). Любая
инвалидность → fail-closed; повреждённый журнал **никогда не превращается в
пустой автоматически** (авто-восстановление запрещено; при load-error запись
в журнал также блокируется — повреждённый файл не может быть затёрт).

## R2-P2 — Безопасное ручное разрешение

Сценарий A (автоматический): только строгая проверка данных Bybit
(точный orderLinkId + symbol + категория + принятый статус) снимает блок и
сохраняет **проверяемое подтверждение** в журнале
(`orderLinkId=… symbol=… orderStatus=… orderId=… verified_at=…`).

Сценарий B (ручной): **`MANUAL_RESOLUTION_NOT_IMPLEMENTED`** — блок
сохраняется. Ручное снятие требует верифицированной авторизации оператора и
проверяемого подтверждения (operator identity, reference, orderLinkId,
symbol, результат проверки статуса, timestamp UTC, причина); надёжного
механизма авторизации нет, поэтому CLI `--manual-resolution` только печатает
запись с реквизитами и возвращает exit 3, **не изменяя журнал**. Фиктивная
система авторизации не вводится; по умолчанию ручное снятие запрещено.
Старый `--mark-resolved --confirm-manual` удалён (произвольная строка не
является доказательством).

## Отчёт по сохранности и целостности журнала

| Свойство | Механизм | Проверено |
|---|---|---|
| Межпроцессное взаимное исключение | отдельный lock-файл (msvcrt/fcntl), никогда не удаляется | multiproc: 1 POST из 2 процессов; lock-held → 0 attempts |
| Атомарность обновлений | LOCK→RELOAD→…→UNLOCK транзакция | multiproc: 10/10 записей двух процессов |
| Crash durability записи | unique tmp + fsync + os.replace + dir fsync (POSIX) | fault injection ×8 стадий |
| Целостность чтения | schema + структура + состояния + обязательные поля + checksum | tamper checksum → fail-closed, файл не перезаписан |
| Отказ без обхода защиты | load-error/write-degraded блокируют и отправку, и запись | corrupted JSON остаётся некорректным после отказа |
| Crash recovery | in_flight → unknown при загрузке процесса | multiproc crash-before-POST / crash-after-POST |
| Ручное снятие | NOT_IMPLEMENTED, fail-closed | exit 3, блок сохраняется |

## Тесты R2 (fake transport, без Bybit и реальных ордеров)

* `tests/test_bybit_order_submit_guard_v1.py` — 37 тестов: таблица строгого
  соответствия (12 параметризованных случаев + malformed + семантика
  realtime→history), fault injection (8 стадий записи/чтения), checksum,
  manual resolution (guard + CLI), два guard-инстанса на одном journal path.
* `tests/test_bybit_order_submit_guard_multiproc_v1.py` — 6 кросс-процессных
  тестов (spawn, реальные OS-локи, файловый обмен результатами — named pipes
  недоступны в sandbox): ровно один POST из двух процессов; нет lost updates;
  crash после резервации и crash после POST; блокировка чужим процессом →
  0 attempts; параллельные reconciliation + create.
* `tests/test_bybit_retry_budget_v1.py` — 40 тестов без изменений поведения:
  budget ≤3 (property), POST max one send, deadline, telemetry parity —
  зелёные; итог CI-набора 245 passed.

## Регрессия R2 (локально, Windows)

| Набор | Результат |
|---|---|
| `test_bybit_order_submit_guard_v1.py` (37) + `multiproc_v1.py` (6) + `test_bybit_retry_budget_v1.py` (40) + `test_bybit_api_telemetry_v1.py` (57) | **140 passed** |
| CI-набор (8 файлов, полный состав workflow) | **245 passed** |
| Полный прогон (минус 4 pre-existing битых модуля) | **5258 passed / 79 skipped / 81 failed / 53 errors** |
| Baseline comparison vs `d8fbfd2` (25 файлов с падениями прогнаны на копии baseline) | **128 unique == 128 unique, `Compare-Object` пуст** → **регрессий нет**; новых failures/errors нет — новые модули полностью зелёные |

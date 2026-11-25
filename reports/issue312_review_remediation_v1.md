# PR #313 — Code Review Remediation Report (v1)

**Issue:** #312 — H2_RSI_ONLY Prospective Runtime V1
**PR:** #313 (Draft) — `research/312-h2-prospective-runtime-v1`
**Remediation commit:** `979a565` (fixes + tests; this report is the next commit)
**Base:** `origin/main` = `388e4ba`; prior branch head `5f1f5d1`
**Status:** `IMPLEMENTATION_READY / RE_REVIEW_REQUIRED / ACTIVATION_HOLD`

Правила отчёта: PASS ставится только там, где тест реально запускался; каждый
результат атрибутируется командой, коммитом и счётчиками. Нереализованные или
непроверенные сценарии перечислены явно (раздел «Ограничения»).

---

## 1. Сводная таблица находок

| # | Находка (review) | Исправление | Тест(ы) | Результат |
|---|---|---|---|---|
| 1 | **P1 Decimal:** `json.dumps(default=str)` терял тип `Decimal`; restore отдавал `str` в `run_h2_event` → валидное событие #305 становилось `INVALID_SOURCE` | Строгий `parse_source_price()`/`exact_price_str()` в `contracts.py` (bool/NaN/Infinity/≤0/нестандартные строки — отказ; `Decimal` — точно; `float` — через `repr`); `source_reader.delivery_from_row()` нормализует цену в канонический `Decimal` (иначе `REASON_INVALID_SOURCE_PRICE`); PG-хранилище: `NUMERIC`-колонка авторитетна, в payload — точная десятичная строка; `get_source_row()` строго восстанавливает тип; CHECK в миграции 095 отказывает NaN/Infinity/0/отрицательным на уровне БД; restore/движок ошибки цены → явный терминальный `INVALID_SOURCE` (не молчаливое приведение) | `test_me_entry_quality_h2_prospective_pg_integration_v1.py::TestPgDecimalNumericRoundTrip` (4: NUMERIC round-trip точности `Decimal('123.4567890123456789')`, redelivery+restart эквивалентность, restore→H2 evaluation точность, CHECK-отказы), `..._review_remediation_v1.py::TestDecimalExactness` (4), `..._source_reader_v1.py::TestSourcePriceValidation` (3) | **PASS** — см. §2.1 |
| 2 | **P1 Watermark:** курсор `(signal_time, source_event_key)` терял поздно закоммиченные события с меньшим `signal_time` | Курсор доставки = порядок ЗАХВАТА `(captured_at, source_event_key)`; две ограниченные фазы за цикл: forward-скан от курсора (гарантированный прогресс) + overlap-прошёл назад на `source_overlap_s` (capture-to-commit disorder, ничьи `captured_at`); ingest идемпотентен, variant states пере-обеспечиваются на дублях (окно краша ingest→state); checkpoint-last: курсор продвигается только после всех durable-записей; строгая проверка `freeze_ts` сохранена; без backfill. Колонки `reader_watermark` → `cursor_captured_at_us`/`cursor_key` | `..._review_remediation_v1.py::TestLateDeliveryNeverLost` (5: обязательный сценарий B-захвачен-раньше/A-с-более-ранним-signal_time — оба приняты и ровно по разу оценены; поздний коммит после рестарта; ничьи `captured_at`; граница батчей; краш между ingest и checkpoint), `..._pg_integration_v1.py::TestPgCaptureOrderIngest`, `..._runtime_v1.py::TestRestartRecovery`, `..._source_reader_v1.py` (курсор) | **PASS** — см. §2.2 |
| 3 | **P1 API budget:** `try_consume()` считал один вызов `get_klines_range()`, а тот пагинирует и ретраит; бюджет в памяти сбрасывался рестартом | `try_consume(count)` — атомарное резервирование ФАКТИЧЕСКОЙ стоимости HTTP-запросов: `pages_for_range()` зеркалит пагинацию `BybitClient.get_klines_range` (1 страница = 1 запрос при закреплённых фабрикой `bybit_max_attempts=1` / `bybit_rate_limit_max_retries=0`), `requests_per_page` — консервативная верхняя граница; ретраи резервируют заново; исчерпание → `BUDGET_EXHAUSTED` без единого запроса; почасовое окно ПЕРСИСТИТСЯ в `api_budget_state` (гидрация при старте, `on_change` сохранение; отказ сохранения → fail-closed latch); глобальный лимитер Bybit и прод-сканер не затронуты | `..._candle_adapter_v1.py::TestBudget` (4 новых) + `TestProvider::test_multipage_fetch_reserves_every_http_request` / `test_multipage_budget_exhaustion_blocks_all_requests` / `test_retry_attempt_is_fully_recounted` (fake transport, без реальных запросов), `..._pg_integration_v1.py::TestPgBudgetState` (restart), `..._store_v1.py::TestApiBudgetState` | **PASS** — см. §2.3 |
| 4 | **P2 Exit code:** `--once` возвращал `return EXIT_OK` в `except Exception` | Новый `EXIT_CYCLE_FAILED=1`: любое исключение → ненулевой код; `store.close()` в `finally` на обоих путях; успех — 0, выключенный рантайм — 3, ошибка конфига — 2 (сохранены) | `..._review_remediation_v1.py::TestRunnerExitCodes` (4: успех 0 + close; исключение ≠0 + close; disabled 3; config error 2) | **PASS** — см. §2.4 |
| 5 | **P2 Migration 095:** `CREATE TABLE IF NOT EXISTS` + голый `CREATE TRIGGER` → повторное применение падало | `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER` (×5), функции `CREATE OR REPLACE`; DO-гард: несовместимый существующий шема → `H2P_SCHEMA_INCOMPATIBLE` + полный rollback (ничего молча не принимается); новые `api_budget_state`, CHECK цены, переименованные колонки курсора; данные не повреждаются, терминальные результаты неизменяемы | `..._pg_integration_v1.py::TestPgMigration095` (4: первое применение + повторное (×3) идемпотентно; re-apply сохраняет существующие результаты и триггеры неизменяемости активны; индуцированная ошибка → полный rollback проверен; несовместимый шема → предсказуемый отказ) | **PASS** — см. §2.5 |

Дополнительно (задача §7): конфигурационный гард — включённый рантайм с
пустыми `H2_PROSPECTIVE_POLICY_FINGERPRINT_PINS` отказывается стартовать без
явного `H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES=1`
(`..._runtime_v1.py::TestDisabledRuntime::test_unfrozen_policy_accumulation_needs_explicit_permission`,
`..._review_remediation_v1.py::TestRunnerExitCodes::test_config_error_exits_config_error`).

---

## 2. Атрибуция тестов

Обязательное правило: **PASS только при реальном запуске**. Все команды ниже
выполнялись из рабочей копии ветки `research/312-h2-prospective-runtime-v1`.

### 2.0 Общие прогоны (коммит `979a565`, рабочая копия этого коммита)

| Команда | Охват | Итог |
|---|---|---|
| `python -m pytest tests/test_me_entry_quality_h2_prospective_*.py` (с `ME_H2PRO_PG_INTEGRATION=1`, `ME_H2PRO_PG_IT_HOST=127.0.0.1`, `ME_H2PRO_PG_IT_PORT=54332`, `ME_H2PRO_PG_IT_USER=postgres`) | весь контур #312 | **125 passed, 0 skipped, 0 failed** |
| то же без `ME_H2PRO_PG_INTEGRATION` | оффлайн-часть | 108 passed, 17 skipped (skip-причина: opt-in флаг PG-интеграции не выставлен) |
| `python -m pytest tests/test_me_entry_quality_h2_<engine-файлы PR #311 + purity> -q` | регрессия PR #311 | **168 passed, 0 failed** (167 исходных + 1 purity guard) |
| `python -m pytest tests -q --no-header --ignore=<5 файлов с Windows-only pwd-ошибками>` | полный сьют против базового лога `reports/issue312_full_run_head_base.txt` (base `388e4ba`) | см. §3 |

**PostgreSQL для интеграционных тестов** — реальный сервер PostgreSQL **17.2**
(извлечён из `embedded-postgres-binaries-windows-amd64-17.2.0.jar`, zonky),
изолированный scratch-кластер на `127.0.0.1:54332`, суперпользователь
`postgres`, auth `trust`, каталог `_pg_data/` (untracked). Каждый тест живёт в
собственной scratch-базе `me_h2pro_it_*` (создаётся/удаляется фикстурой); ни
одна продовая база/схема не читалась и не изменялась. Дампы серверного лога и
cleanup-скрипт: `_pg_binaries/` (untracked). Каталог `sql/migrations/095` к
продовой БД **не** применялся.

### 2.1 Находка 1 — Decimal (коммит `979a565`)

| Сценарий | Тест | Результат |
|---|---|---|
| Реальный `NUMERIC` round-trip: ingest → колонка NUMERIC → payload → restore; `Decimal('123.4567890123456789')` побитно точно | `TestPgDecimalNumericRoundTrip::test_numeric_round_trip_is_exact` (PG 17.2) | PASS |
| Restore → `run_h2_event`: точная десятичная строка сохранена в `original_price_exact` движком PR #311 | `::test_restored_price_reaches_the_h2_engine_exactly` (PG) | PASS |
| NaN/Infinity/-Infinity/0/отрицательные — отказ на уровне БД (CHECK `h2p_ingest_price_chk`); конечная положительная — проходит | `::test_database_refuses_nan_inf_zero_negative_prices` (PG) | PASS |
| Redelivery неизменен; после «рестарта» (новый store/новое соединение) значения идентичны | `::test_redelivery_and_restart_keep_the_exact_price` (PG) | PASS |
| Парсер: NaN/Inf/0/отрицательные/bool/строки со знаком, экспонентой, пробелами — `ValueError` | `TestDecimalExactness::test_nan_inf_zero_negative_are_rejected_at_parse` | PASS |
| Точная строковая форма — «plain decimal»; `0.1` → `"0.1"` без двоичного артефакта | `::test_exact_string_form_is_plain_decimal` | PASS |
| Длинная десятичная доходит до движка без искажений; невосстанавливаемая цена → терминальный `INVALID_SOURCE` | `::test_long_decimal_reaches_the_engine_verbatim`, `::test_unparseable_restored_price_becomes_invalid_source` | PASS |
| `delivery_from_row`: точный `Decimal` в delivered row; таблица отказов (14 форм) | `TestSourcePriceValidation` (3 теста) | PASS |

### 2.2 Находка 2 — Watermark/курсор (коммит `979a565`)

| Сценарий | Тест | Результат |
|---|---|---|
| **Обязательный:** B (signal 10:10) захвачен первым, A (signal 10:05 — раньше!) доставлен позже → оба приняты, оба оценены ровно один раз; второй цикл — только идемпотентные дубли | `TestLateDeliveryNeverLost::test_signal_order_inversion_both_accepted_both_evaluated_once` | PASS |
| Поздний коммит после «рестарта» с capture-моментом СТАРЕ курсора → overlap-прошёл находит его; третий цикл ничего не переоценивает | `::test_late_commit_after_restart_with_older_capture_is_picked_up` | PASS |
| Ничьи `captured_at` (два события, один инстант захвата, лимит батча 1) — оба прочитаны ровно один раз | `::test_equal_captured_at_ties_are_read_exactly_once` | PASS |
| Граница батчей: строк больше, чем `source_max_batches_per_cycle` — остаток добирает следующий цикл, ничего не потерянно | `::test_batch_boundary_drains_bounded_batches_without_loss` | PASS |
| Краш между ingest и checkpoint: курсор не продвинулся, replay после рестарта идемпотентен, один исход | `::test_crash_between_ingest_and_checkpoint_replays_idempotently` | PASS |
| Persistence-слой: инверсия порядка сигналов при захвате (B раньше A) — оба first-seen снапшота в PG | `TestPgCaptureOrderIngest` (PG) | PASS |
| Курсор переживает рестарт (PG + in-memory) | `TestPgIngest::test_cursor_roundtrip` (PG), `TestCursor` (in-memory) | PASS |
| Restart-recovery: терминальные исходы не пересоздаются, redelivery идемпотентен | `..._runtime_v1.py::TestRestartRecovery` | PASS |

**Контракт курсора (доказательства и границы).** Порядок `(captured_at,
source_event_key)` монотонен в порядке записи #305: `source_event` не имеет
identity-колонки, но `captured_at` присваивается в момент построения захвата
(`me_upstream_source_v1/contracts.py`), таблица индексируется по
`(captured_at)`, а запись ведётся единственным писателем (сканер захвата),
каждый statement самокоммитится (один коммит на событие). Остаточный риск —
теоретическая инверсия «захват позже, коммит раньше» между двумя событиями
глубже, чем окно `source_overlap_s` (по умолчанию 2 ч): он покрыт
overlap-прошёлом и документирован в `source_reader.py`; абсолютная гарантия
потребовала бы изменения контракта #305 (отдельная монотонная последовательность
захвата) — это **вне** рамок задачи и не требуется при текущем однописателевом
контракте. `BLOCKED_SOURCE_CURSOR_CONTRACT` **не** выставляется: контракт
проверен по коду #305 (single-writer, self-committing, `captured_at` в момент
захвата, индекс) и закреплён overlap-алгоритмом; резервный риск задокументирован
в §5.

### 2.3 Находка 3 — API budget (коммит `979a565`, только fake transport)

| Сценарий (таблица приёмки) | Тест | Результат |
|---|---|---|
| 1 range / 1 page → фактический учёт | `TestProvider::test_successful_fetch_builds_verified_identity` + `TestBudget::test_pages_map_to_http_requests` | PASS |
| Мультистраничный диапазон (`max_pages > 1`) → ВСЕ страницы в бюджете до первого запроса | `::test_multipage_fetch_reserves_every_http_request` | PASS |
| Ретрай API → перезарезервирован и посчитан полностью | `::test_retry_attempt_is_fully_recounted` | PASS |
| Бюджет исчерпан → ни одного запроса не уходит | `::test_multipage_budget_exhaustion_blocks_all_requests`, `::test_budget_exhaustion_is_fail_closed` | PASS |
| Рестарт → часовой лимит не сбрасывается небезопасно | `TestBudget::test_hour_window_survives_restart` (in-memory), `TestPgBudgetState::test_api_budget_state_survives_restart` (PG) | PASS |
| Rate limit → fail-closed, сканер не затронут (изоляция) | `..._runtime_v1.py::TestFailClosed::test_api_failure_leaves_state_pending` (HTTP 429), `TestProductionIsolation` | PASS |
| Бюджет не подтверждён → провайдер OFF, 0 запросов | `::test_unconfirmed_budget_blocks_candle_source`, `TestProvider::test_disabled_provider_makes_no_request` | PASS |
| Консервативная верхняя граница `requests_per_page` | `TestBudget::test_pages_map_to_http_requests`, `::test_multi_request_reservation_is_all_or_nothing` | PASS |
| Настройки клиента закреплены фабрикой (инвариант 1 страница = 1 запрос) | `TestRunnerClientFactoryPins` (2 теста: `bybit_max_attempts=1`, `bybit_rate_limit_max_retries=0`, `requests_per_page == 1`) | PASS |
| Отказ сохранения состояния → fail-closed latch | `TestBudget::test_persistence_failure_is_fail_closed` | PASS |

Инвариант «1 страница = 1 HTTP-запрос» закреплён фабрикой рантайма
(`bybit_max_attempts=1`, `bybit_rate_limit_max_retries=0`) и документирован в
`CandleApiBudget`. При верификации взаимодействия с настройками клиента тест
**обнаружил и устранил скрытую утечку бюджета**: `Settings` по умолчанию даёт
`bybit_rate_limit_max_retries=5`, что позволяло одной странице стоить до 6
HTTP-запросов при 429; фабрика теперь пинит значение в 0, и это закреплено
тестом `TestRunnerClientFactoryPins` (оба значения настроек + бюджет
`requests_per_page == 1`). Для клиентов с внутренними ретраями предусмотрен
`requests_per_page` (консервативное завышение). **Реальные запросы Bybit при
тестировании не выполнялись ни разу.**

### 2.4 Находка 4 — Exit code (коммит `979a565`)

| Сценарий | Тест | Результат |
|---|---|---|
| Успех → exit 0, store закрыт | `TestRunnerExitCodes::test_successful_once_exits_zero_and_closes_store` | PASS |
| Исключение → exit `EXIT_CYCLE_FAILED=1` (≠ 0), store закрыт | `::test_failed_once_exits_nonzero_and_closes_store` | PASS |
| Disabled → 3 (без I/O) | `::test_disabled_once_exits_not_active` | PASS |
| Ошибка конфигурации → 2 | `::test_config_error_exits_config_error` | PASS |

Smoke-тесты теперь различают PASS/FAIL по коду возврата (`--once`).

### 2.5 Находка 5 — Migration 095 (коммит `979a565`, scratch PG 17.2)

| Сценарий | Тест | Результат |
|---|---|---|
| Первое применение → PASS | фикстура + `TestPgMigration095::test_first_apply_and_reapply_are_idempotent` | PASS |
| Повторное применение (×3) → PASS, идемпотентно | тот же тест | PASS |
| Re-apply не трогает существующие терминальные результаты (байт-в-байт) и триггеры неизменяемости остаются активными | `::test_reapply_preserves_existing_results_and_immutability` | PASS |
| Ошибка внутри миграции → полный транзакционный rollback (таблицы не появляются) | `::test_induced_migration_error_rolls_back_everything` | PASS |
| Несовместимый существующий шема → `H2P_SCHEMA_INCOMPATIBLE`, предсказуемый отказ + rollback новых таблиц | `::test_incompatible_pre_existing_schema_is_predictably_refused` | PASS |
| UPDATE/DELETE на терминальных результатах запрещены триггерами БД | `TestPgLifecycle::test_result_immutability_is_enforced_by_the_database` | PASS |

Все миграционные тесты — только на изолированных scratch-базах; к продовой БД
миграция **не** применялась.

### 2.6 Совместимость с PR #311 (чистый H2-движок)

* Движок `app/research/me_entry_quality_h2_v1/**` **не изменён** (diff ветки
  это подтверждает — все изменения в `me_entry_quality_h2_prospective_v1`,
  `sql/`, `tests/`, `deploy/`).
* Регрессия PR #311: **168 passed / 0 failed** (команда §2.0).
* Purity/isolation-тесты (`test_me_entry_quality_h2_purity_v1.py`) входят в
  эти 168 и проходят.

---

## 3. Полный сьют и сравнение с базой

Базовый лог: `reports/issue312_full_run_head_base.txt` (клон `_h2_pr311` на
`388e4ba`, команда §2.0). Сравнение — `reports/issue312_compare_failures.py`
(регэксп `^(FAILED|ERROR)\s+(\S+)`).

(Заполняется после прогона — см. итоговый комментарий PR: файл
`reports/issue312_full_run_head.txt` содержит полный лог; расхождения против
базы перечислены в итоговом отчёте и в тексте PR.)

---

## 4. Сквозной контур (задача §7) — повторный аудит

| Проверка | Статус | Где закреплено |
|---|---|---|
| `source_event_key`/`lineage_hash` сохраняются сквозного (источник → ingest → состояние → результат) | OK | `TestPgIngest`, `TestLateDeliveryNeverLost`, `..._store_v1.py` |
| Микросекундные границы `signal_time`/`freeze_ts`; строгая проверка `freeze_ts` сохранена | OK | `parse_utc_us` (целочисленные µs), `TestIngestChecks` (в т.ч. сигнал ровно на freeze — отказ) |
| Каузальный ASOF только по закрытым 5m; полное окно 120 свечей; без look-ahead | OK | `required_fetch_range`/`observation_window_elapsed`, `TestFetchRange`, `TestWindowElapsed` |
| Recovery после рестарта без пересоздания исходов | OK | `TestRestartRecovery`, PG-тесты курсора/бюджета |
| Неизменяемость терминальных исходов | OK | триггеры БД + `TestPgLifecycle`, re-apply тест |
| Нет выдуманной цены исполнения / Net E[R] | OK | CHECK `entry_candidate_price IS NULL`, `TestNoFictionalExecution` |
| 0 событий → `SOURCE_EMPTY_WAITING` без искусственного накопления | OK | `TestSourceEmptyWaiting` |
| Накопление с несфrozen-отпечатками политик требует отдельного разрешения | OK | `test_unfrozen_policy_accumulation_needs_explicit_permission` (fail closed по умолчанию) |
| Честный бюджет фактических HTTP-запросов без конкуренции со сканером | OK | §2.3; прод-лимитер не изменён (diff) |

---

## 5. Ограничения и непроверенные сценарии (явно)

1. **Не проверено на реальном трафике #305:** таблица `source_event` сейчас
   пуста (0 строк) — реальная доставка не тестировалась; сценарии покрыты
   синтетическими строками с реальной схемой и реальным PostgreSQL.
2. **Остаточный риск курсора:** глубокая (> `source_overlap_s`, по умолчанию
   2 ч) инверсия capture-to-commit порядка между событиями теоретически
   требует контрактного изменения #305 (отдельная монотонная последовательность
   захвата). Mitigation: overlap-прошёл + идемпотентный ingest; при
   подтверждённой аномалии — фиксировать `BLOCKED_SOURCE_CURSOR_CONTRACT`
   вместо алгоритма с потерями.
3. **Инвариант «1 страница = 1 запрос»** держится на настройках клиента,
   закреплённых фабрикой рантайма (`bybit_max_attempts=1`,
   `bybit_rate_limit_max_retries=0`); для иных клиентов предусмотрен
   консервативный `requests_per_page`. Фактические HTTP-запросы на реальном
   Bybit **не** измерялись (запрещено); соответствие формулы пагинации
   проверено по коду `BybitClient.get_klines_range` и fake-транспорт тестами.
4. **`--once` в режиме `run_forever`** (systemd) не менялся по семантике
   выхода: бесконечный цикл по-прежнему не завершается по ошибке цикла
   (переподключение) — это штатное поведение сервиса, изменение выходных
   кодов касается только `--once` (smoke-режима), как и требовалось.
5. **Производительность overlap-прошёла** на больших окнах не замерялась
   (нагрузочный тест вне рамок); ограничена `source_max_batches_per_cycle`
   и `source_batch_limit`.
6. Тесты с реальным PG выполнялись на **PostgreSQL 17.2** (scratch); продовая
   версия PG не проверялась (миграция в прод не применялась — запрещено).

---

## 6. Изменённые файлы (коммит `979a565`)

* `app/research/me_entry_quality_h2_prospective_v1/`: `contracts.py`,
  `source_reader.py`, `store.py`, `candle_adapter.py`, `runtime.py`,
  `config.py`, `runner.py`
* `sql/migrations/095_me_entry_quality_h2_prospective_v1.sql`
* `deploy/env/h2-prospective-runtime.env.example`
* `tests/`: `test_me_entry_quality_h2_prospective_{source_reader,candle_adapter,store,runtime,pg_integration}_v1.py`,
  **новый** `test_me_entry_quality_h2_prospective_review_remediation_v1.py`

Движок PR #311 (`app/research/me_entry_quality_h2_v1/**`), #305-контур,
торговые/сканерные модули — **не изменялись**.

---

## 7. Итоговый статус

`IMPLEMENTATION_READY / RE_REVIEW_REQUIRED / ACTIVATION_HOLD`

* Готово: все 5 находок исправлены и закреплены тестами, включая
  обязательные сценарии (P1: реальный PG NUMERIC round-trip; mandatory
  late-delivery B/A; бюджетная таблица приёмки; P2: коды выхода;
  идемпотентная миграция).
* Не сделано (запрещено рамками задачи): merge PR #313, deploy, активация
  рантайма, применение миграции 095 к продовой БД, изменения политики
  исполнения/исхода (FR-06 остаётся `BLOCKED_PENDING_DECISION`), параметры H2.
* Решение о переводе Draft → Ready for Review принимает владелец PR.

# PR #313 — Code Review Remediation Report (v2)

> v2 = раунд 1 (5 находок Code Review) + раунд 2 (RE_REVIEW_CHANGES_REQUIRED:
> P1 Source Overlap Starvation, §8) + завершённое сравнение полного прогона с
> базой `388e4ba` (§3).

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
| 2 | **P1 Watermark:** курсор `(signal_time, source_event_key)` терял поздно закоммиченные события с меньшим `signal_time` | Курсор доставки = порядок ЗАХВАТА `(captured_at, source_event_key)`; две ограниченные фазы за цикл: forward-скан от курсора (гарантированный прогресс) + overlap-прошёл назад на `source_overlap_s` (capture-to-commit disorder, ничьи `captured_at`); ingest идемпотентен, variant states пере-обеспечиваются на дублях (окно краша ingest→state); checkpoint-last: курсор продвигается только после всех durable-записей; строгая проверка `freeze_ts` сохранена; без backfill. Колонки `reader_watermark` → `cursor_captured_at_us`/`cursor_key` | `..._review_remediation_v1.py::TestLateDeliveryReconciliation` (5: обязательный сценарий B-захвачен-раньше/A-с-более-ранним-signal_time — оба приняты и ровно по разу оценены; поздний коммит после рестарта; ничьи `captured_at`; граница батчей; краш между ingest и checkpoint), `..._pg_integration_v1.py::TestPgCaptureOrderIngest`, `..._runtime_v1.py::TestRestartRecovery`, `..._source_reader_v1.py` (курсор) | **PASS** — см. §2.2 |
| 3 | **P1 API budget:** `try_consume()` считал один вызов `get_klines_range()`, а тот пагинирует и ретраит; бюджет в памяти сбрасывался рестартом | `try_consume(count)` — атомарное резервирование ФАКТИЧЕСКОЙ стоимости HTTP-запросов: `pages_for_range()` зеркалит пагинацию `BybitClient.get_klines_range` (1 страница = 1 запрос при закреплённых фабрикой `bybit_max_attempts=1` / `bybit_rate_limit_max_retries=0`), `requests_per_page` — консервативная верхняя граница; ретраи резервируют заново; исчерпание → `BUDGET_EXHAUSTED` без единого запроса; почасовое окно ПЕРСИСТИТСЯ в `api_budget_state` (гидрация при старте, `on_change` сохранение; отказ сохранения → fail-closed latch); глобальный лимитер Bybit и прод-сканер не затронуты | `..._candle_adapter_v1.py::TestBudget` (4 новых) + `TestProvider::test_multipage_fetch_reserves_every_http_request` / `test_multipage_budget_exhaustion_blocks_all_requests` / `test_retry_attempt_is_fully_recounted` (fake transport, без реальных запросов), `..._pg_integration_v1.py::TestPgBudgetState` (restart), `..._store_v1.py::TestApiBudgetState` | **PASS** — см. §2.3 |
| 4 | **P2 Exit code:** `--once` возвращал `return EXIT_OK` в `except Exception` | Новый `EXIT_CYCLE_FAILED=1`: любое исключение → ненулевой код; `store.close()` в `finally` на обоих путях; успех — 0, выключенный рантайм — 3, ошибка конфига — 2 (сохранены) | `..._review_remediation_v1.py::TestRunnerExitCodes` (4: успех 0 + close; исключение ≠0 + close; disabled 3; config error 2) | **PASS** — см. §2.4 |
| 5 | **P2 Migration 095:** `CREATE TABLE IF NOT EXISTS` + голый `CREATE TRIGGER` → повторное применение падало | `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER` (×5), функции `CREATE OR REPLACE`; DO-гард: несовместимый существующий шема → `H2P_SCHEMA_INCOMPATIBLE` + полный rollback (ничего молча не принимается); новые `api_budget_state`, CHECK цены, переименованные колонки курсора; данные не повреждаются, терминальные результаты неизменяемы | `..._pg_integration_v1.py::TestPgMigration095` (4: первое применение + повторное (×3) идемпотентно; re-apply сохраняет существующие результаты и триггеры неизменяемости активны; индуцированная ошибка → полный rollback проверен; несовместимый шема → предсказуемый отказ) | **PASS** — см. §2.5 |
| 6 | **Раунд 2, P1 Source Overlap Starvation:** overlap-проход перечитывал первые страницы окна каждый цикл без сохранения прогресса; общий лимит батчей позволял forward-фазе полностью вытеснять reconciliation — поздние события в конце окна не достигались никогда | Независимый durable overlap-курсор (`overlap_cursor`, миграция 096), независимые бюджеты фаз, детерминированные проходы с фиксированной верхней границей и per-batch checkpoint, контигуальное догоняющее сканирование без пропусков + явный диагностический статус `OVERLAP_COVERAGE_DEGRADED` при нарушенном окне; границы гарантии публикуются в каждом отчёте | `..._overlap_reconciliation_v1.py` (14: все 12 обязательных сценариев + ключевой тест 1000+ событий с рестартом), `..._pg_integration_v1.py::TestPgOverlapCursor` (4) + `::TestPgMigration096` (4) | **PASS** — см. §8 |

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
| Граница батчей: строк больше, чем бюджет цикла — остаток добирает следующий цикл из сохранённого курсора (в границах документированной гарантии §8.2) | `::test_batch_boundary_drains_bounded_batches_without_loss` | PASS |
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

## 3. Полный сьют и сравнение с базой (задача §7)

### 3.1. Методика и окружение обеих сторон

| Параметр | Base | PR #313 (head) |
|---|---|---|
| Коммит | `388e4ba0d2f93f7a4295d5d733c78976def0b9c4` (`origin/main`) | см. §3.4 |
| Команда | `python -m pytest tests -q --no-header --ignore=tests/test_me_r_v3_atomic_pg_e2e_issue154.py --ignore=tests/test_me_r_v3_isolated_oos_collector.py --ignore=tests/test_me_r_v3_isolated_pg_collector_issue154.py --ignore=tests/test_operations_monitor_retention.py --ignore=tests/test_srr_short_r_expansion_candle_path_integrity_audit_v1.py` | та же |
| Python | 3.13.17 (venv `.\.venv`) | тот же |
| PostgreSQL | для full-suite не используется (см. ниже); PG-тесты в этом прогоне SKIP | тот же; отдельные PG-прогоны — scratch PostgreSQL **17.2** (`127.0.0.1:54332`, изолированный кластер) |
| psycopg2 | 2.9.13 | тот же |
| Окружение | Windows, рабочая копия ветки; `ME_H2PRO_PG_INTEGRATION` **не выставлен** → 25 PG-тестов #312 SKIP с причиной «opt-in: set ME_H2PRO_PG_INTEGRATION=1» | то же |
| Лог | `reports/issue312_full_run_base.txt` | `reports/issue312_full_run_head.txt` |

**Причины ignore (5 файлов, одинаковы с обеих сторон):**
`test_me_r_v3_atomic_pg_e2e_issue154.py`, `test_me_r_v3_isolated_oos_collector.py`,
`test_me_r_v3_isolated_pg_collector_issue154.py` — требуют внешний e2e-PG
контур V3 (не относятся к #312; исключены из полного прогона исторически);
`test_operations_monitor_retention.py` — тяжёлый retention-прогон;
`test_srr_short_r_expansion_candle_path_integrity_audit_v1.py` — аудиторский
прогон с внешними фикстурами. Идентичный набор ignore у обеих сторон делает
сравнение корректным.

**Артефакты парсинга:** регэксп `^(FAILED|ERROR)\s+(\S+)` из
`issue312_compare_failures.py` дополнительно матчит строки логгирования вида
`ERROR app.x:y.py:NNN`; чистые списки именно тестовых ID извлекаются
`reports/issue312_extract_failures.py` (фильтр `tests/`-node ids).

### 3.2. Base `388e4ba` — результаты

* **73 failed, 7883 passed, 432 skipped, 168 errors** (23:14).
* 241 failing/errored тестовых ID (73 + 168) — полный список:
  `reports/issue312_full_run_failures_base.txt`.
* Все 168 errors — ранее существующие падения коллекции/фикстур
  PG-зависимых analytics/migration-тестов (отсутствие контура БД в прогоне);
  73 failed — ранее существующие (среди них: analytics/migration-свиты,
  scanner grafana/direction, me_r_close_location PG-свиты, prospective_oos_v3
  guards и др. — все присутствуют на `388e4ba` без изменений #312).

### 3.3. Сравнение base ↔ head (фактические данные обоих прогонов)

| Показатель | Base `388e4ba` | PR #313 (head, раунд 2) | Delta |
|---|---|---|---|
| Collected (`--co`, тот же ignore-набор) | 8 555 | 8 703 | +148 |
| Passed | 7 883 | 8 004 | +121 |
| Failed | 73 | 75 | +2 |
| Skipped | 432 | 457 | +25 |
| Errors | 168 | 168 | 0 |
| Время прогона | 23:14 | 25:40 | — |

**Exit code полного Head suite:** job `pwsh-1` завершился с `exit code 0`
(цепочка `pytest … > log; Get-Content log` — код маскируется последней
командой чтения лога). Собственный код выхода pytest = **1** — в прогоне есть
75 failed + 168 errors (контракт кодов pytest: 1 = есть падения; продемонстрирован
замером на этом venv: failing-тест → `exit code 1`).

**Failing-ID diff** (чистые списки тестовых ID,
`reports/issue312_extract_failures.py`; raw-регэксп дополнительно матчит
`ERROR app.x:y.py:NNN`-строки логгера — они исключены):

* Base: 241 failing/errored тестовых ID (73 failed + 168 errors) —
  `reports/issue312_full_run_failures_base.txt`.
* Head: 243 ID (75 failed + 168 errors) —
  `reports/issue312_full_run_failures_head.txt`.
* **Новые в head (не падали на base): 2** —
  `tests/test_htf_baseline_short_phase2_isolation_v1.py::test_new_files_are_confined_to_phase2_surface`,
  `tests/test_htf_baseline_short_phase2_isolation_v1.py::test_no_tracked_file_modified_against_base`.
* Ушедшие из head (падали на base): 0. Популяция errors (168) идентична с
  обеих сторон (diff множеств пуст).

**Атрибуция различий:**

| Изменение | Объяснение | Регрессия #312? |
|---|---|---|
| Failed 73→75 (+2) | Два Phase-2 guard-теста `test_htf_baseline_short_phase2_isolation_v1.py` проверяют состояние рабочей копии («no tracked file modified against base», «new files confined to phase2 surface») и падают на ЛЮБОЙ изменённой рабочей копии ветки; на чистом `388e4ba` проходят; в CI они деселектятся. Не затрагивают рантайм/схему/данные | Нет — worktree-гарды, не поведение кода |
| Passed 7 883→8 004 (+121) | 122 passed новых offline-тестов #312 + 1 новый purity-тест − 2 перечисленных гарда | — |
| Skipped 432→457 (+25) | 25 PG-тестов #312 под opt-in флагом `ME_H2PRO_PG_INTEGRATION` (в этом прогоне флаг не выставлен; все 25 подтверждены отдельным PG-прогоном — §8.6) | — |
| Collected +148 | +147 (новые файлы тестов: `candle_adapter` 24, `lifecycle` 18, `overlap_reconciliation` 14, `pg_integration` 25, `review_remediation` 15, `runtime` 16, `source_reader` 16, `store` 19) + 1 (purity 9→10) | — |
| Errors 168→168 | Идентичная популяция (PG-зависимые analytics/migration-фикстуры без контура БД; присутствуют на `388e4ba`) | — |

**Независимый cross-check (VPS, отчёт ревьюера), старый HEAD `0b00482`
(до раунда 2; PostgreSQL 17.12):** H2 baseline `388e4ba` — 167 PASS;
PR #313 offline — 276 PASS / 1 SKIP; PostgreSQL integration — 17 PASS
(файл PG-тестов до расширения до 25); full-suite collection: Base 8 349 /
Head 8 458, ошибок нет после трёх одинаковых исключений (набор из 3
исключений отличается от локального из 5 — числа коллекции напрямую не
сопоставляются). Эти цифры приведены как независимое подтверждение
направления; локальные прогоны в таблице выше — верифицированные результаты
этого отчёта.

### 3.4. Критерий

PR не объявляется успешным по числу passed; критерий — ОТСУТСТВИЕ регрессий,
присутствующих только в head. Полные логи: `reports/issue312_full_run_base.txt`,
`reports/issue312_full_run_head.txt`.

---

## 4. Сквозной контур (задача §7) — повторный аудит

| Проверка | Статус | Где закреплено |
|---|---|---|
| `source_event_key`/`lineage_hash` сохраняются сквозного (источник → ingest → состояние → результат) | OK | `TestPgIngest`, `TestLateDeliveryReconciliation`, `..._store_v1.py` |
| Микросекундные границы `signal_time`/`freeze_ts`; строгая проверка `freeze_ts` сохранена | OK | `parse_utc_us` (целочисленные µs), `TestIngestChecks` (в т.ч. сигнал ровно на freeze — отказ) |
| Каузальный ASOF только по закрытым 5m; полное окно 120 свечей; без look-ahead | OK | `required_fetch_range`/`observation_window_elapsed`, `TestFetchRange`, `TestWindowElapsed` |
| Recovery после рестарта без пересоздания исходов | OK | `TestRestartRecovery`, `TestCrashSafety` (restart/crash прохода), `TestPgOverlapCursor::test_restart_recovery_state_is_seeable_by_a_new_store`, PG-тесты курсора/бюджета |
| Reconciliation-проход не пропускает диапазоны при движении forward-курсора и меняющихся границах окна | OK | `TestPassBoundaries` (сценарии 12), `TestCoverageBoundary::test_fall_behind_beyond_window_reports_degraded_coverage` |
| Граница гарантии полноты публикуется; её нарушение — диагностический статус, не ложное подтверждение | OK | `details.overlap.guarantee_floor_us` / `oldest_unchecked_capture_ts`, статус `OVERLAP_COVERAGE_DEGRADED`, сценарий 10 |
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
2. **Граница гарантии reconciliation (FR-05):** перепроверка окна
   `source_overlap_s` (по умолчанию 2 ч) действует, пока проходы укладываются
   в окно; при отставании sweep более чем на окно цикл явно помечается
   `OVERLAP_COVERAGE_DEGRADED`, а гарантия ограничивается опубликованной
   границей (`details.overlap.guarantee_floor_us`). Capture-to-commit disorder
   ГЛУБЖЕ границы не покрывается и молча не «подтверждается»; если реальная
   аномалия потребует абсолютной полноты — контрактное изменение #305
   (монотонная последовательность захвата) и статус
   `BLOCKED_SOURCE_CURSOR_CONTRACT` вместо алгоритма с потерями.
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
5. **Производительность reconciliation-проходов** на больших окнах не
   замерялась (нагрузочный тест вне рамок); нагрузка ограничена
   независимыми бюджетами `source_forward_batches_per_cycle` /
   `source_overlap_batches_per_cycle` и `source_batch_limit`; оценка
   остатка (`overlap_backlog_estimate`) — один `SELECT COUNT` по индексу за
   цикл.
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

---

## 8. Раунд 2 ревью (`RE_REVIEW_CHANGES_REQUIRED`): P1 — Source Overlap Starvation

### 8.1. Дефект (как был)

Две фазы цикла (`_ingest_phase`) делили ОБЩИЙ лимит `source_max_batches_per_cycle`:
forward-скан мог полностью вытеснять overlap-проход; overlap-проход при этом
каждый цикл начинался с одной и той же позиции (`cursor − source_overlap_s`)
без сохранения прогресса. При окне в 1000 строк и лимите 300 строк/цикл
проход перечитывал строки 1–300 каждый цикл и никогда не достигал, например,
строки 950 — позднее событие в конце окна оставалось необработанным навсегда.

### 8.2. Дизайн исправления (FR-01…FR-05)

* **FR-01 — независимый durable overlap cursor.** Новая singleton-таблица
  `research_me_entry_quality_h2_prospective.overlap_cursor` (миграция **096**):
  `(cursor_captured_at_us, cursor_key)` — прогресс ВНУТРИ прохода,
  `pass_floor_us`/`pass_ceiling_us` — границы прохода, `pass_active` — проход в
  работе (рестарт его возобновляет), `passes_completed` — счётчик проходов.
  Основной курсор (`reader_watermark`) и overlap cursor независимы; оба в
  research-схеме; оба переживают restart (не process-local).
* **FR-02 — гарантированный прогресс.** Разделённые бюджеты:
  `source_forward_batches_per_cycle` (по умолчанию 8) и
  `source_overlap_batches_per_cycle` (по умолчанию 2); суммарно ограничены.
  Forward backlog не блокирует reconciliation и наоборот (сценарии 3–4).
* **FR-03 — детерминированное завершение прохода.** Верхняя граница прохода
  ФИКСИРУЕТСЯ в момент старта (`pass_ceiling_us` = позиция forward-курсора) и
  не двигается, пока проход идёт: продвижение основного курсора не может
  исключить ещё не проверенные диапазоны. Прогресс сохраняется после КАЖДОЙ
  обработанной порции (per-batch checkpoint). Следующий проход стартует с
  `floor = min(предыдущий ceiling, новый ceiling − source_overlap_s)`:
  штатно окно `source_overlap_s` ПЕРЕПРОВЕРЯЕТСЯ каждым проходом; при
  отставании более чем на окно проход догоняет КОНТИГУАЛЬНО (от предыдущего
  ceiling — диапазоны никогда не пропускаются), а цикл помечается
  диагностическим статусом `OVERLAP_COVERAGE_DEGRADED` (см. FR-05).
  События из #305 не удаляются, исторический backfill не выполняется.
* **FR-04 — crash safety.** Checkpoint-last: overlap-курсор продвигается
  только после durable-ingest и создания variant states (ensure на дублях —
  окно краша ingest→state закрыто). Все 5 точек отказа покрыты тестами
  (чтение→ingest; ingest→states; states→checkpoint; checkpoint→цикл;
  завершение прохода): после restart нет потерь и нет повторного создания
  терминальных outcomes; прерванный проход возобновляется с сохранённой
  позиции (`overlap_restart_recovery`).
* **FR-05 — source completeness и границы гарантии.** Строгие проверки
  `source_event_key`/`source_identity`/`lineage_hash`/
  `signal_time > activation_freeze_ts`/`captured_at >= activation_freeze_ts`
  не изменялись; `freeze_ts` #305 не изменялся. Формулировка `NEVER_LOST` /
  «никогда не теряются» **сознательно не используется**. Гарантия:

  > Событие с capture-моментом `X`, ставшее видимым (закоммиченным) до того,
  > как reconciliation-проход дойдёт до позиции `X`, будет обнаружено. При
  > штатной работе (не `DEGRADED`) каждый проход дополнительно перепроверяет
  > окно `source_overlap_s` позади своей верхней границы.

  **Вне гарантии:** capture-to-commit disorder глубже опубликованной границы
  (`details.overlap.guarantee_floor_us`, `oldest_unchecked_capture_ts` в
  каждом отчёте цикла) не покрывается; событие за границей молча не считается
  обработанным и не «подтверждается»; при невозможности соблюсти окно
  формируется диагностический статус `OVERLAP_COVERAGE_DEGRADED` (health-row +
  маркер `H2P_OVERLAP_COVERAGE_DEGRADED` в логе), а не ложное подтверждение
  coverage.

### 8.3. Телеметрия (задача §4)

Все восемь показателей добавлены в `TelemetryCounters.to_dict()` и доступны
через существующие H2 telemetry и runtime health (`CycleReport.counters`,
JSONB-колонки `runtime_health.counters`, циклический лог
`H2_PROSPECTIVE_CYCLE`; построчное журналирование событий не вводилось):

| Метрика | Назначение |
|---|---|
| `forward_batches_scanned` | forward batch за цикл |
| `overlap_batches_scanned` | reconciliation batch за цикл |
| `overlap_pass_completed` | проход дошёл до фиксированной границы |
| `overlap_cursor_lag_s` | отставание reconciliation-курсора от forward (сек) |
| `oldest_unchecked_capture_ts` | позиция следующей не проверенной строки (µs) |
| `late_events_discovered` | впервые найденные поздние события (accepted и rejected) |
| `overlap_backlog_estimate` | оценка остатка прохода (SELECT COUNT по индексу) |
| `overlap_restart_recovery` | проход, прерванный рестартом, возобновлён |

### 8.4. Обязательные регрессионные сценарии (задача §5) — 12/12

Файл `tests/test_me_entry_quality_h2_prospective_overlap_reconciliation_v1.py`
(синтетические #305-события + fake connection; **без реальных запросов Bybit**):

| № | Сценарий | Тест | Результат |
|---|---|---|---|
| 1 | Overlap больше лимита цикла — курсор движется дальше | `TestOverlapStarvation::test_thousand_row_overlap_drains_progressively_with_restart` (**ключевой**: 1000+ событий, 2 batch/цикл, позднее событие у конца окна, рестарт посередине прохода — обнаружено ровно один раз) + `::test_overlap_progress_is_checkpointed_every_cycle` | PASS |
| 2 | Позднее событие в конце overlap — обнаружено и принято | то же (evt-late в 10-м batch прохода) | PASS |
| 3 | Forward backlog постоянный — overlap работает | `TestIndependentBudgets::test_forward_backlog_cannot_starve_reconciliation` | PASS |
| 4 | Overlap backlog постоянный — forward работает | `TestIndependentBudgets::test_reconciliation_backlog_cannot_block_forward_progress` | PASS |
| 5 | Restart посередине прохода — прогресс восстановлен | `TestCrashSafety::test_restart_mid_pass_resumes_without_resweeping` (без повторного просмотра пройденных страниц) | PASS |
| 6 | Crash после ingest до checkpoint — идемпотентно | `TestCrashSafety::test_crash_between_overlap_ingest_and_checkpoint_replays` | PASS |
| 7 | Несколько событий с одним `captured_at` | `TestCaptureOrderEdgeCases::test_equal_captured_at_ties_survive_pass_boundaries` | PASS |
| 8 | Late delivery с более ранним `signal_time` | `TestCaptureOrderEdgeCases::test_late_delivery_with_earlier_signal_time_is_accepted_by_pass` | PASS |
| 9 | Повторная доставка — нет повторного outcome | `TestCaptureOrderEdgeCases::test_redelivery_never_recreates_outcome` | PASS |
| 10 | Событие за пределами гарантированного overlap — ограничение зафиксировано | `TestCoverageBoundary::test_event_outside_guaranteed_overlap_is_explicitly_recorded` (публикуемая граница `guarantee_floor_us` строго выше capture-момента; событие не считается покрытым) | PASS |
| 11 | Нулевой источник #305 — `SOURCE_EMPTY_WAITING` | `TestPassBoundaries::test_zero_source_events_report_source_empty_waiting` | PASS |
| 12 | Длительная работа с меняющимися границами окна — без скачков | `TestPassBoundaries::test_pass_bounds_across_cycles_never_skip_unchecked_ranges` + `::test_pass_ceiling_is_pinned_at_pass_start` | PASS |

Плюс явный тест нарушения контракта полноты:
`TestCoverageBoundary::test_fall_behind_beyond_window_reports_degraded_coverage`
— догоняющее сканирование без пропусков + статус `OVERLAP_COVERAGE_DEGRADED`
(тест проверяет, что runtime НЕ выдаёт ложный OK).

### 8.5. PostgreSQL и миграции (задача §6)

* Изменения схемы — только в `research_me_entry_quality_h2_prospective`.
* Создана **следующая** миграция `096_me_entry_quality_h2_prospective_overlap_v1.sql`
  (не изменение уже применённой 095): идемпотентная (`CREATE TABLE IF NOT
  EXISTS` + guarded `ALTER`), транзакционная (ошибка → полный rollback),
  DO-гард `H2P_SCHEMA_INCOMPATIBLE`; 095 остаётся неизменной и идемпотентной.
* Тесты на scratch PostgreSQL 17.2 (изолированный кластер `127.0.0.1:54332`,
  per-test scratch БД; **production не затронута**):
  `TestPgMigration096` (первое применение + re-apply ×3; re-apply сохраняет
  overlap state, ingest events, variant states, results, conflict ledger;
  `runtime_health` принимает `OVERLAP_COVERAGE_DEGRADED` и по-прежнему
  отказывает неизвестные статусы), `TestPgOverlapCursor` (roundtrip;
  durability через НОВОЕ соединение; restart-эквивалентность — новый store
  видит `pass_active`/курсор и возобновляет проход; частичные обновления не
  регрессируют незатронутые поля).

### 8.6. Регрессия раунда 2 (реальные прогоны)

| Команда | Охват | Итог |
|---|---|---|
| `python -m pytest tests/test_me_entry_quality_h2_prospective_*.py` с `ME_H2PRO_PG_INTEGRATION=1` (scratch PG 17.2) | весь контур #312 + PG | **147 passed, 0 skipped, 0 failed** |
| `python -m pytest tests/test_me_entry_quality_h2_*v1.py` (без `prospective` в имени) + purity | PR #311 (чистый H2) + production isolation | **168 passed, 0 failed** (158 движок + 10 purity) |
| оба набора вместе | 315 tests | **315 passed** |

### 8.7. Изменённые файлы раунда 2

`runtime.py` (проходы, бюджеты, диагностика), `store.py` (durable overlap
state, merge-семантика частичных обновлений), `source_reader.py`
(ограничение верхней границы `to_captured_us`, `count_source_events_between`),
`config.py` (разделённые бюджеты), `telemetry.py`/`contracts.py` (8 метрик +
статус `OVERLAP_COVERAGE_DEGRADED`), `sql/migrations/096_…overlap_v1.sql` (новая),
`deploy/env/h2-prospective-runtime.env.example`, тесты:
`…_overlap_reconciliation_v1.py` (новый, 14), `…_pg_integration_v1.py` (+8),
`…_review_remediation_v1.py`, `…_runtime_v1.py`, `…_purity_v1.py`
(авторизованные поверхности + миграция 096).

Затронутые запрещённые поверхности — **нет**: `scanner_runner.py`, ME V2
detector, #305 source capture, V3 runtime, paper/live, regime gates, scoring,
trading settings не изменялись (проверено диффом и production isolation
тестами §2.6/§8.6).

---

## 9. Итоговый статус (раунд 2)

`IMPLEMENTATION_READY / RE_REVIEW_REQUIRED / ACTIVATION_HOLD`

* Overlap-сканирование имеет устойчивый прогресс (durable cursor + бюджеты).
* Forward и overlap не блокируют друг друга (сценарии 3–4).
* Restart recovery подтверждён (сценарии 5–6, PG restart-эквивалентность).
* Потерь внутри документированных границ гарантии нет; границы и их нарушение
  фиксируются явно (FR-05, сценарии 10, 12).
* Полный regression suite проанализирован относительно базы `388e4ba` (§3).
* Ограничения и риски отражены в §5 и §8.2.
* Не выполнялось (запрещено): merge PR #313, deploy, установка/запуск
  systemd, изменение `freeze_ts` #305, backfill, изменение production
  PostgreSQL, реальные запросы Bybit, активация H2, утверждение
  execution/outcome policy. PR остаётся в Draft.

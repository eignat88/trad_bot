# HTF_BASELINE_SHORT_PHASE2_LIVE_BYBIT_SOURCE_V1

Родительская задача: GitHub Issue #181 (parent: Issue #176 — Phase-2 observe-only
observer). Статус прод-активации: **BLOCKED (gate #181 закрыт)**.

## Цель

Причинный источник закрытых Bybit 5m-свечей для `Phase2ObserverRunner`, пригодный
для непрерывного prospective OOS с сохранением стабильной идентичности эпизодов.

## Контракт источника

Модуль: `app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py`.

| Свойство | Значение |
|---|---|
| Эндпоинты | только публичные READ-ONLY `/v5/market/time`, `/v5/market/kline` (category=linear, interval=5) |
| API | `BybitPhase2CandleSource(client, ...).load(symbol) -> list[Candle]` |
| Доказательство закрытия свечи | серверное время Bybit (`payload["time"]`), `evaluation_asof_ms = min(requested, server_time)`; локальные часы **никогда** не являются единственным доказательством закрытия |
| Незакрытые свечи | исключаются из выдачи (`unclosed_opens`), окно заканчивается на последней закрытой свече |
| Пагинация | строго ограниченная: `page_limit` (200) × `max_pages`; невырожденные, непересекающиеся окна запроса; превышение → `MAX_PAGES_EXCEEDED` |
| Полная история | `min_history_bars` (по умолчанию `max(3000, warmup + 200h*12 + 200)`), иначе `INCOMPLETE_HISTORY` |
| Валидация рядов | выравнивание по 5m, `CANDLE_NOT_ALIGNED`, `INVALID_OHLC`, `CANDLE_OUT_OF_WINDOW`, `DUPLICATE_CANDLE`, `CONFLICTING_CANDLE` — всё fail-closed |
| Пропуски свечей | ограниченный повторный запрос (`gap_recovery_attempts`, бюджет `max_gap_windows × gap_recovery_attempts`, backoff); невосстановленный пропуск → `INCOMPLETE_CANDLE_COVERAGE` |
| Ошибки API / rate limit | `BYBIT_API_ERROR` / `RATE_LIMITED`: до `max_attempts` с backoff, затем `CandleSourceError` → блокировка символа, без сигналов |
| Дедупликация / first-seen | сохранены без изменений (append-only persistence, `source_event_id` идемпотентен) |

## Стабильная идентичность и курсоры

* `episode_id = sha256(experiment_id|symbol|direction|level_id|first_touch_open_ms)`,
  `level_id = sha256(symbol|direction|formation_close_ms|available_ms|price)` —
  оба контент-anchored и **не зависят от rolling-окна** (requirement 6: смена/вытеснение
  окна не пересоздаёт эпизоды и не меняет идентификаторы).
* Курсор чекпойнта — **табличный курсор-таймстемп**:
  `cursor_schema = HTF_P2_CURSOR_V2_TIMESTAMP`, `cursors[symbol] = {"last_processed_open_ms": int}`.
  Индекс в переменном списке больше не используется.
* Legacy-чекпойнты (`HTF_P2_CURSOR_V1_INDEX`, int-индекс) мигрируют контролируемо:
  `last_processed_open_ms = last_close_ms - BAR_MS`; непроецируемый курсор →
  `CheckpointCursorError` (fail-closed), каждая миграция записана в
  `cursor_migrations` (аудит). Неизвестная схема → отказ загрузки чекпойнта.
* Resume по таймстемпу зажимается durable-состоянием (`min(state.last_close_ms, cursor+BAR)`);
  окно источника, начинающееся после resume-точки → карантин `RESUME_GAP_SOURCE_WINDOW`
  (символ блокируется, история не «перебазируется»).

## Каноническая история

Источник обязан доставить достаточно закрытых 5m-баров для причинного 1H
resistance (200 закрытых часов) и 200-барного ATR. При достаточной истории
`MIN_HTF_BARS`/level-universe считаются канонично; эпизоды при сдвиге окна не
пересоздаются.

## Durable cursor-aware backfill (FIX-1, review 5613552270)

`load(symbol, *, resume_open_ms=None)` принимает безопасную resume-точку из
durable timestamp-курсора (**никогда** не из индекса списка):

* **steady-режим** (`resume_open_ms is None` или курсор внутри steady-окна):
  выдаётся последнее `min_history_bars` закрытых баров — без изменений;
* **backfill-режим** (курсор левее `steady_start` — простой наблюдателя дольше окна
  источника): планируется **ограниченная партия**
  `[resume − (min_history_bars−1) .. min(resume + batch_bars−1, end_open)]` —
  полный причинный lookback для 1H resistance и ATR плюс не более `batch_bars`
  новых баров, где `batch_bars = min(max_backfill_bars, max_pages×page_limit −
  min_history_bars)`; при `batch_bars < 1` → `REQUEST_WINDOW_INVALID`
  (lookback никогда не сжимается молча);
* восстановление идёт **последовательно партиями** от старого курсора к новому
  ASOF; каждый диапазон обязан пройти проверку полноты (пагинация + `expected`)
  до входа в episode-машину; чекпойнт продвигается только по фактически
  обработанным барам;
* запрещено: молча продвигать курсор через пропуск, сбрасывать состояние эпизодов,
  объявлять новый OOS-старт от края нового окна, пере-атрибутировать подтверждённые
  эпизоды, выдавать прошлые события как свежие перспективные OOS;
* если корректность восстановления доказать нельзя — fail-closed
  `SOURCE_BLOCKED`/`QUARANTINED` с явной причиной; курсор и идентичность эпизодов
  сохраняются (`RESUME_GAP_SOURCE_WINDOW` остаётся предохранителем);
* бюджет: запросы/страницы/ретраи ограничены на каждый цикл (`max_pages`,
  `max_attempts`, `gap_recovery_attempts × max_gap_windows`); исторический
  backfill для восстановления состояния отделён от права новых post-freeze OOS-
  событий; любой first-touch или signal-time, упавший на строгую freeze-границу,
  исключается.

`last_load_metrics` фиксирует `backfill`, `resume_open_ms`,
`window_start_open_ms`/`window_end_open_ms`, `batch_bars`; раннер считает
`backfill_batches`/`backfill_bars` и передаёт `resume_open_ms` источнику
(параметр опционален — старые источники совместимы).

**Оговорка эквивалентности:** touch-count уровня считается в пределах доставленного
lookback; доказательство эквивалентности «без перерыва vs checkpoint/restart/
backfill» действительно, когда структура уровней целиком лежит в доставленном
lookback (симуляция гарантирует это одинаковой последовательностью окон).

## Выбор источника (opt-in)

`--candle-source csv|bybit` (по умолчанию `csv` — замороженный детерминированный
replay для тестов). `bybit` доступен только через явный opt-in и конструируется
в `build_candle_source`; при любой ошибке конструирования запуск завершается
`EXIT_NOT_RUNNABLE` без записей.

## Блокировка символа (fail-closed)

Ошибка источника для символа: `SOURCE_BLOCKED` в coverage-отчёте, курсор не
двигается, события не создаются, остальные символы продолжают работать.
CSV-путь использует тот же контракт (`CSV_SOURCE_ERROR`).

## Аудиты (dry-run)

Инструмент: `tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py`
(RESEARCH_ONLY, `InMemoryWriter`, временный чекпойнт, **0 обращений к production
БД**). Отчёт последнего прогона:
`docs/research/HTF_BASELINE_SHORT_PHASE2_BYBIT_SOURCE_V1_DRY_RUN_REPORT.json`:

* `causal_asof` — PASS: ни одно решение не смотрит за верифицированный ASOF
  (`known_at_ms <= evaluation_asof_ms`, `level_available_ms <= first_touch_open_ms < known_at_ms`);
* `bounded_requests` — PASS: строгий бюджет `loads × symbols × (max_attempts ×
  (1 + max_pages) + max_gap_windows × gap_recovery_attempts)` с точным счётчиком
  всех обращений;
* `checkpoint_restart_equivalence` — PASS: рестарт не обрабатывает бары повторно
  (`bars_reprocessed = 0`), не создаёт новых событий, состояние неизменно;
* `recovery_status` (опционально, `--recovery-simulate-downtime-bars`) —
  симуляция простоя дольше окна источника: bounded backfill партиями, затем
  сравнение «без перерыва vs checkpoint/restart/backfill» на идентичном
  историческом наборе (одинаковые `source_event_id`/`episode_id`/
  `first_touch_open_ms`/`known_at_ms`, без повторных FIRST_CONFIRM, без потерь).

### Fail-closed вердикт готовности (FIX-2, review 5613552270)

`overall_verdict == "PASS"` возможен **только** если одновременно:
`source_errors == 0`, `symbols_blocked == 0`, `quarantine_bars == 0`, каждый
запрошенный символ обработан с подтверждённым полным покрытием
(`symbol_status[sym].status == "OK"`, `coverage == "COMPLETE"`), все аудиты
(`causal_asof`, `bounded_requests`, `checkpoint_restart_equivalence`) PASS,
верифицированный ASOF присутствует, recovery-эквивалентность (если запрошена)
PASS. Успех одного символа никогда не маскирует провал другого. Любое нарушение →
`FAIL` с конкретным `readiness_blockers` (`SOURCE_BLOCKED_<SYM>`,
`QUARANTINED_<SYM>`, `INCOMPLETE_COVERAGE_<SYM>`, `SYMBOL_NOT_PROCESSED_<SYM>`,
`SOURCE_ERRORS_PRESENT`, `SYMBOLS_BLOCKED_PRESENT`, `QUARANTINE_BARS_PRESENT`,
`CAUSAL_ASOF_FAILED`, `REQUEST_BUDGET_EXCEEDED`, `RESTART_EQUIVALENCE_BROKEN`,
`MISSING_VERIFIED_ASOF`, `RECOVERY_EQUIVALENCE_BROKEN`). JSON-отчёт содержит
`symbol_status`, `source_errors`, `symbols_blocked`, `quarantine_bars`,
`coverage_status`, `recovery_status`, `readiness_blockers`, `overall_verdict`
(`verdict` сохранён как алиас). Exit code: 0 только при PASS.

## Запрещено (явно)

* Активация OOS / paper / live — запрещена; gate #181 остаётся закрытым.
* Применение миграций 074/075 в этом PR — запрещено.
* Установка/запуск systemd-observer — запрещена.
* Изменение `APPROVED_FREEZE_TS` — запрещено.
* Создание production OOS-наблюдений — запрещено без отдельного разрешения.
* Изменение замороженного Point-B стека, сканера, paper/evaluator и существующего
  OOS — запрещено (контролируется `tests/test_htf_baseline_short_phase2_isolation_v1.py`).

## Тесты

* `tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py` — юнит-матрица
  источника (forming-candle, boundary-after-close, пагинация 200+, пропуски/дубликаты/
  OHLC-конфликты, ошибки API, rate limit, локальные часы никогда не решают).
* `tests/test_htf_baseline_short_phase2_bybit_runner_v1.py` — интеграционная матрица
  (CSV-parity, rolling-window eviction, restart+overlap без дубликатов FIRST_CONFIRM,
  warmup/pre-freeze не становятся чистым OOS, блокировка символа, миграция курсора,
  checkpoint/restart equivalence, causal ASOF audit).
* `tests/test_htf_baseline_short_phase2_bybit_backfill_v1.py` — FIX-1 матрица
  (простой короче/длиннее окна, многочастотное восстановление партиями, пропуск
  свечи и OHLC-конфликт внутри backfill → fail-closed, rate limit не двигает курсор,
  restart посреди backfill без потерь/дубликатов, overlap диапазонов без повторного
  FIRST_CONFIRM, стабильность episode identity при вытеснении окна, pre-freeze
  события не становятся чистым OOS, first-touch до freeze при подтверждении после —
  не eligible, недостаточный HTF/ATR lookback → fail-closed).
* `tests/test_htf_baseline_short_phase2_bybit_dry_run_v1.py` — FIX-2 матрица
  (все символы валидны → PASS; `SOURCE_BLOCKED`/`QUARANTINED`/неполное покрытие →
  FAIL; `run_once()==EXIT_OK` при `source_errors > 0` → FAIL; сломанная
  restart-эквивалентность → FAIL; превышение бюджета запросов → FAIL; отсутствие
  верифицированного ASOF → FAIL).

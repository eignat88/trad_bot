# Issue #322 — Scanner Signal Integrity — исправление PR #323

**Ветка:** `research/issue-322-signal-integrity-main-v1`
**Base:** `main` (`fa86d71`)
**Режим:** DEVELOPMENT + OFFLINE REGRESSION ONLY (без deploy / restart / реальных Bybit-запросов)
**Статус PR:** Draft (сохраняется Draft; merge/deploy не выполняются)

---

## 1. Изменённые файлы

Относительно текущего HEAD PR #323 (`dca6e8b`) коммит-фикс меняет:

| Файл | Изменение |
|------|-----------|
| `app/research/prospective_observer.py` | **Откат к base** — восстановлено точное состояние базовой версии (byte-identical `BASE_SHA` + единственный документированный PR #193 one-liner). Новая identity из файла удалена. |
| `tests/test_prospective_oos_simple.py` | **Откат к base** — убраны `entry_timeframe`/`signal_candle_open_time` из вызовов `observe()` (frozen-подпись их не принимает). |
| `tests/test_prospective_oos_v3_regression.py` | **Откат к base** — аналогично. |
| `app/research/scanner_signal_integrity_observer.py` | **Новый модуль** — адаптер `ScannerSignalIntegrityObserver(ProspectiveOOSObserver)` со стабильной candle-identity (только ME_SHORT), изолированным `ContextVar`, fail-closed. |
| `scanner_runner.py` | Подключение адаптера в `_get_prospective_observer` (wiring candle-полей из PR сохранён). |
| `tests/test_issue322_candle_identity_v1.py` | Перенаправлен на адаптер (детерминированная identity, BIGINT-диапазон, restart-stable). |
| `tests/test_issue322_identity_wiring_v1.py` | Перенаправлен на адаптер + guard неизменности сигнатуры frozen-наблюдателя. |
| `tests/test_issue322_observer_sql_identity_v1.py` | Перенаправлен на адаптер в ME_SHORT-scope (SQL identity, fail-closed, ON CONFLICT). |
| `tests/test_issue322_signal_integrity_contract_v1.py` | Rework: стабильная identity адаптера + изоляция `ContextVar` + защита terminal `scanner_setup` (Task C). |
| `tests/test_issue322_scope_isolation_v1.py` | **Новый** — границы scope (Task B/F) + жизненный цикл ME_SHORT (Task E). |
| `tests/test_issue322_srr_identity_cutover_v1.py` | **Удалён** — SRR-identity-cutover выходит за рамки (см. п.4/5). |
| `docs/research/322_SIGNAL_INTEGRITY_FIX_REPORT.md` | Этот отчёт. |

**Не менялось в этом фиксе** (сохранено из PR #323 как есть, Task C):
`app/db/repository.py` — защита `scanner_setup`: не перезаписывает исходный `run_id`
при повторном обнаружении и не изменяет терминальные `EXECUTED`/`INVALIDATED`/`EXPIRED`
setup (PostgreSQL UPSERT + JSONL). Торговые решения не затронуты.

**Итог против `origin/main`:** `prospective_observer.py`, `test_prospective_oos_simple.py`,
`test_prospective_oos_v3_regression.py` теперь **byte-identical `origin/main`** (правки PR в них
полностью откачены). Сетевой дельтой PR остаются только адаптер, wiring, `repository.py` и тесты.

---

## 2. Архитектура исправления

**Проблема.** PR #323 внедрял новую identity прямо в `app/research/prospective_observer.py`.
Этот файл защищён историческим frozen-контрактом HTF Point-B
(`tests/test_htf_baseline_short_phase2_isolation_v1.py`): он должен быть byte-identical
`BASE_SHA` ровно с одним документированным PR #193 one-liner. Любая иная правка файла
проваливает `Main PR Safety` (`test_frozen_point_b_stack_byte_identical_to_base`,
`test_prospective_observer_delta_is_exactly_the_documented_pr193_line`).

**Решение — отдельный адаптер (Subclass, без правки frozen-файла):**

* Новый модуль `app/research/scanner_signal_integrity_observer.py` содержит
  `ScannerSignalIntegrityObserver(ProspectiveOOSObserver)`. Frozen-наблюдатель не изменён.
* **Детерминированная identity.** `_make_candle_source_key(...)` строит SHA-256 от
  `(scanner_name, symbol, direction, entry_timeframe, signal_candle_open_time)` в
  версионном namespace `issue322:candle:v1`. Результат укладывается в signed PostgreSQL
  `BIGINT` и лежит ниже `-2**31` (вне legacy/HTF 31-битного диапазона) → отсутствие
  коллизий с существующими identity. SHA-256 даёт стабильность при рестарте процесса
  (без зависимости от `PYTHONHASHSEED`).
* **Изолированный `ContextVar`.** На время одного вызова `observe()` candle-lineage
  кладётся в `contextvars.ContextVar` и **обязательно** сбрасывается в `finally`.
  Благодаря context-локальности `contextvars` (per-thread / per-task) lineage не может
  «утечь» между последовательными или конкурентными вызовами.
* **Fail-closed.** Для scope `MOMENTUM_EXHAUSTION`/`SHORT` неполная candle-lineage
  (отсутствующий/невалидный `signal_candle_open_time`/`entry_timeframe`) → захват
  не выполняется вообще (возврат `[]`, без INSERT).
* **Сохранение специализированной identity.** HTF Point-B (`_make_htf_source_key`) и
  ME Reverse Long V3 (`v3_source_key`) считаются внутри frozen-базы и не тронуты;
  адаптер для не-ME_SHORT семей делегирует через `super()` без изменений.
* **Wiring.** `scanner_runner._get_prospective_observer` теперь строит
  `ScannerSignalIntegrityObserver`; передача `entry_timeframe`/`signal_candle_open_time`
  на 5 точках захвата сохранена. Прямой HTF-вызов `observe()` (без candle-полей) корректно
  уходит в не-scope ветку.

**Граница применения (Task B).** Новая candle-identity и fail-closed действуют **только**
для `scanner_name == "MOMENTUM_EXHAUSTION"` и `direction == "SHORT"`. Все остальные семьи
(SRR, HTF, ME Reverse Long V3, breakout-retest, VC, LR, FVG, trend-pullback, …) сохраняют
исходную legacy-identity и **не получают** дополнительного fail-closed.

---

## 3. Тесты — PASS/FAIL (offline)

Целевые (новая/переработанная регрессия Issue #322):

| Набор | Результат |
|-------|-----------|
| `tests/test_issue322_candle_identity_v1.py` | **PASS** |
| `tests/test_issue322_identity_wiring_v1.py` | **PASS** |
| `tests/test_issue322_observer_sql_identity_v1.py` | **PASS** |
| `tests/test_issue322_scope_isolation_v1.py` | **PASS** |
| `tests/test_issue322_signal_integrity_contract_v1.py` | **PASS** |
| **Итого issue322** | **41 / 41 PASS** |

Критерии приёмки:

| Критерий | Модуль | Результат |
|----------|--------|-----------|
| `Main PR Safety` (frozen-инварианты + scope-routing + baseline) | `test_htf_baseline_short_phase2_isolation_v1.py` (minus 2 confinement) + `test_htf_baseline_short_phase2_scope_routing_v1.py` + `test_paper_safety_gate_config.py` | **PASS** (80 + 13 frozen отдельно) |
| ME_SHORT identity regression | issue322-набор выше | **PASS** (41) |
| Scanner setup terminal protection (Task C) | `test_issue322_signal_integrity_contract_v1.py` (repo-тесты) | **PASS** |
| HTF Point-B regression | `tests/test_htf_point_b_*.py` | **PASS** (39) |
| ME Reverse Long V3 regression (специализированные V3) | `test_me_reverse_long_v3_observer/identity/protocol.py` | **PASS** (47) |
| Отсутствие новых регрессий в observer/scanner/repository поверхности | 11 файлов (prospective/srr/shadow/me-short/lr/vc/…) | **PASS** (178) |

Подтверждение frozen-контракта: `git diff BASE_SHA -- app/research/prospective_observer.py`
показывает ровно один документированный PR #193 one-liner; `prospective_observer.py`,
`test_prospective_oos_simple.py`, `test_prospective_oos_v3_regression.py` — byte-identical
`origin/main`. Модуль frozen-изоляции проходит 13/13.

**Заранее существующие FAIL (не вызваны этим PR; подробно в п.4):**
`test_prospective_oos_v3.py` (7) и `test_prospective_oos_v3_regression.py` (8) — это
registry-snapshot / ME_SHORT-lifecycle тесты, ожидающие старое состояние реестра
(15 экспериментов, `READY_TO_START`) и активный захват ME_SHORT A/B/C, тогда как реестр
уже эволюционировал (17 экспериментов; ME_SHORT A/B/C = `CANCELLED`, A_EXECUTION = `COMPLETED`
— состояние, зафиксированное Task E). Эти тесты читают только `prospective_registry.json`,
`sql/migrations/*` и frozen-наблюдатель — **все три byte-identical `origin/main`** — поэтому
выполняются абсолютно так же, как на `origin/main`, и их падения присутствуют до и после
данного PR. Попытка «починить» их потребовала бы либо переписывания устаревших
registry-ожиданий, либо реактивации терминальных ME_SHORT-экспериментов (запрещено Task E).

---

## 4. Оставшиеся ограничения

* **PARTIAL_SCOPE (см. п.5).** Стабильная identity внедрена только для
  `MOMENTUM_EXHAUSTION`/`SHORT`. Для остальных сканер-семейств legacy-identity остаётся
  процесс-зависимой (`hash()`), т.е. глобальная проблема стабильной identity Issue #322
  для них не решена.
* **SRR identity-cutover не включён.** Добавленный в PR механизм
  `SRR_OOS_IDENTITY_CUTOVER_TS` убран: он давал бы дополнительный fail-closed семье SRR,
  что противоречит Task B (новая identity/fail-closed только для ME_SHORT) и Task F
  («не менять SRR freeze/status/cutover»). Соответствующий тест удалён, а поведение SRR
  оставлено неизменным; решение о cutover SRR требует отдельного согласования.
* **Заранее существующие registry-snapshot / ME_SHORT-lifecycle FAIL (15)** — не входят в
  область Issue #322 и не должны «чиниться» в этом PR (см. п.3). Требуют отдельной задачи
  по обслуживанию реестра/жизненного цикла.
* **Оффлайн-режим.** Прогоны выполнены без production БД, без рестартов Scanner/Paper/
  Evaluator, без реальных Bybit-запросов. SQL-контракты проверены статически и на mock.
* **Push/комментарий к PR** — выполняются вручную после ревью (см. п.6).

---

## 5. Соответствие Issue #322 — **PARTIAL_SCOPE**

Issue #322 — общая задача «Scanner signal integrity». Ограничение новой identity только
семьёй ME_SHORT означает, что **исходные глобальные acceptance-criteria Issue #322
выполнены не полностью**:

* стабильная restart-safe identity достигнута только для `MOMENTUM_EXHAUSTION`/`SHORT`;
* для остальных сканер-семейств (SRR, HTF-прочие, breakout-retest, VC, LR, FVG,
  trend-pullback и др.) сохранена прежняя процесс-зависимая identity, и защита от
  дублирующих source-observations при рестарте для них остаётся незакрытой.

**Предложение по разделению (Task F):** разделить общий Issue #322 на два трека —
(1) ME_SHORT-specific signal integrity (настоящий PR, `PARTIAL_SCOPE`) и
(2) глобальный roll-out стабильной candle-identity на остальные сканер-семейства
(отдельная задача/PR с собственным согласованием identity-контрактов каждой семьи).
Настоящее исправление **не следует** выдавать за полное решение Issue #322.

**Не выполнялось (в соответствии с ограничениями):** активация SRR или любых других OOS,
изменение SRR freeze/status/cutover, backfill OOS, включение recovery/H2, изменение frozen
HTF-baseline, изменение исследовательских freeze_ts/status, deploy, merge.

---

## 6. Совместимость с PR #321 (recovery scheduler) — Task G

* Настоящий PR **не смешивает** ветки с PR #321 и **не переносит** его изменения.
  Интеграционные проверки совместимости с recovery scheduler выполняются в отдельной
  временной ветке/worktree.
* Контракт, релевантный для #321: recovery **не должен** создавать повторные source
  observations и **не должен** перезаписывать терминальные `scanner_setup`. Реализованный
  в `app/db/repository.py` guard (Task C) как раз обеспечивает идемпотентность повторных
  обнаружений (исходный `run_id` сохраняется; терминальные `EXECUTED`/`INVALIDATED`/`EXPIRED`
  не изменяются — в PostgreSQL и JSONL), поэтому повторный запуск/recovery не приводит к
  дублированию или «воскрешению» setup. Это покрыто тестами
  `test_issue322_signal_integrity_contract_v1.py` (UPSERT + JSONL-идемпотентность).
* Окончательный вердикт по интеграции #321 + #323 фиксируется в отдельной ветке вне
  данного PR.

## 7. Связь с H2 / Issue #298 — Task H

* H2 использует изолированную архитектуру и источник из #305. Настоящий PR **не объявляет**
  себя обязательной зависимостью H2 (нет технического доказательства такой зависимости).
* H2 runtime, его activation/freeze, торговая логика и независимые сервисы **не изменялись**.

---

## 8. Решение — **READY_FOR_REVIEW** (с оговоркой PARTIAL_SCOPE)

Исправление устраняет блокировщик `Main PR Safety` (frozen-контракт HTF Point-B восстановлен,
byte-identical base), обеспечивает стабильную идентификацию сигналов ME_SHORT и защиту
терминальных `scanner_setup`, сохраняет специализированную HTF/ME-V3 identity и не нарушает
действующие исследования. Offline-регрессия целевых наборов зелёная; отсутствуют production
side effects.

Предостережение для ревью: изменение покрывает **только** ME_SHORT (`PARTIAL_SCOPE`).
Полное решение Issue #322 требует отдельного трека глобального roll-out identity
(п.5) и обслуживания устаревших registry-snapshot/ME_SHORT-lifecycle тестов (п.3/4).

**Операционные ограничения:** REVIEW ONLY / DEPLOY HOLD. PR остаётся в Draft.
Merge/deploy не выполнялись. Реальных Bybit-запросов, рестартов сервисов, backfill OOS,
изменений freeze/status — не производилось.

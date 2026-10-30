# BYBIT_RETRY_BUDGET_CI_V1 — Implementation Proposal & Acceptance Criteria

**Статус:** LOCAL CHANGES READY. Merge / deploy / activation / systemd / config / DB — НЕ выполнялись.
**Ветка:** `research/issue229-retry-budget-hardening-v1`; head коммит PR #233 — `76ddf5b8f77493f91c709ebc560b8d13ee33d6ab`
(`feat(exchange): unified Bybit retry budget and storm guard (#229)`).
**Hold gates (сохранены):** `API_BUDGET_NOT_CLEARED`, `V2_NO_GO`; PR #227 / #232 / #233 — Draft.

---

## 1. Проблема

`Main PR Safety` (`.github/workflows/main-pr-safety.yml`) запускается только для
`pull_request` с **base=main**. Stacked PR **#233** (`base=research/issue228-api-telemetry-v1`,
head `76ddf5b`) CI не получает: изменения #229 никогда не проходят автоматическую проверку,
а «прогнать просто main-ветку» не проверяет head-коммит #223/#233.

## 2. Дизайн

Новый отдельный workflow **`.github/workflows/bybit-retry-budget-ci-v1.yml`**.
`main-pr-safety.yml` **не изменяется** (AC1). Base PR #233 **не меняется** (остаётся
`research/issue228-api-telemetry-v1`).

### 2.1 Триггеры

| Триггер | Конфигурация | Назначение |
|---|---|---|
| `pull_request` | `branches: [main, 'research/**']`, **без paths-фильтра** | Автоматический запуск для stacked PR #233 (base `research/issue228-api-telemetry-v1`) и любого будущего re-stack на main. Без paths-фильтра осознанно: проверка должна следовать за head-коммитом PR, а не за набором затронутых файлов (suite офлайн и быстрый ~1 мин) |
| `workflow_dispatch` | обязательный input `head_sha`, опциональный `pr_number` | Документированный безопасный ручной запуск с привязкой к **точному head SHA** |

### 2.2 Гарантия «тестируется именно 76ddf5b, а не базовая ветка #228»

1. **Checkout точного head-коммита:** `actions/checkout` с
   `ref: ${{ github.event.pull_request.head.sha || inputs.head_sha }}` —
   проверяется head-коммит PR, а не merge-коммит и не `github.base_ref`;
2. **Provenance guard** (отдельный шаг, fail-fast):
   * `git rev-parse HEAD` обязан совпасть с запрошенным SHA (иначе run падает с ошибкой
     «refusing to report CI for the wrong commit»);
   * `git log -1` печатает SHA/дату/субъект для аудита;
   * маркеры Issue #229 в дереве: `app/exchange/retry_budget.py` существует и содержит
     `HARD_MAX_ATTEMPTS = 3`, `class RetryStormGuard`, `class OrderSubmitGuard`;
     `bybit_client.py` содержит `BybitRateLimitError`; существует
     `tests/test_bybit_retry_budget_v1.py`. На базе #228 этих файлов/маркеров нет →
     run на базовой ветке невозможен молча;
3. **PR-head enforcement (dispatch):** при заданном `pr_number` шаг сверяет
   `head_sha` с `GET /repos/{repo}/pulls/{pr_number}.head.sha` через `gh api`
   (permission `pull-requests: read`, только чтение) — ручной запуск привязан к
   актуальному head PR, а не к произвольному коммиту.

### 2.3 Состав проверок (offline fake transport, Python 3.12)

Python 3.12 — `actions/setup-python@v6` (конвенция Main PR Safety). Все тесты используют
ScriptedSession/mock-транспорт: **ни одного сетевого вызова Bybit/VPS/production, ни одной
БД**. Набор (198 тестов, локально зелёные за ~5 c):

| Файл | Что покрывает |
|---|---|
| `tests/test_bybit_retry_budget_v1.py` | RetryBudget: property «≤3 HTTP attempts на логический запрос» (819 последовательностей), классификация (10006/иные retCode/403/«access too frequent»/429), вложенные retry-лимиты (static contracts: нет L3-циклов в shadow/HTF/candle_sync/isolated_oos), backoff `[1,2]`-контракт, storm guard, breaker (P3 defer / P1-P2 wait / P0 after cooldown), **GET/POST order safety** (create_order: одна попытка, double-submit guard), telemetry parity |
| `tests/test_bybit_api_telemetry_v1.py` | BYBIT_API_TELEMETRY_V1: одна запись на HTTP-попытку, `attempt_index`/`retry_reason`/`total_attempts`, безопасность секретов |
| `tests/test_scanner_runtime.py` | Регрессия scanner: таймаут-ретраи и точное backoff-расписание |
| `tests/test_shadow_cli.py` | Shadow runners: L3-циклы удалены, fail-closed дропы |
| `tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py` | HTF candle source: одна логическая операция на fetch, fail-closed карантин |
| `tests/test_candle_sync.py` | candle_sync: коллапс скрытого retry-слоя |

Шаг **«Assert offline scope»** сравнивает список набранных файлов с allowlist
(`diff`): DB-зависимые suites (`prospective_oos`, `me_r_v3`, `postgres`, `pg8000`,
`migration_*`) не могут попасть в прогон молча. Дополнительно: `ME_R_V3_PG_INTEGRATION=false`,
`ME_R_V3_ATOMIC_E2E=false` (как в Main PR Safety), `PYTHONDONTWRITEBYTECODE=1`.

`permissions: contents: read, pull-requests: read`; `persist-credentials: false`;
`concurrency` с cancel-in-progress; `timeout-minutes: 15`; runner `ubuntu-24.04` —
конвенции репозитория.

## 3. Способы получить CI PASS/FAIL по head-коммиту #233 (без production)

### 3.1 Автоматический (основной)

1. Workflow коммитится в ветку `research/issue229-retry-budget-hardening-v1`
   (head PR #233) и пушется оператором (push — вне этой работы);
2. GitHub берёт workflow-файл из head-дерева PR → каждый `synchronize` PR #233 запускает
   **Bybit Retry Budget CI V1** против **точного head SHA**;
3. Результат: `gh pr checks 233` или `gh run list --workflow=bybit-retry-budget-ci-v1.yml`.

### 3.2 Ручной dispatch с привязкой к точному head SHA (после появления файла на default-branch)

```bash
gh workflow run bybit-retry-budget-ci-v1.yml \
  --ref research/issue229-retry-budget-hardening-v1 \
  -f head_sha=76ddf5b8f77493f91c709ebc560b8d13ee33d6ab \
  -f pr_number=233
gh run watch && gh run list --workflow=bybit-retry-budget-ci-v1.yml --limit 1
```

Ограничение GitHub: `workflow_dispatch` доступен только для workflow-файлов,
существующих на default-branch; до тех пор работает способ 3.1 / 3.3.

### 3.3 Локальный запуск, привязанный к точному SHA (работает немедленно)

```powershell
git fetch origin pull/233/head:pr233 76ddf5b8f77493f91c709ebc560b8d13ee33d6ab
git worktree add ../trad_bot_issue229_ci 76ddf5b8f77493f91c709ebc560b8d13ee33d6ab
Set-Location ../trad_bot_issue229_ci
if ((git rev-parse HEAD) -ne '76ddf5b8f77493f91c709ebc560b8d13ee33d6ab') { throw 'SHA mismatch' }
$env:PYTHONPATH='.'; $env:ME_R_V3_PG_INTEGRATION='false'; $env:ME_R_V3_ATOMIC_E2E='false'
python -m pytest -q -p no:cacheprovider `
  tests/test_bybit_retry_budget_v1.py tests/test_bybit_api_telemetry_v1.py `
  tests/test_scanner_runtime.py tests/test_shadow_cli.py `
  tests/test_htf_baseline_short_phase2_bybit_candle_source_v1.py tests/test_candle_sync.py --tb=short
```

(Аналогично на Linux/macOS: `git worktree add`, `git rev-parse HEAD`, тот же pytest-набор.)

## 4. Acceptance Criteria

| # | Критерий | Проверка | Статус |
|---|---|---|---|
| AC1 | `main-pr-safety.yml` не изменён | `git diff ad73530 -- .github/workflows/main-pr-safety.yml` пуст | ✅ |
| AC2 | Отдельный `BYBIT_RETRY_BUDGET_CI_V1` workflow существует; `pull_request` покрывает base `research/issue228-api-telemetry-v1` (auto для #233); `workflow_dispatch` требует `head_sha` | файл `.github/workflows/bybit-retry-budget-ci-v1.yml`, YAML парсится (проверено) | ✅ |
| AC3 | Run проверяет именно head-коммит: checkout по `pull_request.head.sha`/`head_sha` + provenance guard (HEAD == SHA; маркеры #229: `HARD_MAX_ATTEMPTS = 3`, `RetryStormGuard`, `OrderSubmitGuard`, `BybitRateLimitError`, `tests/test_bybit_retry_budget_v1.py`) | шаг `Provenance guard` — fail-fast | ✅ |
| AC4 | Dispatch привязан к head PR: `pr_number` сверяется с `pulls/{n}.head.sha` через `gh api` | шаг provenance guard | ✅ |
| AC5 | Покрытие: RetryBudget ≤3 attempts, nested retry limits (static contracts), GET/POST order safety, telemetry parity, shadow runners, HTF candle source | таблица §2.3; локально 198 passed | ✅ |
| AC6 | Python 3.12; offline fake transport; DB-зависимые тесты не выполняются | setup-python `'3.12'`; allowlist-дифф collected-файлов; env `ME_R_V3_*` | ✅ |
| AC7 | Base PR #233 не меняется; нет merge/deploy/activation/DB/systemd изменений | PR #233 base = `research/issue228-api-telemetry-v1`; изменения только в `.github/workflows/` + `docs/research/` | ✅ |
| AC8 | Hold gates сохранены: `API_BUDGET_NOT_CLEARED`, `V2_NO_GO`, PR #227/#232/#233 Draft | реестры не тронуты | ✅ |

## 5. Non-goals / ограничения

* workflow-файл активируется на GitHub только после push ветки оператором (здесь — локально);
* VPS read-only measurement (AC из #229) — после deploy с operator GO, вне этой работы;
* настоящая проверка «CI PASS/FAIL» требует запуска на GitHub; локально подтверждены:
  валидность YAML, корректность шагов (маркировки/allowlist прогнаны), зелёность
  точного тест-набора (198 passed, Python 3.13.14 локально; CI использует 3.12 —
  код совместим: без синтаксиса новее 3.10).

## 6. Rollback

Удаление `.github/workflows/bybit-retry-budget-ci-v1.yml` (config-compatible: ничего не
активировано, Main PR Safety не изменён, других workflow не касается).

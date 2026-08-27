# Scanner profitability improvement plan

Цель: превратить текущий scanner prototype в проверяемого и безопасного paper/live бота. Приоритет — не добавлять новые сетапы, а измерять фактический результат каждого сигнала.

## Phase 1 — Operations baseline

Status: implemented.

- Scheduled task `BybitScanner`: стартует при включении компьютера.
- Scheduled task `BybitScannerStop`: останавливает `BybitScanner` и `BybitPaperRunner` в 18:00 по будням, Monday-Friday.
- Scheduled task `BybitPaperRunner`: стартует paper-trading gate при включении компьютера.
- Runner грузит проектный `config.yaml`/`.env` независимо от текущей working directory.
- Intermediate tests: scheduler command tests.

Manual verification after install:

```powershell
.\install_task.bat
schtasks /Query /TN "BybitScanner" /FO LIST
schtasks /Query /TN "BybitScannerStop" /FO LIST
schtasks /Run /TN "BybitScanner"
Get-Content .\logs\scanner.log -Tail 20
schtasks /End /TN "BybitScanner"
```

Expected log after start:

```text
scanner config: universe_mode=dynamic top_n=50 ... config=D:\py_pro\trad_bot\config.yaml
scanner universe refreshed: 50 symbols
```

## Phase 2 — Signal quality guardrails

Status: implemented for new scanner outputs. Historical rows still need optional cleanup/reporting.

- Validate risk geometry before saving/showing a setup:
  - LONG: `invalidation_price < entry_zone_low`, `target_1 > entry_zone_high`.
  - SHORT: `invalidation_price > entry_zone_high`, `target_1 < entry_zone_low`.
- Mark invalid setups as `INVALIDATED` with `status_reason='INVALID_RISK_GEOMETRY'`, or do not persist them as tradable.
- Add tests for LONG/SHORT valid and invalid geometry.

Acceptance checks:

```sql
SELECT COUNT(*) FROM dds.scanner_setup
WHERE (direction='LONG' AND invalidation_price >= entry_zone_low)
   OR (direction='SHORT' AND invalidation_price <= entry_zone_high);
```

New invalid tradable count should be `0`.

## Phase 3 — Outcome evaluator

Status: core evaluator, storage schema, repository persistence, batch CLI and hourly scheduled backfill launcher implemented. Remaining work: richer expectancy reports on top of accumulated outcomes.

Add a module that evaluates every saved setup against subsequent candles.

Required fields per setup outcome:

- `entry_touched`
- `first_event`: `NO_ENTRY`, `TP1`, `TP2`, `SL`, `EXPIRED`, `OPEN`
- `result_r`
- `mfe_r`
- `mae_r`
- `bars_to_entry`
- `bars_to_exit`
- `fee_slippage_adjusted_result_r`

Suggested table: `dds.signal_outcome` keyed by `setup_id`.

Acceptance metrics:

- Every setup older than its evaluation horizon has exactly one outcome row.
- Outcome evaluator is deterministic and testable using synthetic candles.
- No look-ahead: only candles after `signal_candle_open_time` are used.

## Phase 4 — Scanner ranking by expectancy

Status: `dds.scanner_expectancy`, `dds.scanner_symbol_expectancy`, `dds.scanner_regime_expectancy`, `dds.score_bucket_expectancy`, and `dds.scanner_confluence_expectancy` views implemented. CLI report command available via `python -m app.scanners.expectancy_report`; it includes confluence-level grouping. Filter-based gating is implemented in the scanner and paper gate.

After outcomes exist, rank signals by measured edge, not raw score.

Compute grouped reports by:

- scanner_name
- symbol
- direction
- market_regime
- score_bucket
- scanner confluence count

Minimum useful columns:

- samples
- win_rate
- avg_r
- median_r
- profit_factor
- max_drawdown_r
- avg_mfe_r
- avg_mae_r

A signal becomes tradeable only when it passes filters such as:

```text
samples >= 30
avg_r > 0
profit_factor >= 1.2
valid_risk_geometry = true
market_regime allowed
```

## Phase 4b — Expectancy filter

Status: implemented. `ExpectancyFilter` loads historical R from `dds.scanner_expectancy` and rejects scanner/direction combinations with negative avg_r_after_costs. Controlled by `expectancy_filter_enabled`, `expectancy_min_avg_r`, and `expectancy_min_samples` in settings.

## Phase 5 — Paper trading gate

Status: core paper gate implemented. `paper_runner.py` and `python -m app.paper.cli run-once` read only recent `READY_TO_TRADE` setups, applies the expectancy filter before entry, validates risk configuration and enforces geometry/position/exposure/daily-loss/loss-streak limits, models fees and slippage, persists and restores trade/account state and entry-timeframe-specific expiry across restarts, and has deterministic entry/exit tests. Creating `data/PAPER_TRADING_STOP` is an emergency kill switch that blocks new paper entries while monitoring exits. It remains a forward-test tool; Funding is settled every configured interval from the latest observed Bybit funding rate and persisted per trade. Live-trading execution remains intentionally out of scope. Use `python -m app.paper.cli readiness` to evaluate the accumulated forward-test data against the explicit live-gate thresholds.

Only after Phase 3-4:

- Paper-execute filtered signals.
- Include fees, slippage, funding.
- Track daily loss, exposure, max concurrent positions.
- Disable live mode until paper forward-test is positive for at least 2-4 weeks.

## Phase 6 — Live trading gate

Status: `LiveExchange` enforces the persisted paper forward-test thresholds in addition to the explicit live-mode flags, so changing configuration alone cannot bypass the gate.

Live trading stays disabled unless all are true:

- positive out-of-sample expectancy;
- stable paper results;
- max drawdown within limit;
- order reconciliation implemented;
- emergency stop works;
- Telegram/alerting works.

# MOMENTUM_EXHAUSTION_REVERSE_LONG V1/V2 Outcome Feature Analysis

> Auto-generated report: pending execution against VPS PostgreSQL
> Status: **SCHEMA RESEARCH COMPLETE — AWAITING VPS DATA**

---

## 1. Executive Summary

Comparative analysis of `MOMENTUM_EXHAUSTION_REVERSE_LONG_V1` and `V2` scanners to identify features that allow V2 to filter out losing entries without sacrificing most winning trades.

**Key difference**: V2 adds a single filter: `rsi_delta_3 > 0` (RSI(14) must be rising at detection time).

**Goal**: Find additional filters that reduce `-1R` losses while preserving profitable trades.

---

## 2. Data Sources

### 2.1 Primary Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `dds.scanner_setup` | Signal/setup records | `setup_id`, `scanner_name`, `scanner_version`, `symbol`, `direction`, `features` (JSONB), `detected_at`, `reference_price`, `entry_zone_low/high`, `invalidation_price`, `target_1`, `score`, `market_regime`, `status` |
| `dds.paper_trade` | Paper trading outcomes | `trade_id`, `setup_id`, `scanner_name`, `symbol`, `direction`, `entry_price`, `exit_price`, `stop_price`, `pnl_r`, `pnl_usdt`, `mfe`, `mae`, `exit_reason`, `status`, `entered_at`, `closed_at`, `duration_sec`, `execution_policy`, `features` (JSONB) |
| `dds.paper_shadow_trade` | Shadow/counterfactual trades | `shadow_trade_id`, `source_trade_id`, `source_setup_id`, `pnl_r`, `mfe_r`, `mae_r` |
| `dds.market_candle` | Historical OHLCV data | `instrument_id`, `timeframe`, `open_time`, `open`, `high`, `low`, `close`, `volume` |
| `dds.instrument` | Symbol registry | `instrument_id`, `symbol` |
| `dds.paper_account` | Account snapshots | `balance`, `max_drawdown` |

### 2.2 Analytics Layer

| Table/View | Purpose |
|------------|---------|
| `analytics.trade_fact` | Canonical trade fact with computed `pnl_r`, `mfe_r`, `mae_r` |
| `analytics.setup_fact` | Canonical setup snapshot |
| `analytics.trade_event` | Append-only trade lifecycle journal |
| `mart.scanner_performance` | Aggregated scanner stats |

### 2.3 Relationship Chain

```
dds.scanner_setup (signal detected)
    ↓ setup_id
dds.paper_trade (paper entry executed)
    ↓ trade_id
dds.paper_trade (closed with pnl_r, exit_reason)
    ↓ (optional)
dds.paper_shadow_trade (counterfactual experiments)
```

### 2.4 Available Features in `features` JSONB

From `dds.scanner_setup.features` and `dds.paper_trade.features`:

```json
{
    "exhaustion_magnitude": 0.0-1.0,
    "body_ratio": 0.0-1.0,
    "rsi_confirmation": 0.0-1.0,
    "volume_ratio": 0.0-1.0,
    "rr_ratio": 0.0-1.0,
    "stop_distance_atr": 0.0-1.0,
    "stop_loss_pct": 2.5,
    "take_profit_pct": 3.0,
    "hold_minutes": 240,
    "source_scanner": "MOMENTUM_EXHAUSTION",
    "source_direction": "SHORT",
    // V2-specific:
    "rsi_14": float,
    "rsi_14_3bars_ago": float,
    "rsi_delta_3": float,
    "rsi_period": 14,
    "rsi_timeframe": "5m",
    "entry_confirmation_rsi_rising": true
}
```

### 2.5 Recoverable Features (from market_candle, no look-ahead)

These can be computed from historical candles at signal time:

| Feature | Formula | Source |
|---------|---------|--------|
| `close_location` | `(close - low) / (high - low)` | Signal candle 5m |
| `wick_size` | `max(high - close, open - low)` | Signal candle 5m |
| `wick_to_atr` | `wick_size / ATR(14)` | Signal candle + ATR |
| `body_to_atr` | `abs(close - open) / ATR(14)` | Signal candle + ATR |
| `candle_range_to_atr` | `(high - low) / ATR(14)` | Signal candle + ATR |
| `ATR` | `ATR(14, 5m)` | Last 5m candle |
| `RSI` | `RSI(14, 5m)` | Last 5m candle (stored in features) |
| `volume_ratio` | `volume / SMA(volume, 10)` | Last 5m candle |
| `ema_distance` | `(close - EMA(20)) / EMA(20)` | Last 5m candle |
| `ema_slope` | `(EMA(20)_now - EMA(20)_5bars_ago) / EMA(20)_5bars_ago` | 5m candles |
| `previous_3bar_return` | `(close[-1] - close[-4]) / close[-4]` | 5m candles |
| `signal_candle_return` | `(close - open) / open` | Signal candle |
| `time_of_day` | `EXTRACT(HOUR FROM detected_at)` | scanner_setup.detected_at |

---

## 3. Sample Size

*Pending VPS connection — will populate after execution.*

| Metric | V1 | V2 |
|--------|----|----|
| Total trades | - | - |
| Period start | - | - |
| Period end | - | - |
| Unique symbols | - | - |
| Unique setups | - | - |

---

## 4. V1 Baseline

*Pending execution.*

| Metric | Value |
|--------|-------|
| trades | - |
| wins | - |
| losses | - |
| win_rate | - |
| net_pnl_usdt | - |
| avg_pnl_usdt | - |
| median_pnl_usdt | - |
| total_r | - |
| expectancy_r | - |
| median_r | - |
| profit_factor | - |
| avg_win_r | - |
| avg_loss_r | - |
| max_win_r | - |
| max_loss_r | - |

### V1 R Distribution

| Range | Count | % |
|-------|-------|---|
| ≤ -1R | - | - |
| (-1R, 0) | - | - |
| [0, +0.5R) | - | - |
| [+0.5R, +1R) | - | - |
| ≥ +1R | - | - |

---

## 5. V2 Baseline

*Pending execution.*

| Metric | Value |
|--------|-------|
| trades | - |
| wins | - |
| losses | - |
| win_rate | - |
| net_pnl_usdt | - |
| avg_pnl_usdt | - |
| median_pnl_usdt | - |
| total_r | - |
| expectancy_r | - |
| median_r | - |
| profit_factor | - |
| avg_win_r | - |
| avg_loss_r | - |
| max_win_r | - |
| max_loss_r | - |

### V2 R Distribution

| Range | Count | % |
|-------|-------|---|
| ≤ -1R | - | - |
| (-1R, 0) | - | - |
| [0, +0.5R) | - | - |
| [+0.5R, +1R) | - | - |
| ≥ +1R | - | - |

---

## 6. Winner vs Loser Features

*Pending execution.*

---

## 7. Repeat-Signal Analysis

*Pending execution.*

---

## 8. Candidate Filters

*Pending execution.*

---

## 9. Train/Validation Results

*Pending execution.*

---

## 10. V1 vs V2 Comparison

### 10.1 What changed between V1 and V2?

V2 adds exactly one filter: `rsi_delta_3 > 0`.

This means V2 rejects V1 setups where:
- RSI(14) on the current closed 5m bar is NOT rising compared to 3 bars ago
- i.e., bearish momentum is still accelerating or flat

### 10.2 What types of trades did V2 stop taking?

Hypothesis: V2 rejects trades where:
- RSI is still falling (bearish momentum not exhausted)
- Price hasn't started bouncing yet
- These are likely the "-1R" losers

### 10.3 Does V2 remove mostly losers or also winners?

*Pending execution.*

### 10.4 Did these metrics improve?

| Metric | V1 | V2 | Delta |
|--------|----|----|-------|
| expectancy_r | - | - | - |
| profit_factor | - | - | - |
| win_rate | - | - | - |
| mae_r (avg) | - | - | - |
| frequency of -1R losses | - | - | - |

### 10.5 Are there features that could still improve V2?

*Pending execution.*

---

## 11. Counterfactual Results

*Pending execution.*

---

## 12. Conclusions

*Pending execution.*

---

## 13. Recommended Next Experiment

*Pending execution.*

---

## Appendix: Analysis Scripts

All SQL/Python scripts used for this analysis are saved in:

```
scripts/analysis/me_reverse_long_v1_v2/
├── 01_sample_size.sql
├── 02_baseline_metrics.sql
├── 03_r_distribution.sql
├── 04_winner_loser_features.sql
├── 05_repeat_signal_analysis.sql
├── 06_candidate_filters.sql
├── 07_counterfactual_simulation.sql
└── run_analysis.py
```

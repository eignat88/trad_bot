#!/usr/bin/env python3
"""
ME_REVERSE_LONG_V1_V2_FILTER_VALIDATION_V2

Extended analysis of MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 and V2:
- RSI recovery from market_candle for all V1 trades
- Counterfactual V2-on-V1 (rsi_delta_3 > 0 filter applied to V1)
- MFE/MAE using paper_trade.mfe_r / mae_r
- Bidirectional threshold search (>= and <=)
- Detailed repeat trade groups
- Combination filters
- Chronological validation (70/30 split)

Usage:
    python scripts/analysis/me_reverse_long_v1_v2/run_analysis_v2.py

Requires:
    - PostgreSQL connection to trad_bot database
    - psycopg2 or similar PostgreSQL driver
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


REPORT_PATH = PROJECT_ROOT / "reports" / "ME_REVERSE_LONG_V1_V2_OUTCOME_FEATURE_ANALYSIS_V2.md"


def get_connection():
    """Get PostgreSQL connection from environment or config."""
    import os
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "trad_bot"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    conn.autocommit = False
    return conn


def run_query(conn, sql: str, params=None) -> list[dict]:
    """Execute query and return list of dicts."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def run_query_single(conn, sql: str, params=None) -> dict | None:
    """Execute query and return single dict or None."""
    results = run_query(conn, sql, params)
    return results[0] if results else None


def _safe_div(num, den, default=None):
    """Safe division avoiding ZeroDivisionError."""
    if den is None or den == 0:
        return default
    return round(num / den, 4) if num is not None else default


# ============================================================
# 1. SAMPLE SIZE
# ============================================================
def get_sample_size(conn) -> dict[str, Any]:
    sql = """
    SELECT
        scanner_name,
        COUNT(*) AS total_trades,
        COUNT(DISTINCT pt.symbol) AS unique_symbols,
        COUNT(DISTINCT pt.setup_id) AS unique_setups,
        MIN(pt.entered_at) AS period_start,
        MAX(pt.entered_at) AS period_end
    FROM dds.paper_trade pt
    WHERE pt.scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND pt.direction = 'LONG'
    AND pt.status = 'CLOSED'
    GROUP BY scanner_name;
    """
    return {row['scanner_name']: row for row in run_query(conn, sql)}


# ============================================================
# 2. BASELINE METRICS (with mfe_r, mae_r)
# ============================================================
def get_baseline_metrics(conn) -> dict[str, Any]:
    sql = """
    SELECT
        pt.scanner_name,
        COUNT(*) AS trades,
        SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN pt.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN pt.pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses,
        ROUND((SUM(CASE WHEN pt.pnl_r > 0 THEN 1.0 ELSE 0 END) / COUNT(*))::numeric, 4) AS win_rate,
        ROUND(SUM(pt.pnl_usdt)::numeric, 2) AS net_pnl_usdt,
        ROUND(SUM(pt.pnl_r)::numeric, 4) AS total_r,
        ROUND(AVG(pt.pnl_r)::numeric, 4) AS expectancy_r,
        CASE
            WHEN SUM(CASE WHEN pt.pnl_r < 0 THEN ABS(pt.pnl_r) ELSE 0 END) > 0
            THEN ROUND((
                SUM(CASE WHEN pt.pnl_r > 0 THEN pt.pnl_r ELSE 0 END)
                / SUM(CASE WHEN pt.pnl_r < 0 THEN ABS(pt.pnl_r) ELSE 0 END))::numeric, 4)
            ELSE NULL
        END AS profit_factor,
        ROUND(AVG(CASE WHEN pt.pnl_r > 0 THEN pt.pnl_r END)::numeric, 4) AS avg_win_r,
        ROUND(AVG(CASE WHEN pt.pnl_r <= 0 THEN pt.pnl_r END)::numeric, 4) AS avg_loss_r,
        MAX(pt.pnl_r) AS max_win_r,
        MIN(pt.pnl_r) AS max_loss_r,
        ROUND(AVG(pt.mfe_r)::numeric, 4) AS avg_mfe_r,
        ROUND(AVG(pt.mae_r)::numeric, 4) AS avg_mae_r,
        ROUND((AVG(pt.duration_sec) / 60)::numeric, 1) AS avg_hold_minutes
    FROM dds.paper_trade pt
    WHERE pt.scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND pt.direction = 'LONG'
    AND pt.status = 'CLOSED'
    GROUP BY pt.scanner_name;
    """
    return {row['scanner_name']: row for row in run_query(conn, sql)}


# ============================================================
# 3. R DISTRIBUTION
# ============================================================
def get_r_distribution(conn) -> dict[str, list]:
    sql = """
    WITH base AS (
        SELECT
            scanner_name,
            pnl_r,
            CASE
                WHEN pnl_r <= -1.0 THEN '<= -1R'
                WHEN pnl_r > -1.0 AND pnl_r < 0 THEN '(-1R, 0)'
                WHEN pnl_r >= 0 AND pnl_r < 0.5 THEN '[0, +0.5R)'
                WHEN pnl_r >= 0.5 AND pnl_r < 1.0 THEN '[+0.5R, +1R)'
                WHEN pnl_r >= 1.0 THEN '>= +1R'
            END AS r_bucket
        FROM dds.paper_trade
        WHERE scanner_name IN (
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
        )
        AND direction = 'LONG'
        AND status = 'CLOSED'
    )
    SELECT
        scanner_name,
        r_bucket,
        COUNT(*) AS count,
        ROUND((COUNT(*)::numeric / SUM(COUNT(*)) OVER (PARTITION BY scanner_name) * 100)::numeric, 1) AS pct
    FROM base
    GROUP BY scanner_name, r_bucket
    ORDER BY scanner_name, r_bucket;
    """
    results = run_query(conn, sql)
    by_scanner = {}
    for row in results:
        scanner = row['scanner_name']
        if scanner not in by_scanner:
            by_scanner[scanner] = []
        by_scanner[scanner].append(row)
    return by_scanner


# ============================================================
# 4. RSI RECOVERY: get RSI from market_candle for all V1 trades
# ============================================================
def get_rsi_recovery(conn) -> list[dict]:
    """Recover RSI(14) and rsi_delta_3 from 5m candles for V1 trades.

    Uses a simplified Wilder's RSI: average gain/loss over 14-bar rolling window.
    No look-ahead: only candles up to and including signal candle.
    """
    sql = """
    WITH v1_trades AS (
        SELECT
            pt.trade_id,
            pt.setup_id,
            pt.symbol,
            pt.scanner_name,
            pt.entered_at,
            pt.pnl_r,
            pt.pnl_usdt,
            pt.mfe_r,
            pt.mae_r,
            pt.exit_reason,
            pt.duration_sec,
            ss.detected_at AS signal_time,
            ss.signal_candle_open_time,
            ss.instrument_id,
            ss.features
        FROM dds.paper_trade pt
        JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
        WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
          AND pt.direction = 'LONG'
          AND pt.status = 'CLOSED'
    ),
    -- Convert signal_candle_open_time (bigint epoch seconds) to timestamptz
    signal_candles AS (
        SELECT
            vt.*,
            to_timestamp(vt.signal_candle_open_time) AS signal_candle_ts
        FROM v1_trades vt
    ),
    -- Get 5m candles from market.candle up to and including signal candle
    -- No look-ahead: open_time <= signal_time, is_closed = true
    candle_data AS (
        SELECT
            sc.trade_id,
            mc.open_time,
            mc.close,
            ROW_NUMBER() OVER (
                PARTITION BY sc.trade_id
                ORDER BY mc.open_time DESC
            ) AS candle_idx  -- 1 = signal candle, 2 = 1 bar before, etc.
        FROM signal_candles sc
        JOIN market.candle mc
            ON mc.instrument_id = sc.instrument_id
            AND mc.timeframe = '5'
            AND mc.is_closed = true
            AND mc.open_time >= sc.signal_candle_ts - INTERVAL '180 minutes'
            AND mc.open_time <= sc.signal_candle_ts
    ),
    -- Compute gains and losses for RSI
    candle_gains AS (
        SELECT
            trade_id,
            open_time,
            close,
            candle_idx,
            GREATEST(close - LAG(close) OVER (
                PARTITION BY trade_id ORDER BY open_time
            ), 0) AS gain,
            GREATEST(LAG(close) OVER (
                PARTITION BY trade_id ORDER BY open_time
            ) - close, 0) AS loss
        FROM candle_data
    ),
    -- Wilder's RSI using 14-bar rolling window
    rsi_raw AS (
        SELECT
            trade_id,
            open_time,
            candle_idx,
            AVG(gain) OVER w AS avg_gain,
            AVG(loss) OVER w AS avg_loss,
            COUNT(*) OVER w AS window_count
        FROM candle_gains
        WHERE gain IS NOT NULL
        WINDOW w AS (
            PARTITION BY trade_id
            ORDER BY open_time
            ROWS BETWEEN 14 PRECEDING AND CURRENT ROW
        )
    ),
    rsi_computed AS (
        SELECT
            trade_id,
            candle_idx,
            CASE
                WHEN avg_loss = 0 THEN 100.0
                WHEN avg_gain = 0 THEN 0.0
                ELSE ROUND((100.0 - 100.0 / (1.0 + avg_gain / avg_loss))::numeric, 4)
            END AS rsi_14
        FROM rsi_raw
        WHERE window_count >= 14
    ),
    -- Get RSI for signal candle (candle_idx = 1) and 3 bars before (candle_idx = 4)
    rsi_at_signal AS (
        SELECT
            trade_id,
            MAX(CASE WHEN candle_idx = 1 THEN rsi_14 END) AS rsi_signal,
            MAX(CASE WHEN candle_idx = 4 THEN rsi_14 END) AS rsi_3_bars_ago
        FROM rsi_computed
        GROUP BY trade_id
    )
    SELECT
        vt.trade_id,
        vt.symbol,
        vt.pnl_r,
        vt.pnl_usdt,
        vt.mfe_r,
        vt.mae_r,
        vt.exit_reason,
        vt.duration_sec,
        vt.entered_at,
        ra.rsi_signal AS rsi_14_at_signal,
        ra.rsi_3_bars_ago,
        CASE
            WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
            THEN ROUND((ra.rsi_signal - ra.rsi_3_bars_ago)::numeric, 4)
            ELSE NULL
        END AS rsi_delta_3,
        CASE
            WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
                 AND (ra.rsi_signal - ra.rsi_3_bars_ago) > 0
            THEN 'PASS'
            ELSE 'REJECT'
        END AS v2_filter_result,
        -- Coverage status
        CASE
            WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
            THEN 'RECOVERED'
            WHEN ra.rsi_signal IS NOT NULL
            THEN 'RSI_ONLY_NO_DELTA'
            ELSE 'MISSING'
        END AS recovery_status,
        NULLIF(vt.features->>'exhaustion_magnitude', '')::numeric AS exhaustion_magnitude,
        NULLIF(vt.features->>'body_ratio', '')::numeric AS body_ratio,
        NULLIF(vt.features->>'rsi_confirmation', '')::numeric AS rsi_confirmation,
        NULLIF(vt.features->>'volume_ratio', '')::numeric AS volume_ratio,
        NULLIF(vt.features->>'rr_ratio', '')::numeric AS rr_ratio,
        NULLIF(vt.features->>'stop_distance_atr', '')::numeric AS stop_distance_atr
    FROM v1_trades vt
    LEFT JOIN rsi_at_signal ra ON ra.trade_id = vt.trade_id
    ORDER BY vt.entered_at;
    """
    return run_query(conn, sql)


# ============================================================
# 5. RSI COVERAGE DIAGNOSTICS
# ============================================================
def get_rsi_coverage(conn) -> dict[str, Any]:
    """Check RSI recovery coverage for V1 trades."""
    sql = """
    WITH v1_trades AS (
        SELECT
            pt.trade_id,
            ss.signal_candle_open_time,
            ss.instrument_id
        FROM dds.paper_trade pt
        JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
        WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
          AND pt.direction = 'LONG'
          AND pt.status = 'CLOSED'
    ),
    signal_candles AS (
        SELECT
            vt.*,
            to_timestamp(vt.signal_candle_open_time) AS signal_candle_ts
        FROM v1_trades vt
    ),
    candle_counts AS (
        SELECT
            sc.trade_id,
            COUNT(mc.open_time) AS candles_found
        FROM signal_candles sc
        JOIN market.candle mc
            ON mc.instrument_id = sc.instrument_id
            AND mc.timeframe = '5'
            AND mc.is_closed = true
            AND mc.open_time >= sc.signal_candle_ts - INTERVAL '180 minutes'
            AND mc.open_time <= sc.signal_candle_ts
        GROUP BY sc.trade_id
    )
    SELECT
        (SELECT COUNT(*) FROM v1_trades) AS total_trades,
        COUNT(cc.trade_id) AS trades_with_candles,
        COUNT(cc.trade_id) FILTER (WHERE cc.candles_found >= 17) AS trades_enough_candles,
        ROUND((COUNT(cc.trade_id)::numeric / NULLIF((SELECT COUNT(*) FROM v1_trades), 0) * 100)::numeric, 1) AS coverage_pct
    FROM v1_trades vt
    LEFT JOIN candle_counts cc ON cc.trade_id = vt.trade_id;
    """
    return run_query_single(conn, sql)


# ============================================================
# 6. COUNTERFACTUAL: V2-on-V1 (rsi_delta_3 > 0 filter on V1)
# ============================================================
def get_counterfactual_v2_on_v1(rsi_data: list[dict]) -> dict[str, Any]:
    """Apply rsi_delta_3 > 0 filter to V1 trades (counterfactual V2)."""
    v1_all = [t for t in rsi_data]
    v1_pass = [t for t in rsi_data if t.get('v2_filter_result') == 'PASS']
    v1_reject = [t for t in rsi_data if t.get('v2_filter_result') == 'REJECT']

    def _metrics(trades: list[dict], label: str) -> dict:
        if not trades:
            return {'label': label, 'trades': 0}
        n = len(trades)
        wins = [t for t in trades if t['pnl_r'] > 0]
        losses = [t for t in trades if t['pnl_r'] <= 0]
        hard_losses = [t for t in trades if t['pnl_r'] <= -0.9]
        total_r = sum(t['pnl_r'] for t in trades)
        gross_win_r = sum(t['pnl_r'] for t in wins)
        gross_loss_r = sum(abs(t['pnl_r']) for t in losses)
        return {
            'label': label,
            'trades': n,
            'wins': len(wins),
            'losses': len(losses),
            'hard_losses': len(hard_losses),
            'win_rate': round(len(wins) / n, 4) if n else 0,
            'total_r': round(total_r, 4),
            'expectancy_r': round(total_r / n, 4) if n else 0,
            'profit_factor': round(gross_win_r / gross_loss_r, 4) if gross_loss_r > 0 else None,
            'avg_win_r': round(gross_win_r / len(wins), 4) if wins else 0,
            'avg_loss_r': round(-gross_loss_r / len(losses), 4) if losses else 0,
        }

    return {
        'V1_ALL': _metrics(v1_all, 'V1 ALL'),
        'V1_IF_RSI_DELTA_3_GT_0': _metrics(v1_pass, 'V1 if rsi_delta_3 > 0'),
        'V1_REJECTED': _metrics(v1_reject, 'V1 rejected by V2 filter'),
    }


# ============================================================
# 6. BIDIRECTIONAL THRESHOLD SEARCH
# ============================================================
def get_bidirectional_thresholds(conn) -> list[dict]:
    """Test both >= and <= thresholds for each feature on V1 trades."""
    features = [
        'exhaustion_magnitude', 'body_ratio', 'rsi_confirmation',
        'volume_ratio', 'rr_ratio', 'stop_distance_atr',
    ]
    thresholds_ge = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    thresholds_le = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

    results = []
    for feat in features:
        # >= thresholds
        for t in thresholds_ge:
            sql = f"""
            WITH base AS (
                SELECT
                    pt.trade_id,
                    pt.pnl_r,
                    NULLIF(ss.features->>'{feat}', '')::numeric AS feat_val
                FROM dds.paper_trade pt
                JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
                WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
                  AND pt.direction = 'LONG'
                  AND pt.status = 'CLOSED'
            ),
            filtered AS (
                SELECT
                    COUNT(*) AS trades,
                    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses,
                    ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_r,
                    ROUND((
                        SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END), 0))::numeric, 4
                    ) AS profit_factor
                FROM base
                WHERE feat_val >= {t}
            ),
            baseline AS (
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS total_losses,
                    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS total_hard_losses
                FROM base
            )
            SELECT
                '{feat} >= {t}' AS filter_name,
                b.total AS total_before,
                b.total_hard_losses AS hard_losses_before,
                f.trades AS total_after,
                f.wins AS wins_after,
                f.losses AS losses_after,
                f.hard_losses AS hard_losses_after,
                f.expectancy_r,
                f.profit_factor,
                ROUND(((b.total - f.trades)::numeric / NULLIF(b.total, 0) * 100)::numeric, 1) AS pct_removed,
                ROUND(((b.total_hard_losses - f.hard_losses)::numeric / NULLIF(b.total_hard_losses, 0) * 100)::numeric, 1) AS pct_hard_losses_removed
            FROM baseline b, filtered f;
            """
            row = run_query_single(conn, sql)
            if row:
                results.append(row)

        # <= thresholds
        for t in thresholds_le:
            sql = f"""
            WITH base AS (
                SELECT
                    pt.trade_id,
                    pt.pnl_r,
                    NULLIF(ss.features->>'{feat}', '')::numeric AS feat_val
                FROM dds.paper_trade pt
                JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
                WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
                  AND pt.direction = 'LONG'
                  AND pt.status = 'CLOSED'
            ),
            filtered AS (
                SELECT
                    COUNT(*) AS trades,
                    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses,
                    ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_r,
                    ROUND((
                        SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END), 0))::numeric, 4
                    ) AS profit_factor
                FROM base
                WHERE feat_val <= {t}
            ),
            baseline AS (
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS total_losses,
                    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS total_hard_losses
                FROM base
            )
            SELECT
                '{feat} <= {t}' AS filter_name,
                b.total AS total_before,
                b.total_hard_losses AS hard_losses_before,
                f.trades AS total_after,
                f.wins AS wins_after,
                f.losses AS losses_after,
                f.hard_losses AS hard_losses_after,
                f.expectancy_r,
                f.profit_factor,
                ROUND(((b.total - f.trades)::numeric / NULLIF(b.total, 0) * 100)::numeric, 1) AS pct_removed,
                ROUND(((b.total_hard_losses - f.hard_losses)::numeric / NULLIF(b.total_hard_losses, 0) * 100)::numeric, 1) AS pct_hard_losses_removed
            FROM baseline b, filtered f;
            """
            row = run_query_single(conn, sql)
            if row:
                results.append(row)

    return results


# ============================================================
# 7. DETAILED REPEAT TRADE ANALYSIS
# ============================================================
def get_repeat_detailed(conn) -> list[dict]:
    """Detailed repeat trade groups: first, after win, after loss, by time gap."""
    sql = """
    WITH symbol_trades AS (
        SELECT
            trade_id,
            scanner_name,
            symbol,
            pnl_r,
            entered_at,
            LAG(entered_at) OVER (
                PARTITION BY symbol, scanner_name
                ORDER BY entered_at
            ) AS prev_entered_at,
            LAG(pnl_r) OVER (
                PARTITION BY symbol, scanner_name
                ORDER BY entered_at
            ) AS prev_pnl_r,
            EXTRACT(EPOCH FROM (
                entered_at - LAG(entered_at) OVER (
                    PARTITION BY symbol, scanner_name
                    ORDER BY entered_at
                )
            )) / 60 AS gap_minutes
        FROM dds.paper_trade
        WHERE scanner_name IN (
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
        )
        AND direction = 'LONG'
        AND status = 'CLOSED'
    ),
    grouped AS (
        SELECT
            *,
            CASE
                WHEN prev_entered_at IS NULL THEN 'first_trade'
                WHEN prev_pnl_r > 0 THEN 'repeat_after_win'
                WHEN prev_pnl_r <= 0 THEN 'repeat_after_loss'
            END AS repeat_group,
            CASE
                WHEN prev_entered_at IS NULL THEN 'first'
                WHEN gap_minutes <= 60 THEN 'repeat_le_1h'
                WHEN gap_minutes <= 180 THEN 'repeat_1h_3h'
                ELSE 'repeat_gt_3h'
            END AS time_group
        FROM symbol_trades
    )
    SELECT
        scanner_name,
        repeat_group,
        time_group,
        COUNT(*) AS n,
        SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses,
        ROUND((SUM(CASE WHEN pnl_r > 0 THEN 1.0 ELSE 0 END) / COUNT(*))::numeric, 4) AS win_rate,
        ROUND(SUM(pnl_r)::numeric, 4) AS total_r,
        ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_r,
        CASE
            WHEN SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END) > 0
            THEN ROUND((
                SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                / SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END))::numeric, 4)
            ELSE NULL
        END AS profit_factor
    FROM grouped
    GROUP BY scanner_name, repeat_group, time_group
    ORDER BY scanner_name, repeat_group, time_group;
    """
    return run_query(conn, sql)


# ============================================================
# 8. COMBINATION FILTERS
# ============================================================
def get_combination_filters(conn) -> list[dict]:
    """Test 2-feature combinations on V1 trades."""
    combos = [
        ("rr_ratio >= 0.5 AND volume_ratio >= 0.3", "rr>=0.5 & vol>=0.3"),
        ("rr_ratio >= 0.5 AND volume_ratio >= 0.5", "rr>=0.5 & vol>=0.5"),
        ("rr_ratio >= 0.5 AND exhaustion_magnitude <= 0.5", "rr>=0.5 & exhaust<=0.5"),
        ("rr_ratio >= 0.5 AND exhaustion_magnitude <= 0.4", "rr>=0.5 & exhaust<=0.4"),
        ("exhaustion_magnitude <= 0.5 AND volume_ratio >= 0.3", "exhaust<=0.5 & vol>=0.3"),
        ("exhaustion_magnitude <= 0.4 AND rr_ratio >= 0.3", "exhaust<=0.4 & rr>=0.3"),
        ("body_ratio >= 0.5 AND rr_ratio >= 0.5", "body>=0.5 & rr>=0.5"),
        ("rsi_confirmation >= 0.3 AND rr_ratio >= 0.5", "rsi_conf>=0.3 & rr>=0.5"),
    ]

    results = []
    for cond, label in combos:
        sql = f"""
        WITH base AS (
            SELECT
                pt.trade_id,
                pt.pnl_r,
                NULLIF(ss.features->>'exhaustion_magnitude', '')::numeric AS exhaustion_magnitude,
                NULLIF(ss.features->>'body_ratio', '')::numeric AS body_ratio,
                NULLIF(ss.features->>'rsi_confirmation', '')::numeric AS rsi_confirmation,
                NULLIF(ss.features->>'volume_ratio', '')::numeric AS volume_ratio,
                NULLIF(ss.features->>'rr_ratio', '')::numeric AS rr_ratio,
                NULLIF(ss.features->>'stop_distance_atr', '')::numeric AS stop_distance_atr
            FROM dds.paper_trade pt
            JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
            WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
              AND pt.direction = 'LONG'
              AND pt.status = 'CLOSED'
        ),
        baseline AS (
            SELECT
                COUNT(*) AS total_before,
                SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_before,
                ROUND(AVG(pnl_r)::numeric, 4) AS er_before,
                ROUND((
                    SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END), 0))::numeric, 4
                ) AS pf_before
            FROM base
        ),
        filtered AS (
            SELECT
                COUNT(*) AS total_after,
                SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins_after,
                SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_after,
                ROUND(AVG(pnl_r)::numeric, 4) AS er_after,
                ROUND((
                    SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END), 0))::numeric, 4
                ) AS pf_after
            FROM base
            WHERE {cond}
        )
        SELECT
            '{label}' AS filter_name,
            b.total_before,
            b.hard_before,
            b.er_before,
            b.pf_before,
            f.total_after,
            f.wins_after,
            f.hard_after,
            f.er_after,
            f.pf_after
        FROM baseline b, filtered f;
        """
        row = run_query_single(conn, sql)
        if row:
            results.append(row)

    return results


# ============================================================
# 9. CHRONOLOGICAL VALIDATION (70/30 split)
# ============================================================
def get_chronological_validation(conn) -> list[dict]:
    """70% oldest = discovery, 30% newest = validation.
    Apply best single-feature thresholds found on discovery to validation."""
    # First, get all V1 trades ordered by entered_at
    all_trades = get_rsi_recovery(conn)
    if not all_trades:
        return []

    n = len(all_trades)
    split_idx = int(n * 0.7)
    discovery = all_trades[:split_idx]
    validation = all_trades[split_idx:]

    # Test key thresholds on discovery and validation
    thresholds = [
        ('exhaustion_magnitude >= 0.5', lambda t: t.get('exhaustion_magnitude') is not None and t['exhaustion_magnitude'] >= 0.5),
        ('exhaustion_magnitude <= 0.4', lambda t: t.get('exhaustion_magnitude') is not None and t['exhaustion_magnitude'] <= 0.4),
        ('exhaustion_magnitude <= 0.5', lambda t: t.get('exhaustion_magnitude') is not None and t['exhaustion_magnitude'] <= 0.5),
        ('volume_ratio >= 0.5', lambda t: t.get('volume_ratio') is not None and t['volume_ratio'] >= 0.5),
        ('volume_ratio >= 0.3', lambda t: t.get('volume_ratio') is not None and t['volume_ratio'] >= 0.3),
        ('rr_ratio >= 0.5', lambda t: t.get('rr_ratio') is not None and t['rr_ratio'] >= 0.5),
        ('rr_ratio >= 0.3', lambda t: t.get('rr_ratio') is not None and t['rr_ratio'] >= 0.3),
        ('stop_distance_atr >= 0.5', lambda t: t.get('stop_distance_atr') is not None and t['stop_distance_atr'] >= 0.5),
        ('rsi_delta_3 > 0', lambda t: t.get('v2_filter_result') == 'PASS'),
    ]

    def _metrics(trades):
        if not trades:
            return {'trades': 0, 'wins': 0, 'hard_losses': 0, 'win_rate': 0, 'expectancy_r': 0, 'profit_factor': None}
        n = len(trades)
        wins = sum(1 for t in trades if t['pnl_r'] > 0)
        hard = sum(1 for t in trades if t['pnl_r'] <= -0.9)
        total_r = sum(t['pnl_r'] for t in trades)
        gross_win = sum(t['pnl_r'] for t in trades if t['pnl_r'] > 0)
        gross_loss = sum(abs(t['pnl_r']) for t in trades if t['pnl_r'] <= 0)
        return {
            'trades': n,
            'wins': wins,
            'hard_losses': hard,
            'win_rate': round(wins / n, 4),
            'expectancy_r': round(total_r / n, 4),
            'profit_factor': round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        }

    results = []
    for name, filt in thresholds:
        disc_filtered = [t for t in discovery if filt(t)]
        val_filtered = [t for t in validation if filt(t)]
        disc_all_m = _metrics(discovery)
        val_all_m = _metrics(validation)
        disc_f_m = _metrics(disc_filtered)
        val_f_m = _metrics(val_filtered)
        results.append({
            'filter_name': name,
            'discovery_total': disc_all_m['trades'],
            'discovery_filtered': disc_f_m,
            'validation_total': val_all_m['trades'],
            'validation_filtered': val_f_m,
        })

    return results


# ============================================================
# 10. EXIT REASONS
# ============================================================
def get_exit_reasons(conn) -> dict[str, list]:
    sql = """
    SELECT
        scanner_name,
        exit_reason,
        COUNT(*) AS count,
        ROUND(AVG(pnl_r)::numeric, 4) AS avg_pnl_r,
        ROUND(SUM(pnl_usdt)::numeric, 2) AS total_pnl_usdt
    FROM dds.paper_trade
    WHERE scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND direction = 'LONG'
    AND status = 'CLOSED'
    GROUP BY scanner_name, exit_reason
    ORDER BY scanner_name, count DESC;
    """
    results = run_query(conn, sql)
    by_scanner = {}
    for row in results:
        scanner = row['scanner_name']
        if scanner not in by_scanner:
            by_scanner[scanner] = []
        by_scanner[scanner].append(row)
    return by_scanner


# ============================================================
# REPORT GENERATOR
# ============================================================
def _fmt(val, default='-'):
    if val is None:
        return default
    return str(val)


def generate_report(data: dict[str, Any]) -> str:
    lines = []

    lines.append("# MOMENTUM_EXHAUSTION_REVERSE_LONG V1/V2 Filter Validation V2")
    lines.append("")
    lines.append(f"> Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Executive Summary
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append("**Goal**: determine if `rsi_delta_3 > 0` has genuine edge, or V2 just reduces signals.")
    lines.append("")

    sample = data.get('sample_size', {})
    v1s = sample.get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', {})
    v2s = sample.get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V2', {})
    lines.append(f"- **V1 trades**: {v1s.get('total_trades', 'N/A')} ({v1s.get('unique_symbols', 'N/A')} symbols)")
    lines.append(f"- **V2 trades**: {v2s.get('total_trades', 'N/A')} ({v2s.get('unique_symbols', 'N/A')} symbols)")
    lines.append(f"- **V1 period**: {v1s.get('period_start', 'N/A')} → {v1s.get('period_end', 'N/A')}")
    lines.append(f"- **V2 period**: {v2s.get('period_start', 'N/A')} → {v2s.get('period_end', 'N/A')}")
    lines.append("")

    # RSI Coverage Stats
    rsi_data = data.get('rsi_recovery', [])
    rsi_available = sum(1 for t in rsi_data if t.get('rsi_delta_3') is not None)
    coverage = data.get('rsi_coverage', {})
    lines.append(f"- **V1 trades with recovered RSI**: {rsi_available} / {len(rsi_data)}")
    if coverage:
        lines.append(f"- **RSI coverage**: {coverage.get('trades_with_candles', 0)} / {coverage.get('total_trades', 0)} ({coverage.get('coverage_pct', 0)}%)")
        lines.append(f"- **Trades with enough candles (≥17)**: {coverage.get('trades_enough_candles', 0)}")
    lines.append("")

    # 2. Baseline
    lines.append("## 2. Baseline Metrics")
    lines.append("")
    baseline = data.get('baseline', {})
    for scanner, label in [('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', 'V1'), ('MOMENTUM_EXHAUSTION_REVERSE_LONG_V2', 'V2')]:
        b = baseline.get(scanner, {})
        if b:
            lines.append(f"### {label}")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|--------|------:|")
            for key, val in b.items():
                if key != 'scanner_name':
                    lines.append(f"| {key} | {_fmt(val)} |")
            lines.append("")

    # 3. R Distribution
    lines.append("## 3. R Distribution")
    lines.append("")
    r_dist = data.get('r_dist', {})
    for scanner in ['MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2']:
        rows = r_dist.get(scanner, [])
        v = 'V1' if 'V1' in scanner else 'V2'
        lines.append(f"### {v}")
        lines.append("")
        lines.append("| Bucket | Count | % |")
        lines.append("|-------:|------:|:-:|")
        for row in rows:
            lines.append(f"| {row['r_bucket']} | {row['count']} | {row['pct']}% |")
        lines.append("")

    # 4. Counterfactual V2-on-V1
    lines.append("## 4. Counterfactual: V2 Filter Applied to V1")
    lines.append("")
    cf = data.get('counterfactual', {})
    if cf:
        lines.append("| Scenario | Trades | Wins | Losses | Hard Losses | WR | Total R | E[R] | PF | Avg Win R | Avg Loss R |")
        lines.append("|----------|-------:|-----:|-------:|------------:|---:|--------:|-----:|---:|----------:|-----------:|")
        for key in ['V1_ALL', 'V1_IF_RSI_DELTA_3_GT_0', 'V1_REJECTED']:
            m = cf.get(key, {})
            lines.append(f"| {m.get('label', key)} | {m.get('trades', 0)} | {m.get('wins', 0)} | {m.get('losses', 0)} | {m.get('hard_losses', 0)} | {m.get('win_rate', 0)} | {_fmt(m.get('total_r'))} | {_fmt(m.get('expectancy_r'))} | {_fmt(m.get('profit_factor'))} | {_fmt(m.get('avg_win_r'))} | {_fmt(m.get('avg_loss_r'))} |")
        lines.append("")

    # 5. Winner/Loser Features
    lines.append("## 5. Winner vs Loser Features (V1)")
    lines.append("")
    wl = data.get('winner_loser', {}).get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', [])
    if wl:
        headers = [k for k in wl[0].keys() if k != 'scanner_name']
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join([":---" for _ in headers]) + " |")
        for row in wl:
            lines.append("| " + " | ".join(_fmt(row.get(k)) for k in headers) + " |")
    lines.append("")

    # 6. Bidirectional Thresholds
    lines.append("## 6. Bidirectional Threshold Search (V1)")
    lines.append("")
    thresholds = data.get('bidirectional_thresholds', [])
    if thresholds:
        # Sort by expectancy_r descending, show top 20
        sorted_t = sorted([t for t in thresholds if t.get('total_after') and t['total_after'] >= 5],
                          key=lambda x: x.get('expectancy_r') or 0, reverse=True)[:20]
        lines.append("| Filter | Trades After | Hard Losses | E[R] | PF | % Removed | % Hard Removed |")
        lines.append("|--------|-------------:|------------:|-----:|---:|----------:|---------------:|")
        for t in sorted_t:
            lines.append(f"| {t['filter_name']} | {t.get('total_after', '-')} | {t.get('hard_losses_after', '-')} | {_fmt(t.get('expectancy_r'))} | {_fmt(t.get('profit_factor'))} | {_fmt(t.get('pct_removed'))}% | {_fmt(t.get('pct_hard_losses_removed'))}% |")
    lines.append("")

    # 7. Repeat Trades Detailed
    lines.append("## 7. Repeat Trade Analysis")
    lines.append("")
    repeat = data.get('repeat_detailed', [])
    if repeat:
        lines.append("| Scanner | Group | Time | N | WR | E[R] | PF | Total R | Hard Losses |")
        lines.append("|---------|-------|------|--:|---:|-----:|---:|--------:|------------:|")
        for r in repeat:
            lines.append(f"| {'V1' if 'V1' in r.get('scanner_name','') else 'V2'} | {r.get('repeat_group','-')} | {r.get('time_group','-')} | {r.get('n','-')} | {_fmt(r.get('win_rate'))} | {_fmt(r.get('expectancy_r'))} | {_fmt(r.get('profit_factor'))} | {_fmt(r.get('total_r'))} | {r.get('hard_losses','-')} |")
    lines.append("")

    # 8. Combination Filters
    lines.append("## 8. Combination Filters (V1)")
    lines.append("")
    combos = data.get('combination_filters', [])
    if combos:
        sorted_c = sorted(combos, key=lambda x: x.get('er_after') or 0, reverse=True)
        lines.append("| Filter | Trades After | Hard After | E[R] | PF | % Removed |")
        lines.append("|--------|-------------:|-----------:|-----:|---:|----------:|")
        for c in sorted_c:
            pct = round((1 - (c.get('total_after', 0) or 0) / (c.get('total_before', 1) or 1)) * 100, 1) if c.get('total_before') else 0
            lines.append(f"| {c.get('filter_name','-')} | {c.get('total_after','-')} | {c.get('hard_after','-')} | {_fmt(c.get('er_after'))} | {_fmt(c.get('pf_after'))} | {pct}% |")
    lines.append("")

    # 9. Chronological Validation
    lines.append("## 9. Chronological Validation (70/30)")
    lines.append("")
    chrono = data.get('chronological', [])
    if chrono:
        lines.append("| Filter | Disc Trades | Disc E[R] | Disc PF | Disc WR | Val Trades | Val E[R] | Val PF | Val WR |")
        lines.append("|--------|------------:|----------:|--------:|--------:|-----------:|---------:|-------:|-------:|")
        for c in chrono:
            df = c.get('discovery_filtered', {})
            vf = c.get('validation_filtered', {})
            lines.append(f"| {c.get('filter_name','-')} | {df.get('trades','-')} | {_fmt(df.get('expectancy_r'))} | {_fmt(df.get('profit_factor'))} | {_fmt(df.get('win_rate'))} | {vf.get('trades','-')} | {_fmt(vf.get('expectancy_r'))} | {_fmt(vf.get('profit_factor'))} | {_fmt(vf.get('win_rate'))} |")
    lines.append("")

    # 10. Exit Reasons
    lines.append("## 10. Exit Reasons")
    lines.append("")
    exit_reasons = data.get('exit_reasons', {})
    for scanner in ['MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2']:
        rows = exit_reasons.get(scanner, [])
        v = 'V1' if 'V1' in scanner else 'V2'
        lines.append(f"### {v}")
        lines.append("")
        lines.append("| Exit Reason | Count | Avg PnL R | Total PnL USDT |")
        lines.append("|-------------|------:|----------:|---------------:|")
        for row in rows:
            lines.append(f"| {row['exit_reason']} | {row['count']} | {row['avg_pnl_r']} | {row['total_pnl_usdt']} |")
        lines.append("")

    # 11. Conclusions
    lines.append("## 11. Conclusions")
    lines.append("")

    # Coverage warning
    coverage = data.get('rsi_coverage', {})
    coverage_pct = coverage.get('coverage_pct', 0) or 0
    if coverage_pct < 90:
        lines.append(f"⚠️ **RSI Recovery Coverage Warning**: {coverage_pct}% — conclusions may be weak due to insufficient data.")
        lines.append(f"   - Total V1 trades: {coverage.get('total_trades', 'N/A')}")
        lines.append(f"   - Trades with candles: {coverage.get('trades_with_candles', 'N/A')}")
        lines.append(f"   - Trades with enough candles (≥17): {coverage.get('trades_enough_candles', 'N/A')}")
        lines.append("")

    # Auto-generate from counterfactual
    cf = data.get('counterfactual', {})
    v1_all = cf.get('V1_ALL', {})
    v1_pass = cf.get('V1_IF_RSI_DELTA_3_GT_0', {})
    v1_rej = cf.get('V1_REJECTED', {})

    if v1_all.get('trades') and v1_pass.get('trades'):
        lines.append(f"### rsi_delta_3 > 0 Counterfactual")
        lines.append("")
        er_all = v1_all.get('expectancy_r', 0) or 0
        er_pass = v1_pass.get('expectancy_r', 0) or 0
        er_rej = v1_rej.get('expectancy_r', 0) or 0
        wr_all = v1_all.get('win_rate', 0) or 0
        wr_pass = v1_pass.get('win_rate', 0) or 0
        hard_all = v1_all.get('hard_losses', 0)
        hard_pass = v1_pass.get('hard_losses', 0)

        lines.append(f"- **V1 ALL**: {v1_all['trades']} trades, E[R]={er_all}, WR={wr_all}, hard_losses={hard_all}")
        lines.append(f"- **V1 if rsi_delta_3>0**: {v1_pass['trades']} trades, E[R]={er_pass}, WR={wr_pass}, hard_losses={hard_pass}")
        lines.append(f"- **V1 rejected**: {v1_rej.get('trades',0)} trades, E[R]={er_rej}")
        lines.append("")

        if er_pass > er_all:
            lines.append(f"**VERDICT**: rsi_delta_3 > 0 has POSITIVE edge. E[R] improves from {er_all} to {er_pass}.")
        elif er_pass < er_all:
            lines.append(f"**VERDICT**: rsi_delta_3 > 0 does NOT have clear edge. E[R] drops from {er_all} to {er_pass}.")
        else:
            lines.append(f"**VERDICT**: E[R] is similar. Main effect is signal reduction.")
        lines.append("")

    # exhaustion_magnitude hypothesis
    lines.append("### exhaustion_magnitude Hypothesis")
    lines.append("")
    thresholds = data.get('bidirectional_thresholds', [])
    low_exhaust = [t for t in thresholds if 'exhaustion_magnitude <=' in t.get('filter_name', '') and (t.get('total_after') or 0) >= 5]
    high_exhaust = [t for t in thresholds if 'exhaustion_magnitude >=' in t.get('filter_name', '') and (t.get('total_after') or 0) >= 5]
    if low_exhaust:
        best_low = max(low_exhaust, key=lambda x: x.get('expectancy_r') or 0)
        lines.append(f"- Best `<=` filter: {best_low['filter_name']}, E[R]={_fmt(best_low.get('expectancy_r'))}, PF={_fmt(best_low.get('profit_factor'))}")
    if high_exhaust:
        best_high = max(high_exhaust, key=lambda x: x.get('expectancy_r') or 0)
        lines.append(f"- Best `>=` filter: {best_high['filter_name']}, E[R]={_fmt(best_high.get('expectancy_r'))}, PF={_fmt(best_high.get('profit_factor'))}")
    lines.append("")

    lines.append("*Detailed conclusions to be filled after VPS execution with real data.*")
    lines.append("")

    # 12. Recommended Next Experiment
    lines.append("## 12. Recommended Next Experiment")
    lines.append("")
    lines.append("1. Run this analysis on VPS with production data")
    lines.append("2. Verify counterfactual E[R] and hard-loss reduction")
    lines.append("3. If exhaustion_magnitude <= X shows edge: consider as V3 filter candidate")
    lines.append("4. If combination filters improve E[R]: test in shadow paper trading")
    lines.append("5. Always validate on held-out data before production deployment")
    lines.append("")

    # Appendix
    lines.append("## Appendix: Analysis Scripts")
    lines.append("")
    lines.append("```")
    lines.append("scripts/analysis/me_reverse_long_v1_v2/")
    lines.append("├── 01_sample_size.sql")
    lines.append("├── 02_baseline_metrics.sql")
    lines.append("├── 03_r_distribution.sql")
    lines.append("├── 04_winner_loser_features.sql")
    lines.append("├── 05_repeat_signal_analysis.sql")
    lines.append("├── 06_candidate_filters.sql")
    lines.append("├── 07_feature_join_diagnostic.sql")
    lines.append("├── 08_rsi_recovery_from_candles.sql")
    lines.append("├── run_analysis.py")
    lines.append("└── run_analysis_v2.py  ← THIS")
    lines.append("```")

    return '\n'.join(lines)


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("ME_REVERSE_LONG_V1_V2_FILTER_VALIDATION_V2")
    print("=" * 60)
    print()

    print("Connecting to PostgreSQL...")
    conn = get_connection()

    try:
        print("1. Sample size...")
        sample_size = get_sample_size(conn)

        print("2. Baseline metrics...")
        baseline = get_baseline_metrics(conn)

        print("3. R distribution...")
        r_dist = get_r_distribution(conn)

        print("4. RSI recovery from candles...")
        rsi_recovery = get_rsi_recovery(conn)
        print(f"   Recovered RSI for {len(rsi_recovery)} V1 trades")

        print("4b. RSI coverage diagnostics...")
        rsi_coverage = get_rsi_coverage(conn)
        print(f"   Coverage: {rsi_coverage.get('coverage_pct', 0)}%")

        print("5. Counterfactual V2-on-V1...")
        counterfactual = get_counterfactual_v2_on_v1(rsi_recovery)

        print("6. Winner/loser features...")
        winner_loser_raw = get_winner_loser_features(conn)

        print("7. Bidirectional threshold search...")
        bidirectional_thresholds = get_bidirectional_thresholds(conn)

        print("8. Repeat trade analysis...")
        repeat_detailed = get_repeat_detailed(conn)

        print("9. Combination filters...")
        combination_filters = get_combination_filters(conn)

        print("10. Chronological validation...")
        chronological = get_chronological_validation(conn)

        print("11. Exit reasons...")
        exit_reasons = get_exit_reasons(conn)

        data = {
            'sample_size': sample_size,
            'baseline': baseline,
            'r_dist': r_dist,
            'rsi_recovery': rsi_recovery,
            'rsi_coverage': rsi_coverage,
            'counterfactual': counterfactual,
            'winner_loser': winner_loser_raw,
            'bidirectional_thresholds': bidirectional_thresholds,
            'repeat_detailed': repeat_detailed,
            'combination_filters': combination_filters,
            'chronological': chronological,
            'exit_reasons': exit_reasons,
        }

        print("12. Generating report...")
        report = generate_report(data)

        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report, encoding='utf-8')
        print(f"Report written to: {REPORT_PATH}")

        json_path = REPORT_PATH.with_suffix('.json')
        def serialize(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return str(obj)
        json_path.write_text(json.dumps(data, default=serialize, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f"Raw data saved to: {json_path}")

        print()
        print("Analysis complete!")

    finally:
        conn.close()


def get_winner_loser_features(conn) -> dict[str, list]:
    sql = """
    WITH trades AS (
        SELECT
            pt.trade_id,
            pt.scanner_name,
            pt.symbol,
            pt.pnl_r,
            pt.mfe_r,
            pt.mae_r,
            pt.duration_sec,
            NULLIF(ss.features->>'exhaustion_magnitude', '')::numeric AS exhaustion_magnitude,
            NULLIF(ss.features->>'body_ratio', '')::numeric AS body_ratio,
            NULLIF(ss.features->>'rsi_confirmation', '')::numeric AS rsi_confirmation,
            NULLIF(ss.features->>'volume_ratio', '')::numeric AS volume_ratio,
            NULLIF(ss.features->>'rr_ratio', '')::numeric AS rr_ratio,
            NULLIF(ss.features->>'stop_distance_atr', '')::numeric AS stop_distance_atr,
            NULLIF(ss.features->>'rsi_14', '')::numeric AS rsi_14,
            NULLIF(ss.features->>'rsi_delta_3', '')::numeric AS rsi_delta_3,
            CASE
                WHEN pt.pnl_r > 0 THEN 'WIN'
                WHEN pt.pnl_r <= 0 AND pt.pnl_r > -0.9 THEN 'LOSS'
                ELSE 'HARD_LOSS'
            END AS outcome_group
        FROM dds.paper_trade pt
        JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
        WHERE pt.scanner_name IN (
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
        )
        AND pt.direction = 'LONG'
        AND pt.status = 'CLOSED'
    )
    SELECT
        scanner_name,
        outcome_group,
        COUNT(*) AS count,
        ROUND(AVG(exhaustion_magnitude)::numeric, 4) AS avg_exhaustion_mag,
        ROUND(AVG(body_ratio)::numeric, 4) AS avg_body_ratio,
        ROUND(AVG(rsi_confirmation)::numeric, 4) AS avg_rsi_confirm,
        ROUND(AVG(volume_ratio)::numeric, 4) AS avg_volume_ratio,
        ROUND(AVG(rr_ratio)::numeric, 4) AS avg_rr_ratio,
        ROUND(AVG(stop_distance_atr)::numeric, 4) AS avg_stop_dist_atr,
        ROUND(AVG(rsi_14)::numeric, 2) AS avg_rsi_14,
        ROUND(AVG(rsi_delta_3)::numeric, 4) AS avg_rsi_delta_3,
        ROUND(AVG(pnl_r)::numeric, 4) AS avg_pnl_r,
        ROUND(AVG(mfe_r)::numeric, 4) AS avg_mfe_r,
        ROUND(AVG(mae_r)::numeric, 4) AS avg_mae_r
    FROM trades
    GROUP BY scanner_name, outcome_group
    ORDER BY scanner_name, outcome_group;
    """
    results = run_query(conn, sql)
    by_scanner = {}
    for row in results:
        scanner = row['scanner_name']
        if scanner not in by_scanner:
            by_scanner[scanner] = []
        by_scanner[scanner].append(row)
    return by_scanner


if __name__ == "__main__":
    main()

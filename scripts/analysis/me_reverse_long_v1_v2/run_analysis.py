#!/usr/bin/env python3
"""
ME_REVERSE_LONG_V1_V2_OUTCOME_FEATURE_ANALYSIS_V1

Comparative analysis of MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 and V2
to find features that allow V2 to filter out losing entries.

Usage:
    python scripts/analysis/me_reverse_long_v1_v2/run_analysis.py

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

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


REPORT_PATH = PROJECT_ROOT / "reports" / "ME_REVERSE_LONG_V1_V2_OUTCOME_FEATURE_ANALYSIS_V1.md"


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


# ============================================================
# 1. SAMPLE SIZE
# ============================================================
def get_sample_size(conn) -> dict[str, Any]:
    """Get sample size and date ranges for V1 and V2."""
    sql = """
    SELECT
        scanner_name,
        COUNT(*) AS total_trades,
        COUNT(DISTINCT symbol) AS unique_symbols,
        COUNT(DISTINCT setup_id) AS unique_setups,
        MIN(entered_at) AS period_start,
        MAX(entered_at) AS period_end
    FROM dds.paper_trade
    WHERE scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND direction = 'LONG'
    AND status = 'CLOSED'
    GROUP BY scanner_name;
    """
    return {row['scanner_name']: row for row in run_query(conn, sql)}


# ============================================================
# 2. BASELINE METRICS
# ============================================================
def get_baseline_metrics(conn) -> dict[str, Any]:
    """Calculate baseline trading metrics for V1 and V2."""
    sql = """
    SELECT
        scanner_name,
        COUNT(*) AS trades,
        SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
        ROUND((SUM(CASE WHEN pnl_r > 0 THEN 1.0 ELSE 0 END) / COUNT(*))::numeric, 4) AS win_rate,
        ROUND(SUM(pnl_usdt)::numeric, 2) AS net_pnl_usdt,
        ROUND(AVG(pnl_usdt)::numeric, 2) AS avg_pnl_usdt,
        ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_usdt))::numeric, 2) AS median_pnl_usdt,
        ROUND(SUM(pnl_r)::numeric, 4) AS total_r,
        ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_r,
        ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_r))::numeric, 4) AS median_r,
        CASE
            WHEN SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END) > 0
            THEN ROUND((
                SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                / SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END))::numeric, 4)
            ELSE NULL
        END AS profit_factor,
        ROUND(AVG(CASE WHEN pnl_r > 0 THEN pnl_r END)::numeric, 4) AS avg_win_r,
        ROUND(AVG(CASE WHEN pnl_r <= 0 THEN pnl_r END)::numeric, 4) AS avg_loss_r,
        MAX(pnl_r) AS max_win_r,
        MIN(pnl_r) AS max_loss_r,
        ROUND(AVG(mfe)::numeric, 4) AS avg_mfe,
        ROUND(AVG(mae)::numeric, 4) AS avg_mae,
        ROUND((AVG(duration_sec) / 60)::numeric, 1) AS avg_hold_minutes
    FROM dds.paper_trade
    WHERE scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND direction = 'LONG'
    AND status = 'CLOSED'
    GROUP BY scanner_name;
    """
    return {row['scanner_name']: row for row in run_query(conn, sql)}


# ============================================================
# 3. R DISTRIBUTION
# ============================================================
def get_r_distribution(conn) -> dict[str, list]:
    """Calculate R-distribution buckets for V1 and V2."""
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
# 4. WINNER/LOSER FEATURES
# ============================================================
def get_winner_loser_features(conn) -> dict[str, list]:
    """Compare features between winners and losers."""
    sql = """
    WITH trades AS (
        SELECT
            pt.trade_id,
            pt.scanner_name,
            pt.symbol,
            pt.pnl_r,
            pt.mfe,
            pt.mae,
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
        ROUND((AVG(duration_sec) / 60)::numeric, 1) AS avg_hold_minutes,
        ROUND(AVG(mfe)::numeric, 4) AS avg_mfe,
        ROUND(AVG(mae)::numeric, 4) AS avg_mae
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


# ============================================================
# 5. REPEAT SIGNAL ANALYSIS
# ============================================================
def get_repeat_signal_analysis(conn) -> dict[str, list]:
    """Analyze repeat signals on same symbol."""
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
            ) AS prev_pnl_r
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
        COUNT(*) AS total_trades,
        SUM(CASE WHEN prev_entered_at IS NOT NULL THEN 1 ELSE 0 END) AS repeat_trades,
        ROUND((
            SUM(CASE WHEN prev_entered_at IS NOT NULL THEN 1 ELSE 0 END)::numeric
            / COUNT(*) * 100)::numeric, 1
        ) AS repeat_pct,
        SUM(CASE WHEN prev_entered_at IS NOT NULL
                 AND EXTRACT(EPOCH FROM (entered_at - prev_entered_at)) / 60 <= 60
            THEN 1 ELSE 0 END) AS repeats_within_1h,
        SUM(CASE WHEN prev_entered_at IS NOT NULL
                 AND EXTRACT(EPOCH FROM (entered_at - prev_entered_at)) / 60 <= 180
            THEN 1 ELSE 0 END) AS repeats_within_3h,
        SUM(CASE WHEN prev_pnl_r IS NOT NULL AND prev_pnl_r <= 0 THEN 1 ELSE 0 END) AS repeats_after_loss,
        SUM(CASE WHEN prev_pnl_r IS NOT NULL AND prev_pnl_r > 0 THEN 1 ELSE 0 END) AS repeats_after_win,
        ROUND((
            SUM(CASE WHEN prev_entered_at IS NOT NULL AND pnl_r > 0 THEN 1 ELSE 0 END)::numeric
            / NULLIF(SUM(CASE WHEN prev_entered_at IS NOT NULL THEN 1 ELSE 0 END), 0))::numeric,
            4
        ) AS repeat_win_rate,
        ROUND((
            SUM(CASE WHEN prev_entered_at IS NULL AND pnl_r > 0 THEN 1 ELSE 0 END)::numeric
            / NULLIF(SUM(CASE WHEN prev_entered_at IS NULL THEN 1 ELSE 0 END), 0))::numeric,
            4
        ) AS first_trade_win_rate
    FROM symbol_trades
    GROUP BY scanner_name
    ORDER BY scanner_name;
    """
    return {row['scanner_name']: row for row in run_query(conn, sql)}


# ============================================================
# 6. CANDIDATE FILTERS
# ============================================================
def get_candidate_filters(conn) -> list[dict]:
    """Test various filter thresholds and compute their impact."""
    filters = [
        ("exhaustion_magnitude >= 0.3", "exhaustion_magnitude >= 0.3"),
        ("exhaustion_magnitude >= 0.5", "exhaustion_magnitude >= 0.5"),
        ("exhaustion_magnitude >= 0.7", "exhaustion_magnitude >= 0.7"),
        ("body_ratio >= 0.4", "body_ratio >= 0.4"),
        ("body_ratio >= 0.6", "body_ratio >= 0.6"),
        ("body_ratio >= 0.8", "body_ratio >= 0.8"),
        ("rsi_confirmation >= 0.3", "rsi_confirmation >= 0.3"),
        ("rsi_confirmation >= 0.5", "rsi_confirmation >= 0.5"),
        ("rsi_confirmation >= 0.7", "rsi_confirmation >= 0.7"),
        ("volume_ratio >= 0.3", "volume_ratio >= 0.3"),
        ("volume_ratio >= 0.5", "volume_ratio >= 0.5"),
        ("volume_ratio >= 0.7", "volume_ratio >= 0.7"),
        ("rr_ratio >= 0.3", "rr_ratio >= 0.3"),
        ("rr_ratio >= 0.5", "rr_ratio >= 0.5"),
        ("rr_ratio >= 0.7", "rr_ratio >= 0.7"),
        ("stop_distance_atr >= 0.3", "stop_distance_atr >= 0.3"),
        ("stop_distance_atr >= 0.5", "stop_distance_atr >= 0.5"),
        ("stop_distance_atr >= 0.7", "stop_distance_atr >= 0.7"),
        ("rsi_14 >= 70", "rsi_14 >= 70"),
        ("rsi_14 >= 75", "rsi_14 >= 75"),
        ("rsi_14 >= 80", "rsi_14 >= 80"),
        ("rsi_delta_3 > 0", "rsi_delta_3 > 0"),
        ("rsi_delta_3 >= 0.5", "rsi_delta_3 >= 0.5"),
        ("rsi_delta_3 >= 1.0", "rsi_delta_3 >= 1.0"),
    ]

    results = []
    for filter_name, filter_condition in filters:
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
                NULLIF(ss.features->>'stop_distance_atr', '')::numeric AS stop_distance_atr,
                NULLIF(ss.features->>'rsi_14', '')::numeric AS rsi_14,
                NULLIF(ss.features->>'rsi_delta_3', '')::numeric AS rsi_delta_3
            FROM dds.paper_trade pt
            JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
            WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
              AND pt.direction = 'LONG'
              AND pt.status = 'CLOSED'
        ),
        baseline AS (
            SELECT
                COUNT(*) AS total_before,
                SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses_before,
                SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins_before,
                SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses_before,
                ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_before,
                ROUND((
                    SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END), 0))::numeric, 4
                ) AS pf_before
            FROM base
        ),
        filtered AS (
            SELECT
                COUNT(*) AS total_after,
                SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses_after,
                SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins_after,
                SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses_after,
                ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_after,
                ROUND((
                    SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END), 0))::numeric, 4
                ) AS pf_after
            FROM base
            WHERE {filter_condition}
        )
        SELECT
            '{filter_name}' AS filter_name,
            b.total_before,
            b.losses_before,
            b.wins_before,
            b.hard_losses_before,
            b.expectancy_before,
            b.pf_before,
            f.total_after,
            f.losses_after,
            f.wins_after,
            f.hard_losses_after,
            f.expectancy_after,
            f.pf_after,
            ROUND(((b.total_before - f.total_after)::numeric / b.total_before * 100)::numeric, 1) AS pct_trades_removed,
            ROUND(((b.losses_before - f.losses_after)::numeric / NULLIF(b.losses_before, 0) * 100)::numeric, 1) AS pct_losses_removed,
            ROUND((f.wins_after::numeric / NULLIF(b.wins_before, 0) * 100)::numeric, 1) AS pct_wins_retained,
            CASE
                WHEN f.hard_losses_after > 0 AND f.wins_after > 0
                THEN ROUND(((b.hard_losses_before - f.hard_losses_after)::numeric / NULLIF((b.wins_before - f.wins_after)::numeric, 0))::numeric, 2)
                ELSE NULL
            END AS hard_losses_per_winner_lost
        FROM baseline b, filtered f;
        """
        row = run_query_single(conn, sql)
        if row:
            results.append(row)

    return results


# ============================================================
# 7. EXIT REASON ANALYSIS
# ============================================================
def get_exit_reason_analysis(conn) -> dict[str, list]:
    """Analyze exit reasons by scanner version."""
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
# 8. SYMBOL ANALYSIS (specific examples from spec)
# ============================================================
def get_symbol_analysis(conn) -> list[dict]:
    """Analyze specific symbols mentioned in the spec."""
    sql = """
    SELECT
        pt.scanner_name,
        pt.symbol,
        pt.trade_id,
        pt.pnl_r,
        pt.pnl_usdt,
        pt.entered_at,
        pt.closed_at,
        pt.exit_reason,
        NULLIF(ss.features->>'rsi_delta_3', '')::numeric AS rsi_delta_3,
        NULLIF(ss.features->>'rsi_14', '')::numeric AS rsi_14,
        NULLIF(ss.features->>'exhaustion_magnitude', '')::numeric AS exhaustion_magnitude,
        NULLIF(ss.features->>'body_ratio', '')::numeric AS body_ratio,
        LAG(pt.entered_at) OVER (
            PARTITION BY pt.symbol, pt.scanner_name
            ORDER BY pt.entered_at
        ) AS prev_entered_at,
        LAG(pt.pnl_r) OVER (
            PARTITION BY pt.symbol, pt.scanner_name
            ORDER BY pt.entered_at
        ) AS prev_pnl_r
    FROM dds.paper_trade pt
    JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
    WHERE pt.scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND pt.direction = 'LONG'
    AND pt.status = 'CLOSED'
    AND pt.symbol IN ('USELESSUSDT', 'WIFUSDT', 'TAOUSDT', 'BCHUSDT')
    ORDER BY pt.symbol, pt.entered_at;
    """
    return run_query(conn, sql)


# ============================================================
# REPORT GENERATOR
# ============================================================
def format_table(headers: list[str], rows: list[list], align: list[str] = None) -> str:
    """Format a markdown table."""
    if not rows:
        return "*No data available*\n"

    if align is None:
        align = [':---' for _ in headers]

    lines = []
    lines.append('| ' + ' | '.join(headers) + ' |')
    lines.append('| ' + ' | '.join(align) + ' |')
    for row in rows:
        lines.append('| ' + ' | '.join(str(c) if c is not None else '-' for c in row) + ' |')
    return '\n'.join(lines) + '\n'


def generate_report(data: dict[str, Any]) -> str:
    """Generate the full markdown report."""
    lines = []

    # Header
    lines.append("# MOMENTUM_EXHAUSTION_REVERSE_LONG V1/V2 Outcome Feature Analysis")
    lines.append("")
    lines.append(f"> Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # 1. Executive Summary
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append("Comparative analysis of `MOMENTUM_EXHAUSTION_REVERSE_LONG_V1` and `V2` scanners.")
    lines.append("")
    lines.append("**V2 key difference**: adds `rsi_delta_3 > 0` filter (RSI must be rising).")
    lines.append("")

    sample = data.get('sample_size', {})
    v1_sample = sample.get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', {})
    v2_sample = sample.get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V2', {})
    lines.append(f"- **V1 trades**: {v1_sample.get('total_trades', 'N/A')} ({v1_sample.get('unique_symbols', 'N/A')} symbols)")
    lines.append(f"- **V2 trades**: {v2_sample.get('total_trades', 'N/A')} ({v2_sample.get('unique_symbols', 'N/A')} symbols)")
    lines.append(f"- **V1 period**: {v1_sample.get('period_start', 'N/A')} → {v1_sample.get('period_end', 'N/A')}")
    lines.append(f"- **V2 period**: {v2_sample.get('period_start', 'N/A')} → {v2_sample.get('period_end', 'N/A')}")
    lines.append("")

    # 2. Data Sources
    lines.append("## 2. Data Sources")
    lines.append("")
    lines.append("| Table | Purpose |")
    lines.append("|-------|---------|")
    lines.append("| `dds.scanner_setup` | Signal records with features JSONB |")
    lines.append("| `dds.paper_trade` | Paper trading outcomes with PnL |")
    lines.append("| `dds.paper_shadow_trade` | Shadow/counterfactual experiments |")
    lines.append("| `market.candle` | Historical OHLCV for feature recovery |")
    lines.append("| `analytics.trade_fact` | Canonical trade fact |")
    lines.append("")

    # 3. Sample Size
    lines.append("## 3. Sample Size")
    lines.append("")
    lines.append("| Version | Trades | Symbols | Setups | Period Start | Period End |")
    lines.append("|---------|-------:|--------:|-------:|-------------|------------|")
    for name, s in sample.items():
        version = 'V1' if 'V1' in name else 'V2'
        lines.append(f"| {version} | {s.get('total_trades', '-')} | {s.get('unique_symbols', '-')} | {s.get('unique_setups', '-')} | {s.get('period_start', '-')} | {s.get('period_end', '-')} |")
    lines.append("")

    # 4. V1 Baseline
    lines.append("## 4. V1 Baseline")
    lines.append("")
    baseline = data.get('baseline', {})
    v1 = baseline.get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', {})
    if v1:
        lines.append("| Metric | Value |")
        lines.append("|--------|------:|")
        for key, val in v1.items():
            if key != 'scanner_name':
                lines.append(f"| {key} | {val} |")
    lines.append("")

    # 5. V2 Baseline
    lines.append("## 5. V2 Baseline")
    lines.append("")
    v2 = baseline.get('MOMENTUM_EXHAUSTION_REVERSE_LONG_V2', {})
    if v2:
        lines.append("| Metric | Value |")
        lines.append("|--------|------:|")
        for key, val in v2.items():
            if key != 'scanner_name':
                lines.append(f"| {key} | {val} |")
    lines.append("")

    # 6. R Distribution
    lines.append("## 6. R Distribution")
    lines.append("")
    r_dist = data.get('r_dist', {})
    for scanner, rows in r_dist.items():
        version = 'V1' if 'V1' in scanner else 'V2'
        lines.append(f"### {version} R Distribution")
        lines.append("")
        lines.append("| Bucket | Count | % |")
        lines.append("|-------:|------:|:-:|")
        for row in rows:
            lines.append(f"| {row['r_bucket']} | {row['count']} | {row['pct']}% |")
        lines.append("")

    # 7. Winner vs Loser Features
    lines.append("## 7. Winner vs Loser Features")
    lines.append("")
    wl = data.get('winner_loser', {})
    for scanner, rows in wl.items():
        version = 'V1' if 'V1' in scanner else 'V2'
        lines.append(f"### {version} Features by Outcome")
        lines.append("")
        if rows:
            headers = [k for k in rows[0].keys() if k not in ('scanner_name',)]
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("| " + " | ".join([":---" for _ in headers]) + " |")
            for row in rows:
                lines.append("| " + " | ".join(str(row.get(k, '-')) for k in headers) + " |")
        lines.append("")

    # 8. Exit Reasons
    lines.append("## 8. Exit Reasons")
    lines.append("")
    exit_reasons = data.get('exit_reasons', {})
    for scanner, rows in exit_reasons.items():
        version = 'V1' if 'V1' in scanner else 'V2'
        lines.append(f"### {version} Exit Reasons")
        lines.append("")
        lines.append("| Exit Reason | Count | Avg PnL R | Total PnL USDT |")
        lines.append("|-------------|------:|----------:|---------------:|")
        for row in rows:
            lines.append(f"| {row['exit_reason']} | {row['count']} | {row['avg_pnl_r']} | {row['total_pnl_usdt']} |")
        lines.append("")

    # 9. Repeat Signal Analysis
    lines.append("## 9. Repeat Signal Analysis")
    lines.append("")
    repeat = data.get('repeat_signals', {})
    for scanner, row in repeat.items():
        version = 'V1' if 'V1' in scanner else 'V2'
        lines.append(f"### {version}")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|------:|")
        for key, val in row.items():
            if key != 'scanner_name':
                lines.append(f"| {key} | {val} |")
        lines.append("")

    # 10. Candidate Filters
    lines.append("## 10. Candidate Filters (V1)")
    lines.append("")
    filters = data.get('candidate_filters', [])
    if filters:
        lines.append("| Filter | Trades Before | Trades After | % Removed | Losses Before | Losses After | % Losses Removed | Wins Before | Wins After | % Wins Retained | WR Before | WR After | E[R] Before | E[R] After | PF Before | PF After |")
        lines.append("|--------|-------------:|-------------:|----------:|--------------:|-------------:|-----------------:|------------:|-----------:|----------------:|----------:|---------:|------------:|-----------:|----------:|---------:|")
        for f in filters:
            wr_before = round(f.get('wins_before', 0) / f.get('total_before', 1) * 100, 1) if f.get('total_before') else 0
            wr_after = round(f.get('wins_after', 0) / f.get('total_after', 1) * 100, 1) if f.get('total_after') else 0
            lines.append(f"| {f.get('filter_name', '')} | {f.get('total_before', '-')} | {f.get('total_after', '-')} | {f.get('pct_trades_removed', '-')}% | {f.get('losses_before', '-')} | {f.get('losses_after', '-')} | {f.get('pct_losses_removed', '-')}% | {f.get('wins_before', '-')} | {f.get('wins_after', '-')} | {f.get('pct_wins_retained', '-')}% | {wr_before}% | {wr_after}% | {f.get('expectancy_before', '-')} | {f.get('expectancy_after', '-')} | {f.get('pf_before', '-')} | {f.get('pf_after', '-')} |")
    lines.append("")

    # 11. Specific Symbol Analysis
    lines.append("## 11. Specific Symbol Analysis")
    lines.append("")
    symbols = data.get('symbol_analysis', [])
    if symbols:
        lines.append("| Scanner | Symbol | Trade ID | PnL R | Entered | Exit Reason | RSI Delta 3 | Prev Result |")
        lines.append("|---------|--------|----------|------:|---------|-------------|------------:|------------:|")
        for s in symbols:
            prev_result = f"{s.get('prev_pnl_r', '-')}R" if s.get('prev_pnl_r') is not None else 'FIRST'
            lines.append(f"| {'V1' if 'V1' in s.get('scanner_name', '') else 'V2'} | {s.get('symbol', '')} | {s.get('trade_id', '')} | {s.get('pnl_r', '')} | {s.get('entered_at', '')} | {s.get('exit_reason', '')} | {s.get('rsi_delta_3', '-')} | {prev_result} |")
    lines.append("")

    # 12. Conclusions
    lines.append("## 12. Conclusions")
    lines.append("")
    lines.append("*To be filled after analysis execution.*")
    lines.append("")

    # 13. Recommended Next Experiment
    lines.append("## 13. Recommended Next Experiment")
    lines.append("")
    lines.append("*To be filled after analysis execution.*")
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
    lines.append("└── run_analysis.py")
    lines.append("```")

    return '\n'.join(lines)


# ============================================================
# MAIN
# ============================================================
def main():
    """Run full analysis and generate report."""
    print("=" * 60)
    print("ME_REVERSE_LONG_V1_V2_OUTCOME_FEATURE_ANALYSIS_V1")
    print("=" * 60)
    print()

    print("Connecting to PostgreSQL...")
    conn = get_connection()

    try:
        print("1. Getting sample size...")
        sample_size = get_sample_size(conn)

        print("2. Getting baseline metrics...")
        baseline = get_baseline_metrics(conn)

        print("3. Getting R distribution...")
        r_dist = get_r_distribution(conn)

        print("4. Getting winner/loser features...")
        winner_loser = get_winner_loser_features(conn)

        print("5. Getting exit reason analysis...")
        exit_reasons = get_exit_reason_analysis(conn)

        print("6. Getting repeat signal analysis...")
        repeat_signals = get_repeat_signal_analysis(conn)

        print("7. Getting candidate filters...")
        candidate_filters = get_candidate_filters(conn)

        print("8. Getting symbol analysis...")
        symbol_analysis = get_symbol_analysis(conn)

        data = {
            'sample_size': sample_size,
            'baseline': baseline,
            'r_dist': r_dist,
            'winner_loser': winner_loser,
            'exit_reasons': exit_reasons,
            'repeat_signals': repeat_signals,
            'candidate_filters': candidate_filters,
            'symbol_analysis': symbol_analysis,
        }

        print("9. Generating report...")
        report = generate_report(data)

        # Write report
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report, encoding='utf-8')
        print(f"Report written to: {REPORT_PATH}")

        # Also save raw data as JSON for reproducibility
        json_path = REPORT_PATH.with_suffix('.json')
        # Convert datetime objects to strings for JSON serialization
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


if __name__ == "__main__":
    main()

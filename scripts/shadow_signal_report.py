"""Shadow Signal Report Generator.

Generates analysis reports for ATR_WICK_REJECTION_SHORT_V1 experiment.

Usage:
    python -m scripts.shadow_signal_report [--days N] [--output FILE]
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db.repository import ScannerRepository

logger = logging.getLogger(__name__)


def generate_shadow_signal_report(
    repo: ScannerRepository,
    days: int = 1,
    output_file: str | None = None,
) -> dict[str, Any]:
    """Generate comprehensive shadow signal analysis report.

    Args:
        repo: Database repository
        days: Number of days to analyze
        output_file: Optional output file path

    Returns:
        Report dictionary
    """
    report = {
        "experiment": "ATR_WICK_REJECTION_SHORT_V1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis_period_days": days,
        "sections": {},
    }

    # 1. Signal Summary by Symbol
    report["sections"]["signal_summary"] = _get_signal_summary(repo, days)

    # 2. MFE/MAE Distribution
    report["sections"]["mfe_mae_distribution"] = _get_mfe_mae_distribution(repo, days)

    # 3. Target Achievement Rates
    report["sections"]["target_achievement"] = _get_target_achievement(repo, days)

    # 4. Feature Comparison (profitable vs losing)
    report["sections"]["feature_comparison"] = _get_feature_comparison(repo, days)

    # 5. Top Performing Signals
    report["sections"]["top_signals"] = _get_top_signals(repo, days)

    # 6. Summary Statistics
    report["sections"]["summary_stats"] = _get_summary_stats(repo, days)

    # Save to file if specified
    if output_file:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info("Report saved to %s", output_file)

    return report


def _get_signal_summary(repo: ScannerRepository, days: int) -> list[dict]:
    """Get signal summary by symbol."""
    if not repo._use_pg:
        return []

    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT
            symbol,
            COUNT(*) as signals,
            ROUND(AVG(atr_pct)::numeric, 4) as avg_atr_pct,
            ROUND(AVG(rsi)::numeric, 2) as avg_rsi,
            ROUND(AVG(wick_atr)::numeric, 4) as avg_wick_atr,
            ROUND(AVG(close_location)::numeric, 4) as avg_close_location,
            ROUND(AVG(bb_width)::numeric, 4) as avg_bb_width,
            ROUND(AVG(volume_ratio)::numeric, 4) as avg_volume_ratio
        FROM dds.shadow_signal
        WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND signal_time > now() - (%s * interval '1 day')
        GROUP BY symbol
        ORDER BY signals DESC
        """,
        (days,),
    )

    return [
        {
            "symbol": row[0],
            "signals": row[1],
            "avg_atr_pct": float(row[2]) if row[2] else 0,
            "avg_rsi": float(row[3]) if row[3] else 0,
            "avg_wick_atr": float(row[4]) if row[4] else 0,
            "avg_close_location": float(row[5]) if row[5] else 0,
            "avg_bb_width": float(row[6]) if row[6] else 0,
            "avg_volume_ratio": float(row[7]) if row[7] else 0,
        }
        for row in cursor.fetchall()
    ]


def _get_mfe_mae_distribution(repo: ScannerRepository, days: int) -> list[dict]:
    """Get MFE/MAE distribution by symbol."""
    if not repo._use_pg:
        return []

    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT
            o.symbol,
            COUNT(*) as samples,
            ROUND(AVG(o.mfe_60m)::numeric, 4) as avg_mfe_60m,
            ROUND(AVG(o.mae_60m)::numeric, 4) as avg_mae_60m,
            ROUND(AVG(o.mfe_120m)::numeric, 4) as avg_mfe_120m,
            ROUND(AVG(o.mae_120m)::numeric, 4) as avg_mae_120m,
            ROUND(
                COUNT(*) FILTER (WHERE o.reached_minus_0_5)::numeric / COUNT(*), 4
            ) as pct_reached_0_5,
            ROUND(
                COUNT(*) FILTER (WHERE o.reached_minus_1_0)::numeric / COUNT(*), 4
            ) as pct_reached_1_0,
            ROUND(
                COUNT(*) FILTER (WHERE o.reached_minus_1_5)::numeric / COUNT(*), 4
            ) as pct_reached_1_5,
            ROUND(
                COUNT(*) FILTER (WHERE o.reached_minus_2_0)::numeric / COUNT(*), 4
            ) as pct_reached_2_0
        FROM dds.shadow_signal_outcome o
        WHERE o.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND o.evaluated_at > now() - (%s * interval '1 day')
        GROUP BY o.symbol
        HAVING COUNT(*) >= 3
        ORDER BY avg_mfe_60m DESC NULLS LAST
        """,
        (days,),
    )

    return [
        {
            "symbol": row[0],
            "samples": row[1],
            "avg_mfe_60m": float(row[2]) if row[2] else 0,
            "avg_mae_60m": float(row[3]) if row[3] else 0,
            "avg_mfe_120m": float(row[4]) if row[4] else 0,
            "avg_mae_120m": float(row[5]) if row[5] else 0,
            "pct_reached_0_5": float(row[6]) if row[6] else 0,
            "pct_reached_1_0": float(row[7]) if row[7] else 0,
            "pct_reached_1_5": float(row[8]) if row[8] else 0,
            "pct_reached_2_0": float(row[9]) if row[9] else 0,
        }
        for row in cursor.fetchall()
    ]


def _get_target_achievement(repo: ScannerRepository, days: int) -> dict:
    """Get overall target achievement rates."""
    if not repo._use_pg:
        return {}

    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE reached_minus_0_5) as reached_0_5,
            COUNT(*) FILTER (WHERE reached_minus_1_0) as reached_1_0,
            COUNT(*) FILTER (WHERE reached_minus_1_5) as reached_1_5,
            COUNT(*) FILTER (WHERE reached_minus_2_0) as reached_2_0,
            COUNT(*) FILTER (WHERE hit_plus_0_5_before_target) as hit_0_5_adverse,
            COUNT(*) FILTER (WHERE hit_plus_1_0_before_target) as hit_1_0_adverse,
            COUNT(*) FILTER (WHERE hit_plus_1_5_before_target) as hit_1_5_adverse
        FROM dds.shadow_signal_outcome
        WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND evaluated_at > now() - (%s * interval '1 day')
        """,
        (days,),
    )

    row = cursor.fetchone()
    if not row or row[0] == 0:
        return {}

    total = row[0]
    return {
        "total_signals": total,
        "reached_minus_0_5": {
            "count": row[1],
            "rate": round(row[1] / total, 4),
        },
        "reached_minus_1_0": {
            "count": row[2],
            "rate": round(row[2] / total, 4),
        },
        "reached_minus_1_5": {
            "count": row[3],
            "rate": round(row[3] / total, 4),
        },
        "reached_minus_2_0": {
            "count": row[4],
            "rate": round(row[4] / total, 4),
        },
        "hit_plus_0_5_before_target": {
            "count": row[5],
            "rate": round(row[5] / total, 4),
        },
        "hit_plus_1_0_before_target": {
            "count": row[6],
            "rate": round(row[6] / total, 4),
        },
        "hit_plus_1_5_before_target": {
            "count": row[7],
            "rate": round(row[7] / total, 4),
        },
    }


def _get_feature_comparison(repo: ScannerRepository, days: int) -> list[dict]:
    """Get feature comparison between profitable and losing signals."""
    if not repo._use_pg:
        return []

    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT
            o.symbol,
            CASE WHEN o.mfe_60m > 0 THEN 'profitable' ELSE 'losing' END as outcome,
            COUNT(*) as samples,
            ROUND(AVG(s.atr_pct)::numeric, 4) as avg_atr_pct,
            ROUND(AVG(s.rsi)::numeric, 2) as avg_rsi,
            ROUND(AVG(s.wick_atr)::numeric, 4) as avg_wick_atr,
            ROUND(AVG(s.close_location)::numeric, 4) as avg_close_location,
            ROUND(AVG(s.bb_width)::numeric, 4) as avg_bb_width,
            ROUND(AVG(s.distance_to_upper_bb)::numeric, 4) as avg_distance_to_upper_bb,
            ROUND(AVG(s.volume_ratio)::numeric, 4) as avg_volume_ratio,
            ROUND(AVG(s.ema_slope)::numeric, 6) as avg_ema_slope
        FROM dds.shadow_signal s
        JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
        WHERE o.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND o.evaluated_at > now() - (%s * interval '1 day')
          AND o.mfe_60m IS NOT NULL
        GROUP BY o.symbol, CASE WHEN o.mfe_60m > 0 THEN 'profitable' ELSE 'losing' END
        HAVING COUNT(*) >= 2
        ORDER BY o.symbol, outcome
        """,
        (days,),
    )

    return [
        {
            "symbol": row[0],
            "outcome": row[1],
            "samples": row[2],
            "avg_atr_pct": float(row[3]) if row[3] else 0,
            "avg_rsi": float(row[4]) if row[4] else 0,
            "avg_wick_atr": float(row[5]) if row[5] else 0,
            "avg_close_location": float(row[6]) if row[6] else 0,
            "avg_bb_width": float(row[7]) if row[7] else 0,
            "avg_distance_to_upper_bb": float(row[8]) if row[8] else 0,
            "avg_volume_ratio": float(row[9]) if row[9] else 0,
            "avg_ema_slope": float(row[10]) if row[10] else 0,
        }
        for row in cursor.fetchall()
    ]


def _get_top_signals(repo: ScannerRepository, days: int, limit: int = 10) -> list[dict]:
    """Get top performing signals by MFE."""
    if not repo._use_pg:
        return []

    cursor = repo._conn.cursor()
    cursor.execute(
        """
        SELECT
            s.symbol,
            s.signal_time,
            s.signal_price,
            s.wick_atr,
            s.rsi,
            s.close_location,
            o.mfe_60m,
            o.mae_60m,
            o.reached_minus_1_0
        FROM dds.shadow_signal s
        JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
        WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND s.signal_time > now() - (%s * interval '1 day')
          AND o.mfe_60m IS NOT NULL
        ORDER BY o.mfe_60m DESC
        LIMIT %s
        """,
        (days, limit),
    )

    return [
        {
            "symbol": row[0],
            "signal_time": row[1].isoformat() if row[1] else None,
            "signal_price": float(row[2]) if row[2] else 0,
            "wick_atr": float(row[3]) if row[3] else 0,
            "rsi": float(row[4]) if row[4] else 0,
            "close_location": float(row[5]) if row[5] else 0,
            "mfe_60m": float(row[6]) if row[6] else 0,
            "mae_60m": float(row[7]) if row[7] else 0,
            "reached_minus_1_0": row[8],
        }
        for row in cursor.fetchall()
    ]


def _get_summary_stats(repo: ScannerRepository, days: int) -> dict:
    """Get overall summary statistics."""
    if not repo._use_pg:
        return {}

    cursor = repo._conn.cursor()

    # Total signals
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM dds.shadow_signal
        WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND signal_time > now() - (%s * interval '1 day')
        """,
        (days,),
    )
    total_signals = cursor.fetchone()[0]

    # Total evaluated
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM dds.shadow_signal_outcome
        WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND evaluated_at > now() - (%s * interval '1 day')
        """,
        (days,),
    )
    total_evaluated = cursor.fetchone()[0]

    # Average MFE/MAE
    cursor.execute(
        """
        SELECT
            ROUND(AVG(mfe_60m)::numeric, 4) as avg_mfe_60m,
            ROUND(AVG(mae_60m)::numeric, 4) as avg_mae_60m,
            ROUND(AVG(mfe_120m)::numeric, 4) as avg_mfe_120m,
            ROUND(AVG(mae_120m)::numeric, 4) as avg_mae_120m
        FROM dds.shadow_signal_outcome
        WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
          AND evaluated_at > now() - (%s * interval '1 day')
        """,
        (days,),
    )
    row = cursor.fetchone()
    avg_mfe_60m = float(row[0]) if row[0] else 0
    avg_mae_60m = float(row[1]) if row[1] else 0
    avg_mfe_120m = float(row[2]) if row[2] else 0
    avg_mae_120m = float(row[3]) if row[3] else 0

    # Win rate (MFE > 0)
    if total_evaluated > 0:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM dds.shadow_signal_outcome
            WHERE experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
              AND evaluated_at > now() - (%s * interval '1 day')
              AND mfe_60m > 0
            """,
            (days,),
        )
        winning = cursor.fetchone()[0]
        win_rate = winning / total_evaluated
    else:
        win_rate = 0

    return {
        "total_signals": total_signals,
        "total_evaluated": total_evaluated,
        "avg_mfe_60m": avg_mfe_60m,
        "avg_mae_60m": avg_mae_60m,
        "avg_mfe_120m": avg_mfe_120m,
        "avg_mae_120m": avg_mae_120m,
        "win_rate_60m": round(win_rate, 4),
    }


def main():
    """Main entry point for report generation."""
    parser = argparse.ArgumentParser(
        description="Generate shadow signal analysis report"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to analyze (default: 1)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (JSON)",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Create repository
    repo = ScannerRepository(backend="postgres")

    # Generate report
    report = generate_shadow_signal_report(
        repo=repo,
        days=args.days,
        output_file=args.output,
    )

    # Print summary
    print("\n" + "=" * 80)
    print("SHADOW SIGNAL REPORT: ATR_WICK_REJECTION_SHORT_V1")
    print("=" * 80)
    print(f"Generated: {report['generated_at']}")
    print(f"Analysis period: {report['analysis_period_days']} day(s)")
    print("=" * 80)

    # Print summary stats
    stats = report["sections"]["summary_stats"]
    if stats:
        print("\nSUMMARY STATISTICS:")
        print(f"  Total signals: {stats['total_signals']}")
        print(f"  Total evaluated: {stats['total_evaluated']}")
        print(f"  Avg MFE (60m): {stats['avg_mfe_60m']:.2f}%")
        print(f"  Avg MAE (60m): {stats['avg_mae_60m']:.2f}%")
        print(f"  Win rate (60m): {stats['win_rate_60m']*100:.1f}%")

    # Print target achievement
    targets = report["sections"]["target_achievement"]
    if targets:
        print("\nTARGET ACHIEVEMENT:")
        for target_name, target_data in targets.items():
            if isinstance(target_data, dict) and "rate" in target_data:
                print(f"  {target_name}: {target_data['rate']*100:.1f}%")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
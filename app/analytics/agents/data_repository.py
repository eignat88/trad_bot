"""Read-only data access for specialist input assembly.

Provides deterministic queries against canonical analytics tables
(trade_fact, setup_fact, metric_snapshot, etc.) without mutating state.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class FunnelData:
    """Aggregated funnel facts for a specific scanner+direction segment."""
    scanner: str
    direction: str
    total_setups: int = 0
    entry_attempts: int = 0
    fills: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl_r: float = 0.0
    avg_pnl_r: float = 0.0
    profit_factor: float = 0.0
    avg_winner_r: float = 0.0
    avg_loser_r: float = 0.0
    avg_mfe_r: float = 0.0
    avg_mae_r: float = 0.0


@dataclass
class TradeCase:
    """Individual trade case for LLM consumption."""
    trade_id: int
    symbol: str
    scanner_name: str
    direction: str
    entered_at: Optional[str]
    closed_at: Optional[str]
    status: str
    pnl_r: Optional[float]
    mfe_r: Optional[float]
    mae_r: Optional[float]
    exit_reason: Optional[str]
    avg_entry_price: Optional[float]
    exit_price: Optional[float]
    market_regime: Optional[str]
    has_dca: bool = False


@dataclass
class MetricSnapshot:
    """A single metric snapshot row."""
    period: str
    segment: str
    metric_name: str
    metric_value: Optional[float]
    sample_count: Optional[int]
    window_from: str
    window_to: str


@dataclass
class DriftCandidate:
    """A programmatic drift/anomaly candidate."""
    candidate_type: str  # FREQUENCY_COLLAPSE, PNL_DRIFT, CONVERSION_DRIFT, etc.
    scanner: str
    direction: str
    metric_name: str
    current_value: Optional[float]
    baseline_value: Optional[float]
    change_pct: Optional[float]
    sample_size: int
    description: str


class SpecialistDataRepository:
    """Read-only queries for specialist input assembly."""
    
    def __init__(self, conn):
        self._conn = conn
    
    def get_funnel_data(
        self, run_id: UUID, period: str = "24h",
        scanners: Optional[list[str]] = None,
        directions: Optional[list[str]] = None,
    ) -> list[FunnelData]:
        """Get aggregated funnel data for scanner+direction segments.
        
        Reads from metric_snapshot (already computed by canonical build).
        """
        # Query metric_snapshot for the given run_id and period
        # Group by segment, aggregate funnel metrics
        # Return list of FunnelData
        cursor = self._conn.cursor()
        
        # Get metric snapshots for this run and period
        query = """
            SELECT segment, metric_name, metric_value, sample_count
            FROM analytics.metric_snapshot
            WHERE run_id = %s AND period = %s
            ORDER BY segment, metric_name
        """
        cursor.execute(query, (str(run_id), period))
        rows = cursor.fetchall()
        
        # Parse segments (format: "scanner:TREND_PULLBACK_V2:LONG")
        segments_data: dict[str, dict] = {}
        for segment, metric_name, value, count in rows:
            parts = segment.split(":")
            if len(parts) >= 3 and parts[0] == "scanner_direction":
                key = segment
                if key not in segments_data:
                    segments_data[key] = {
                        "scanner": parts[1] if len(parts) > 1 else "",
                        "direction": parts[2] if len(parts) > 2 else "",
                    }
                segments_data[key][metric_name] = value
                segments_data[key][f"{metric_name}_count"] = count
        
        results = []
        for key, data in segments_data.items():
            results.append(FunnelData(
                scanner=data.get("scanner", ""),
                direction=data.get("direction", ""),
                total_setups=int(data.get("total_setups", 0) or 0),
                entry_attempts=int(data.get("entry_attempts", 0) or 0),
                fills=int(data.get("fills", 0) or 0),
                closed_trades=int(data.get("closed_trades", 0) or 0),
                wins=int(data.get("wins", 0) or 0),
                losses=int(data.get("losses", 0) or 0),
                win_rate=float(data.get("win_rate", 0) or 0),
                total_pnl_r=float(data.get("total_pnl_r", 0) or 0),
                avg_pnl_r=float(data.get("avg_r", 0) or 0),
                profit_factor=float(data.get("profit_factor", 0) or 0),
                avg_winner_r=float(data.get("avg_winner_r", 0) or 0),
                avg_loser_r=float(data.get("avg_loser_r", 0) or 0),
                avg_mfe_r=float(data.get("avg_mfe_r", 0) or 0),
                avg_mae_r=float(data.get("avg_mae_r", 0) or 0),
            ))
        
        return results
    
    def get_trade_cases(
        self, run_id: UUID, limit: int = 20,
        scanners: Optional[list[str]] = None,
        directions: Optional[list[str]] = None,
    ) -> list[TradeCase]:
        """Get top trade cases for LLM consumption.
        
        Selects the most significant trades by absolute pnl_r.
        """
        cursor = self._conn.cursor()
        query = """
            SELECT trade_id, symbol, scanner_name, direction,
                   entered_at, closed_at, status, pnl_r, mfe_r, mae_r,
                   exit_reason, avg_entry_price, exit_price, market_regime,
                   dca_fill_price
            FROM analytics.trade_fact
            WHERE run_id = %s AND status IN ('CLOSED', 'EXPIRED')
            ORDER BY ABS(pnl_r) DESC NULLS LAST
            LIMIT %s
        """
        cursor.execute(query, (str(run_id), limit))
        
        results = []
        for row in cursor.fetchall():
            results.append(TradeCase(
                trade_id=row[0],
                symbol=row[1],
                scanner_name=row[2],
                direction=row[3],
                entered_at=row[4].isoformat() if row[4] else None,
                closed_at=row[5].isoformat() if row[5] else None,
                status=row[6],
                pnl_r=float(row[7]) if row[7] else None,
                mfe_r=float(row[8]) if row[8] else None,
                mae_r=float(row[9]) if row[9] else None,
                exit_reason=row[10],
                avg_entry_price=float(row[11]) if row[11] else None,
                exit_price=float(row[12]) if row[12] else None,
                market_regime=row[13],
                has_dca=row[14] is not None,
            ))
        return results
    
    def get_metric_snapshots(
        self, run_id: UUID, periods: Optional[list[str]] = None,
    ) -> list[MetricSnapshot]:
        """Get metric snapshots for all periods and segments."""
        if periods is None:
            periods = ["24h", "7d", "30d"]
        
        cursor = self._conn.cursor()
        placeholders = ", ".join(["%s"] * len(periods))
        query = f"""
            SELECT period, segment, metric_name, metric_value, sample_count,
                   window_from, window_to
            FROM analytics.metric_snapshot
            WHERE run_id = %s AND period IN ({placeholders})
            ORDER BY period, segment, metric_name
        """
        cursor.execute(query, (str(run_id), *periods))
        
        return [
            MetricSnapshot(
                period=row[0], segment=row[1], metric_name=row[2],
                metric_value=float(row[3]) if row[3] else None,
                sample_count=int(row[4]) if row[4] else None,
                window_from=row[5].isoformat() if row[5] else "",
                window_to=row[6].isoformat() if row[6] else "",
            )
            for row in cursor.fetchall()
        ]
    
    def get_horizon_metrics(
        self, run_id: UUID, trade_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Get horizon metrics for specific trades."""
        cursor = self._conn.cursor()
        if trade_ids:
            placeholders = ", ".join(["%s"] * len(trade_ids))
            query = f"""
                SELECT trade_id, anchor, horizon, favorable_move_r,
                       adverse_move_r, late_entry_loss_r,
                       post_exit_opportunity_r, coverage_status
                FROM analytics.trade_horizon_metric
                WHERE run_id = %s AND trade_id IN ({placeholders})
                ORDER BY trade_id, anchor, horizon
            """
            cursor.execute(query, (str(run_id), *trade_ids))
        else:
            query = """
                SELECT trade_id, anchor, horizon, favorable_move_r,
                       adverse_move_r, late_entry_loss_r,
                       post_exit_opportunity_r, coverage_status
                FROM analytics.trade_horizon_metric
                WHERE run_id = %s
                ORDER BY trade_id, anchor, horizon
            """
            cursor.execute(query, (str(run_id),))
        
        return [
            {
                "trade_id": row[0], "anchor": row[1], "horizon": row[2],
                "favorable_move_r": float(row[3]) if row[3] else None,
                "adverse_move_r": float(row[4]) if row[4] else None,
                "late_entry_loss_r": float(row[5]) if row[5] else None,
                "post_exit_opportunity_r": float(row[6]) if row[6] else None,
                "coverage_status": row[7],
            }
            for row in cursor.fetchall()
        ]
    
    def get_replay_metrics(
        self, run_id: UUID, trade_ids: Optional[list[int]] = None,
    ) -> list[dict]:
        """Get replay metrics for specific trades."""
        cursor = self._conn.cursor()
        if trade_ids:
            placeholders = ", ".join(["%s"] * len(trade_ids))
            query = f"""
                SELECT trade_id, scenario, simulated_pnl_r,
                       scenario_family, scenario_params, coverage_status
                FROM analytics.trade_replay_metric
                WHERE run_id = %s AND trade_id IN ({placeholders})
                ORDER BY trade_id, scenario
            """
            cursor.execute(query, (str(run_id), *trade_ids))
        else:
            query = """
                SELECT trade_id, scenario, simulated_pnl_r,
                       scenario_family, scenario_params, coverage_status
                FROM analytics.trade_replay_metric
                WHERE run_id = %s
                ORDER BY trade_id, scenario
            """
            cursor.execute(query, (str(run_id),))
        
        return [
            {
                "trade_id": row[0], "scenario": row[1],
                "simulated_pnl_r": float(row[2]) if row[2] else None,
                "scenario_family": row[3],
                "scenario_params": row[4] if isinstance(row[4], dict) else {},
                "coverage_status": row[5],
            }
            for row in cursor.fetchall()
        ]
    
    def get_quality_summary(self, run_id: UUID) -> dict:
        """Get quality check summary for this run."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT check_name, severity, status
            FROM analytics.data_quality_result
            WHERE run_id = %s
        """, (str(run_id),))
        
        checks = {}
        for name, severity, status in cursor.fetchall():
            checks[name] = {"severity": severity, "status": status}
        
        return checks
    
    def detect_drift_candidates(
        self, run_id: UUID, periods: list[str] = None,
    ) -> list[DriftCandidate]:
        """Programmatic drift detection: compare 24h vs 7d baseline."""
        if periods is None:
            periods = ["24h", "7d", "30d"]
        
        snapshots = self.get_metric_snapshots(run_id, periods)
        
        # Index by (segment, metric_name, period)
        indexed: dict[tuple[str, str, str], float] = {}
        for s in snapshots:
            if s.metric_value is not None:
                indexed[(s.segment, s.metric_name, s.period)] = s.metric_value
        
        candidates = []
        drift_metrics = ["pnl_r", "win_rate", "profit_factor", "closed_trades"]
        
        for (segment, metric_name, period_24h), val_24h in indexed.items():
            if period_24h != "24h":
                continue
            if metric_name not in drift_metrics:
                continue
            
            baseline_key = (segment, metric_name, "7d")
            if baseline_key not in indexed:
                continue
            
            val_7d = indexed[baseline_key]
            if val_7d == 0:
                change_pct = None
            else:
                change_pct = ((val_24h - val_7d) / abs(val_7d)) * 100
            
            parts = segment.split(":")
            scanner = parts[1] if len(parts) > 1 else ""
            direction = parts[2] if len(parts) > 2 else ""
            
            # Flag significant drift (>30% change)
            if change_pct is not None and abs(change_pct) > 30:
                candidates.append(DriftCandidate(
                    candidate_type="PNL_DRIFT" if metric_name == "pnl_r" else "METRIC_DRIFT",
                    scanner=scanner,
                    direction=direction,
                    metric_name=metric_name,
                    current_value=val_24h,
                    baseline_value=val_7d,
                    change_pct=round(change_pct, 1),
                    sample_size=1,
                    description=f"{metric_name} changed {change_pct:.1f}% (24h vs 7d) for {segment}",
                ))
        
        # Also detect frequency collapse (zero signals vs baseline)
        freq_key_24h = None
        freq_key_7d = None
        for (segment, metric_name, period), val in indexed.items():
            if metric_name == "total_setups" and period == "24h":
                freq_key_24h = (segment, val)
            if metric_name == "total_setups" and period == "7d":
                freq_key_7d = (segment, val)
        
        if freq_key_24h and freq_key_7d:
            seg_24h, val_24h = freq_key_24h
            seg_7d, val_7d = freq_key_7d
            if val_24h == 0 and val_7d > 0:
                parts = seg_24h.split(":")
                candidates.append(DriftCandidate(
                    candidate_type="FREQUENCY_COLLAPSE",
                    scanner=parts[1] if len(parts) > 1 else "",
                    direction=parts[2] if len(parts) > 2 else "",
                    metric_name="total_setups",
                    current_value=0,
                    baseline_value=val_7d,
                    change_pct=-100.0,
                    sample_size=1,
                    description=f"Zero signals in 24h vs {val_7d} in 7d baseline for {seg_24h}",
                ))
        
        return candidates

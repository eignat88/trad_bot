"""Funnel and Performance specialist input assembler."""
from __future__ import annotations
import logging
from typing import Any
from uuid import UUID

from app.analytics.agents.input.base import SpecialistInput
from app.analytics.agents.data_repository import SpecialistDataRepository
from app.analytics.agents.evidence import EvidenceCatalog, build_metric_id

logger = logging.getLogger(__name__)

MAX_SEGMENTS = 20
MAX_CASES = 20
MAX_EVIDENCE = 100


class FunnelPerformanceInputAssembler:
    """Assembles input for the Funnel and Performance specialist."""
    
    def __init__(self, data_repo: SpecialistDataRepository):
        self._data_repo = data_repo
    
    def assemble(
        self,
        *,
        run_id: UUID,
        dataset_version: str,
        maturity: str,
        quality_status: str,
        limitations: list[str],
        analysis_window_from: str,
        analysis_window_to: str,
    ) -> SpecialistInput:
        """Assemble funnel + performance input from canonical data."""
        limitations = list(limitations)
        evidence_ids: list[str] = []
        evidence_catalog: list[dict] = []
        
        # 1. Get funnel data for 24h, 7d, 30d
        funnel_24h = self._data_repo.get_funnel_data(run_id, "24h")
        funnel_7d = self._data_repo.get_funnel_data(run_id, "7d")
        funnel_30d = self._data_repo.get_funnel_data(run_id, "30d")
        
        if not funnel_30d:
            limitations.append("30d data unavailable — baseline may be incomplete")
        
        # 2. Build metrics dict for all periods
        metrics: dict[str, Any] = {}
        for period_name, data_list in [("24h", funnel_24h), ("7d", funnel_7d), ("30d", funnel_30d)]:
            for fd in data_list:
                prefix = f"funnel:{fd.scanner}:{fd.direction}:{period_name}"
                metrics[f"{prefix}:total_setups"] = {
                    "value": fd.total_setups, "units": "count",
                    "metric_version": "v1", "population": fd.total_setups,
                }
                metrics[f"{prefix}:fills"] = {
                    "value": fd.fills, "units": "count",
                    "metric_version": "v1", "population": fd.total_setups,
                }
                metrics[f"{prefix}:closed_trades"] = {
                    "value": fd.closed_trades, "units": "count",
                    "metric_version": "v1", "population": fd.total_setups,
                }
                metrics[f"{prefix}:win_rate"] = {
                    "value": fd.win_rate, "units": "ratio",
                    "metric_version": "v1", "population": fd.closed_trades,
                }
                metrics[f"{prefix}:pnl_r"] = {
                    "value": fd.total_pnl_r, "units": "R",
                    "metric_version": "v1", "population": fd.closed_trades,
                }
                metrics[f"{prefix}:avg_r"] = {
                    "value": fd.avg_pnl_r, "units": "R",
                    "metric_version": "v1", "population": fd.closed_trades,
                }
                metrics[f"{prefix}:profit_factor"] = {
                    "value": fd.profit_factor, "units": "ratio",
                    "metric_version": "v1", "population": fd.closed_trades,
                }
                
                # Build evidence IDs
                for metric_name in ["total_setups", "fills", "closed_trades",
                                    "win_rate", "pnl_r", "avg_r", "profit_factor"]:
                    eid = build_metric_id(fd.scanner, fd.direction, period_name, metric_name)
                    evidence_ids.append(eid)
                    evidence_catalog.append({
                        "evidence_id": eid,
                        "evidence_type": "METRIC",
                        "entity_type": "scanner_direction",
                        "entity_id": f"{fd.scanner}:{fd.direction}",
                        "metric": metric_name,
                        "window": period_name,
                    })
        
        # 3. Build segments list (limit to MAX_SEGMENTS)
        segments = []
        all_scanner_dirs = set()
        for fd in funnel_24h + funnel_7d:
            key = (fd.scanner, fd.direction)
            all_scanner_dirs.add(key)
        
        for scanner, direction in sorted(all_scanner_dirs)[:MAX_SEGMENTS]:
            segments.append({
                "scanner": scanner, "direction": direction,
                "has_24h": any(f.scanner == scanner and f.direction == direction for f in funnel_24h),
                "has_7d": any(f.scanner == scanner and f.direction == direction for f in funnel_7d),
                "has_30d": any(f.scanner == scanner and f.direction == direction for f in funnel_30d),
            })
        
        # 4. Get trade cases
        cases_data = self._data_repo.get_trade_cases(run_id, limit=MAX_CASES)
        cases = []
        for tc in cases_data:
            case_dict = {
                "trade_id": tc.trade_id, "symbol": tc.symbol,
                "scanner": tc.scanner_name, "direction": tc.direction,
                "pnl_r": tc.pnl_r, "mfe_r": tc.mfe_r, "mae_r": tc.mae_r,
                "exit_reason": tc.exit_reason, "market_regime": tc.market_regime,
                "has_dca": tc.has_dca, "status": tc.status,
            }
            cases.append(case_dict)
            eid = f"case:trade:{tc.trade_id}"
            evidence_ids.append(eid)
            evidence_catalog.append({
                "evidence_id": eid, "evidence_type": "CASE",
                "entity_type": "trade", "entity_id": str(tc.trade_id),
            })
        
        # 5. Truncation check
        total_candidates = len(all_scanner_dirs)
        if total_candidates > MAX_SEGMENTS:
            limitations.append(
                f"Input truncated: {total_candidates} segments, showing top {MAX_SEGMENTS}"
            )
        
        # Deduplicate evidence
        evidence_ids = list(dict.fromkeys(evidence_ids))[:MAX_EVIDENCE]
        
        return SpecialistInput(
            agent_name="FUNNEL_AND_PERFORMANCE",
            run_id=run_id,
            dataset_version=dataset_version,
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
            maturity=maturity,
            quality_status=quality_status,
            limitations=limitations,
            sample_sizes={"total_segments": len(segments), "total_cases": len(cases)},
            metrics=metrics,
            segments=segments,
            cases=cases,
            evidence_ids=evidence_ids,
            evidence_catalog=evidence_catalog,
        )

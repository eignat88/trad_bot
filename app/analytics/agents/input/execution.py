"""Execution Quality specialist input assembler."""
from __future__ import annotations
import logging
from typing import Any
from uuid import UUID

from app.analytics.agents.input.base import SpecialistInput
from app.analytics.agents.data_repository import SpecialistDataRepository
from app.analytics.agents.evidence import EvidenceCatalog, build_metric_id, build_case_id

logger = logging.getLogger(__name__)

MAX_TRADES = 30
MAX_EVIDENCE = 100


class ExecutionQualityInputAssembler:
    """Assembles input for the Execution Quality specialist."""
    
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
        limitations = list(limitations)
        evidence_ids: list[str] = []
        evidence_catalog: list[dict] = []
        
        # 1. Get trade cases with execution details
        cases_data = self._data_repo.get_trade_cases(run_id, limit=MAX_TRADES)
        cases = []
        for tc in cases_data:
            case_dict = {
                "trade_id": tc.trade_id, "symbol": tc.symbol,
                "scanner": tc.scanner_name, "direction": tc.direction,
                "pnl_r": tc.pnl_r, "mfe_r": tc.mfe_r, "mae_r": tc.mae_r,
                "exit_reason": tc.exit_reason,
                "avg_entry_price": tc.avg_entry_price,
                "exit_price": tc.exit_price,
                "market_regime": tc.market_regime,
                "has_dca": tc.has_dca, "status": tc.status,
            }
            cases.append(case_dict)
            eid = build_case_id("trade", str(tc.trade_id))
            evidence_ids.append(eid)
            evidence_catalog.append({
                "evidence_id": eid, "evidence_type": "CASE",
                "entity_type": "trade", "entity_id": str(tc.trade_id),
            })
        
        # 2. Get horizon metrics for these trades
        trade_ids = [tc.trade_id for tc in cases_data]
        horizons = self._data_repo.get_horizon_metrics(run_id, trade_ids)
        
        # Aggregate horizon metrics
        metrics: dict[str, Any] = {}
        if horizons:
            # Average MFE/MAE across entry horizons
            entry_mfe = [h["favorable_move_r"] for h in horizons
                         if h["anchor"] == "entry" and h["favorable_move_r"] is not None]
            entry_mae = [h["adverse_move_r"] for h in horizons
                         if h["anchor"] == "entry" and h["adverse_move_r"] is not None]
            post_exit_opp = [h["post_exit_opportunity_r"] for h in horizons
                            if h["anchor"] == "exit" and h["post_exit_opportunity_r"] is not None]
            
            if entry_mfe:
                avg_mfe = sum(entry_mfe) / len(entry_mfe)
                metrics["execution:avg_entry_mfe_r"] = {
                    "value": round(avg_mfe, 3), "units": "R",
                    "metric_version": "v1", "population": len(entry_mfe),
                }
                evidence_ids.append(build_metric_id("execution", "ALL", "24h", "avg_entry_mfe_r"))
            
            if entry_mae:
                avg_mae = sum(entry_mae) / len(entry_mae)
                metrics["execution:avg_entry_mae_r"] = {
                    "value": round(avg_mae, 3), "units": "R",
                    "metric_version": "v1", "population": len(entry_mae),
                }
                evidence_ids.append(build_metric_id("execution", "ALL", "24h", "avg_entry_mae_r"))
            
            if post_exit_opp:
                avg_opp = sum(post_exit_opp) / len(post_exit_opp)
                metrics["execution:avg_post_exit_opportunity_r"] = {
                    "value": round(avg_opp, 3), "units": "R",
                    "metric_version": "v1", "population": len(post_exit_opp),
                }
        
        # 3. Get replay metrics
        replays = self._data_repo.get_replay_metrics(run_id, trade_ids)
        
        # Compare ACTUAL vs NO_DCA replay
        actual_by_trade: dict[int, float] = {}
        nodca_by_trade: dict[int, float] = {}
        for r in replays:
            if r["scenario"] == "ACTUAL" and r["simulated_pnl_r"] is not None:
                actual_by_trade[r["trade_id"]] = r["simulated_pnl_r"]
            if r["scenario"] == "RISK_NORMALIZED_NO_DCA" and r["simulated_pnl_r"] is not None:
                nodca_by_trade[r["trade_id"]] = r["simulated_pnl_r"]
        
        if actual_by_trade and nodca_by_trade:
            common = set(actual_by_trade.keys()) & set(nodca_by_trade.keys())
            if common:
                diffs = [actual_by_trade[t] - nodca_by_trade[t] for t in common]
                avg_diff = sum(diffs) / len(diffs)
                metrics["execution:dca_impact_r"] = {
                    "value": round(avg_diff, 3), "units": "R",
                    "metric_version": "v1", "population": len(common),
                }
                evidence_ids.append(build_metric_id("execution", "ALL", "24h", "dca_impact_r"))
                limitations.append(f"DCA impact based on {len(common)} trades with valid replay data")
        
        # 4. Summary metrics
        closed = [tc for tc in cases_data if tc.status == "CLOSED"]
        metrics["execution:total_trades"] = {
            "value": len(cases), "units": "count",
            "metric_version": "v1", "population": len(cases),
        }
        metrics["execution:total_closed"] = {
            "value": len(closed), "units": "count",
            "metric_version": "v1", "population": len(closed),
        }
        
        # Truncation
        if len(cases_data) >= MAX_TRADES:
            limitations.append(f"Trade cases truncated to {MAX_TRADES}")
        
        evidence_ids = list(dict.fromkeys(evidence_ids))[:MAX_EVIDENCE]
        
        return SpecialistInput(
            agent_name="EXECUTION_QUALITY",
            run_id=run_id,
            dataset_version=dataset_version,
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
            maturity=maturity,
            quality_status=quality_status,
            limitations=limitations,
            sample_sizes={"total_cases": len(cases), "total_closed": len(closed)},
            metrics=metrics,
            segments=[],
            cases=cases,
            evidence_ids=evidence_ids,
            evidence_catalog=evidence_catalog,
        )

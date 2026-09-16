"""Drift and Anomaly specialist input assembler."""
from __future__ import annotations
import logging
from typing import Any
from uuid import UUID

from app.analytics.agents.input.base import SpecialistInput
from app.analytics.agents.data_repository import SpecialistDataRepository
from app.analytics.agents.evidence import build_metric_id, build_quality_id

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 20
MAX_EVIDENCE = 100


class DriftAnomalyInputAssembler:
    """Assembles input for the Drift and Anomaly specialist."""
    
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
        
        # 1. Get metric snapshots for comparison
        snapshots = self._data_repo.get_metric_snapshots(run_id, ["24h", "7d", "30d"])
        
        # 2. Build metrics dict
        metrics: dict[str, Any] = {}
        for s in snapshots:
            key = f"drift:{s.segment}:{s.period}:{s.metric_name}"
            metrics[key] = {
                "value": s.metric_value, "units": "count" if "count" in s.metric_name or "total" in s.metric_name else "ratio",
                "metric_version": "v1", "population": s.sample_count or 0,
            }
            eid = build_metric_id("drift", s.segment.split(":")[-1] if ":" in s.segment else s.segment, s.period, s.metric_name)
            evidence_ids.append(eid)
        
        # 3. Detect drift candidates programmatically
        drift_candidates = self._data_repo.detect_drift_candidates(run_id)
        
        # 4. Build segments from snapshots
        segments_set = set()
        for s in snapshots:
            segments_set.add(s.segment)
        
        segments = [{"segment": seg} for seg in sorted(segments_set)[:20]]
        
        # 5. Build cases from drift candidates
        cases = []
        for dc in drift_candidates[:MAX_CANDIDATES]:
            case_dict = {
                "candidate_type": dc.candidate_type,
                "scanner": dc.scanner,
                "direction": dc.direction,
                "metric_name": dc.metric_name,
                "current_value": dc.current_value,
                "baseline_value": dc.baseline_value,
                "change_pct": dc.change_pct,
                "description": dc.description,
            }
            cases.append(case_dict)
            eid = f"drift:candidate:{dc.candidate_type}:{dc.scanner}:{dc.direction}"
            evidence_ids.append(eid)
        
        # 6. Get quality findings as evidence
        quality = self._data_repo.get_quality_summary(run_id)
        for check_name, info in quality.items():
            eid = build_quality_id(check_name)
            evidence_ids.append(eid)
            evidence_catalog.append({
                "evidence_id": eid, "evidence_type": "QUALITY",
                "entity_type": "quality_check", "entity_id": check_name,
                "severity": info["severity"], "status": info["status"],
            })
        
        if not drift_candidates:
            limitations.append("No significant drift candidates detected programmatically")
        
        evidence_ids = list(dict.fromkeys(evidence_ids))[:MAX_EVIDENCE]
        
        return SpecialistInput(
            agent_name="DRIFT_AND_ANOMALY",
            run_id=run_id,
            dataset_version=dataset_version,
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
            maturity=maturity,
            quality_status=quality_status,
            limitations=limitations,
            sample_sizes={"total_snapshots": len(snapshots), "drift_candidates": len(drift_candidates)},
            metrics=metrics,
            segments=segments,
            cases=cases,
            evidence_ids=evidence_ids,
            evidence_catalog=evidence_catalog,
        )

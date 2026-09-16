"""Deterministic action policy for Chief Trading Analyst output.

Validates that the chosen action_class is justified by the Chief output.
Prevents invalid action assignments (e.g., PRODUCTION_INCIDENT without evidence).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionResult:
    """Result of action policy validation."""
    valid: bool
    action_class: str
    violations: tuple[str, ...]


class ActionPolicyV1:
    """Deterministic action classification policy.
    
    Rules:
    - PRODUCTION_INCIDENT requires operational/structural incident evidence
    - DATA_FIX requires quality-related findings or limitations
    - BACKTEST requires at least one proposed_experiment
    - NO_ACTION forbidden if critical unresolved anomaly or missing specialist
    - All action classes must be one of the 6 allowed values
    """
    
    ALLOWED_ACTIONS = frozenset({
        "NO_ACTION", "MONITOR", "INVESTIGATE",
        "BACKTEST", "DATA_FIX", "PRODUCTION_INCIDENT",
    })
    
    @classmethod
    def validate(
        cls,
        action_class: str,
        findings: list[dict[str, Any]],
        hypotheses: list[dict[str, Any]],
        proposed_experiments: list[dict[str, Any]],
        missing_agents: list[str],
        data_quality_status: str,
        limitations: list[str],
        anomalies: list[dict[str, Any]] | None = None,
    ) -> ActionResult:
        """Validate that the action class is justified by the output."""
        violations: list[str] = []
        
        # Must be one of the allowed values
        if action_class not in cls.ALLOWED_ACTIONS:
            violations.append(f"Invalid action_class: {action_class}. Allowed: {cls.ALLOWED_ACTIONS}")
            return ActionResult(valid=False, action_class=action_class, violations=tuple(violations))
        
        # NO_ACTION guards
        if action_class == "NO_ACTION":
            # Cannot be NO_ACTION if there are unresolved critical anomalies
            if anomalies:
                critical = [a for a in anomalies if a.get("severity") == "HIGH"]
                if critical:
                    violations.append("NO_ACTION not allowed with HIGH severity anomalies")
            
            # Cannot be NO_ACTION if required specialist is missing
            if missing_agents:
                violations.append(f"NO_ACTION not allowed with missing agents: {missing_agents}")
            
            # Cannot be NO_ACTION if data quality is DEGRADED with critical limitations
            if data_quality_status == "DEGRADED":
                limitations_with_critical = [l for l in limitations if "critical" in l.lower() or "data corruption" in l.lower()]
                if limitations_with_critical:
                    violations.append("NO_ACTION not allowed with critical data quality limitations")
        
        # PRODUCTION_INCIDENT guards
        if action_class == "PRODUCTION_INCIDENT":
            has_incident_evidence = False
            if anomalies:
                for a in anomalies:
                    if a.get("severity") == "HIGH" and a.get("code", "").startswith("STRUCTURAL"):
                        has_incident_evidence = True
                        break
            if findings:
                for f in findings:
                    stmt = f.get("statement", "").lower()
                    if "lifecycle corruption" in stmt or "duplicate fill" in stmt or "risk-control" in stmt:
                        has_incident_evidence = True
                        break
            if not has_incident_evidence:
                violations.append("PRODUCTION_INCIDENT requires operational/structural incident evidence")
        
        # BACKTEST guards
        if action_class == "BACKTEST":
            if not proposed_experiments:
                violations.append("BACKTEST requires at least one proposed_experiment")
        
        # DATA_FIX guards
        if action_class == "DATA_FIX":
            has_quality_evidence = data_quality_status == "DEGRADED" or any(
                "quality" in l.lower() or "data" in l.lower() or "coverage" in l.lower()
                for l in limitations
            )
            if not has_quality_evidence:
                violations.append("DATA_FIX requires quality evidence or data-related limitations")
        
        return ActionResult(
            valid=len(violations) == 0,
            action_class=action_class,
            violations=tuple(violations),
        )

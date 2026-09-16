"""Deterministic report renderer for daily trading reports.

Converts Chief output JSON into human-readable text.
No LLM calls — pure deterministic formatting.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID


def render_daily_report(
    *,
    report_id: UUID,
    analysis_run_id: UUID,
    business_date: str,
    maturity: str,
    executive_summary: str,
    findings: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    proposed_experiments: list[dict[str, Any]],
    action_class: str,
    limitations: list[str],
    evidence_refs: list[str],
    partial: bool,
    missing_agents: list[str],
    specialist_summaries: dict[str, str] | None = None,
) -> str:
    """Render a daily trading report as human-readable text.
    
    This is fully deterministic — same input produces identical output.
    No LLM involved.
    """
    lines: list[str] = []
    
    # Header
    maturity_label = "PROVISIONAL — preliminary analysis" if maturity == "PROVISIONAL" else "FINAL — completed analysis window"
    lines.append("=" * 72)
    lines.append("DAILY TRADING ANALYTICS REPORT")
    lines.append("=" * 72)
    lines.append(f"Report ID:    {report_id}")
    lines.append(f"Run ID:       {analysis_run_id}")
    lines.append(f"Business:     {business_date}")
    lines.append(f"Maturity:     {maturity_label}")
    lines.append(f"Action:       {action_class}")
    if partial:
        lines.append(f"Status:       PARTIAL — missing specialist data")
    lines.append("")
    
    # Executive Summary
    lines.append("─" * 72)
    lines.append("EXECUTIVE SUMMARY")
    lines.append("─" * 72)
    lines.append(executive_summary)
    lines.append("")
    
    # Specialist Summaries
    if specialist_summaries:
        lines.append("─" * 72)
        lines.append("SPECIALIST SUMMARIES")
        lines.append("─" * 72)
        for agent_name, summary in specialist_summaries.items():
            lines.append(f"  {agent_name}: {summary}")
        lines.append("")
    
    # Missing Agents
    if missing_agents:
        lines.append("─" * 72)
        lines.append("UNAVAILABLE SPECIALIST ANALYSIS")
        lines.append("─" * 72)
        for agent in missing_agents:
            lines.append(f"  ⚠ {agent}: analysis unavailable")
        lines.append("")
    
    # Key Findings
    if findings:
        lines.append("─" * 72)
        lines.append("KEY FINDINGS")
        lines.append("─" * 72)
        for i, f in enumerate(findings, 1):
            section = f.get("section", "")
            statement = f.get("statement", "")
            confidence = f.get("confidence", "")
            lines.append(f"  [{section}] {statement}")
            lines.append(f"    Confidence: {confidence} | Evidence: {', '.join(f.get('evidence_refs', []))}")
            lines.append("")
    
    # Hypotheses
    if hypotheses:
        lines.append("─" * 72)
        lines.append("HYPOTHESES & PROPOSED EXPERIMENTS")
        lines.append("─" * 72)
        for h in hypotheses:
            lines.append(f"  Hypothesis: {h.get('hypothesis', '')}")
            lines.append(f"  Experiment: {h.get('falsifiable_experiment', '')}")
            lines.append(f"  Data needed: {', '.join(h.get('required_data', []))}")
            lines.append(f"  Validation: {h.get('validation_criterion', '')}")
            lines.append("")
    
    # Proposed Experiments
    if proposed_experiments:
        lines.append("─" * 72)
        lines.append("PROPOSED EXPERIMENTS")
        lines.append("─" * 72)
        for e in proposed_experiments:
            lines.append(f"  • {e.get('experiment', '')}")
            lines.append(f"    Required: {', '.join(e.get('required_data', []))}")
            lines.append(f"    Criterion: {e.get('validation_criterion', '')}")
            lines.append("")
    
    # Limitations
    if limitations:
        lines.append("─" * 72)
        lines.append("LIMITATIONS")
        lines.append("─" * 72)
        for lim in limitations:
            lines.append(f"  • {lim}")
        lines.append("")
    
    # Evidence
    if evidence_refs:
        lines.append("─" * 72)
        lines.append(f"EVIDENCE ({len(evidence_refs)} references)")
        lines.append("─" * 72)
        for ref in evidence_refs[:20]:  # cap display
            lines.append(f"  • {ref}")
        if len(evidence_refs) > 20:
            lines.append(f"  ... and {len(evidence_refs) - 20} more")
        lines.append("")
    
    # Footer
    lines.append("=" * 72)
    lines.append("END OF REPORT")
    lines.append("=" * 72)
    
    return "\n".join(lines)

"""Issue #324 / H1-E.6 — ``H1E_OOS_READINESS_V1`` (PROPOSAL ONLY).

Pre-registration TEMPLATE for the future H1-E OOS comparison of the
cohorts ``H1_BULLISH`` / ``H1_BEARISH`` / ``H1_NEUTRAL`` and the nine
4H×1D regime combinations against the UNCHANGED baseline control.

This module COMPUTES NO OUTCOMES.  It emits a frozen-shape proposal
document that a separately reviewed and frozen policy plus an owner GO
must approve BEFORE any accumulation or evaluation (Phase B/C hold).  The
template contains no evaluator, no statistics, no thresholds tuned from
data — every value is either an explicit ``TBD_FROZEN`` placeholder or a
pre-registered design constant stated in advance.
"""
from __future__ import annotations

from typing import Any

H1E_OOS_READINESS_VERSION = "H1E_OOS_READINESS_V1"

TEMPLATE_STATUS = "PROPOSAL_ONLY_NOT_FROZEN"
OUTCOME_COMPUTATION = "FORBIDDEN_IN_PHASE_A"

COHORT_H1_BULLISH = "H1_BULLISH"
COHORT_H1_BEARISH = "H1_BEARISH"
COHORT_H1_NEUTRAL = "H1_NEUTRAL"

REGIME_COMBINATIONS: tuple[str, ...] = (
    "BULLISH/BULLISH", "BULLISH/BEARISH", "BULLISH/NEUTRAL",
    "BEARISH/BULLISH", "BEARISH/BEARISH", "BEARISH/NEUTRAL",
    "NEUTRAL/BULLISH", "NEUTRAL/BEARISH", "NEUTRAL/NEUTRAL",
)


def build_preregistration_template() -> dict[str, Any]:
    """Build the H1-E OOS pre-registration template (pure, proposal only).

    Every cohort/combination is compared against the UNCHANGED baseline
    control on IDENTICAL ``source_event_key`` values (paired design) — the
    control definition is never re-tuned, never re-fit, never replaced.
    """
    def _cohort(name: str) -> dict[str, Any]:
        return {
            "cohort": name,
            "inclusion": (
                "prospective #305 source events with signal_time > h1e "
                "freeze_ts, H1-E classification ELIGIBLE, combined regime "
                f"== {name}"
            ),
            "exclusions": (
                "pre-freeze events; #305 UNVERIFIED window events; the "
                "historical SOXSUSDT QA example; deferred/non-eligible "
                "classifications; identity conflicts",
            ),
            "identity": "original source_event_key (verbatim)",
            "accumulated_count": None,
            "status": "NOT_STARTED",
        }

    return {
        "template_version": H1E_OOS_READINESS_VERSION,
        "status": TEMPLATE_STATUS,
        "outcome_computation": OUTCOME_COMPUTATION,
        "phase_bc_hold": {
            "phase_b": "VPS read-only preflight — SEPARATE OWNER GO",
            "phase_c": "isolated deploy / H1-E freeze minting — SEPARATE "
                       "OWNER GO",
            "accumulation": "requires separately reviewed + frozen policy "
                            "and GO",
            "evaluation": "requires separately reviewed + frozen policy and "
                          "GO",
            "merge_or_deploy": "NOT authorized by this template",
        },
        "cohorts": {
            COHORT_H1_BULLISH: _cohort(COHORT_H1_BULLISH),
            COHORT_H1_BEARISH: _cohort(COHORT_H1_BEARISH),
            COHORT_H1_NEUTRAL: _cohort(COHORT_H1_NEUTRAL),
        },
        "htf_combinations": [
            {
                "combination": combo,
                "definition": (
                    "verbatim 4H×1D regime pair retained from H1-C/H1-D — "
                    "never merged, never averaged"),
                "accumulated_count": None,
                "status": "NOT_STARTED",
            }
            for combo in REGIME_COMBINATIONS
        ],
        "control": {
            "definition": "UNCHANGED_BASELINE_CONTROL",
            "pairing": "identical source_event_key (paired comparison)",
            "tuning": "FORBIDDEN — the control is never re-fit or replaced",
            "description": (
                "the existing unchanged baseline evaluated on exactly the "
                "same prospective events as the cohort"),
        },
        "sample_thresholds": {
            "min_events_per_cohort": "TBD_FROZEN",
            "min_distinct_symbols": "TBD_FROZEN",
            "min_distinct_days": "TBD_FROZEN",
            "min_events_per_combination": "TBD_FROZEN",
            "uncertainty_policy": "TBD_FROZEN (pre-registered CI method)",
            "accumulated_counts": {},
        },
        "execution_model": {
            "entry": "TBD_FROZEN",
            "fees_bps": "TBD_FROZEN",
            "slippage_model": "TBD_FROZEN",
            "tp_sl_timeout": "TBD_FROZEN",
            "followup_windows": [
                "TBD_FROZEN (pre-registered follow-up horizons)",
            ],
        },
        "metrics": [
            "MAE_R",
            "MFE_R",
            "MISSED_WINNERS",
            "TIME_TO_PEAK",
            "ACCEPTED_RATE",
            "REJECTED_RATE",
        ],
        "strata": [
            "accepted",
            "rejected_with_reason_code",
            "failure_strata (HTF incomplete / API / limiter / conflict)",
        ],
        "evidence_retention": (
            "per-event H1-E ledger rows + H1D_HTF_SNAPSHOT_2 provenance "
            "hashes retained for the experiment lifetime"),
        "verdict_gate": (
            "no OOS verdict is issued by this contour; a verdict requires "
            "the frozen evaluation policy and a separate GO"),
    }


def assert_proposal_only(template: dict[str, Any]) -> None:
    """Fail closed unless ``template`` is an unmodified proposal."""
    if template.get("status") != TEMPLATE_STATUS:
        raise ValueError("template status must remain PROPOSAL_ONLY_NOT_FROZEN")
    if template.get("outcome_computation") != OUTCOME_COMPUTATION:
        raise ValueError("outcome computation is forbidden in Phase A")
    if not template.get("phase_bc_hold"):
        raise ValueError("template must carry the explicit Phase B/C hold")

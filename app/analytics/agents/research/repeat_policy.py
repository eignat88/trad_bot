"""Repeat policy for research findings.

Evaluates whether a finding should transition based on its occurrence
history.  The policy is purely computational — no DB writes, no
side effects.  The caller (ingestion service) is responsible for
executing the recommended transition via the repository.

CRITICAL DESIGN — NO production defaults:
    All thresholds default to 0 (disabled).  If the config is absent or
    all thresholds are 0, promotion is DISABLED.  This prevents
    unintended auto-promotion in production without explicit operator
    configuration.

Independent occurrence counting:
    - Only distinct business_date OR distinct non-overlapping
      analysis_run_id counts as an independent observation.
    - PROVISIONAL + FINAL of the same business_date = 1 independent
      observation (FINAL supersedes).
    - Retry/replay/repaired with the same analysis_run_id = NOT
      independent.
    - Confidence filter: only occurrences meeting min_confidence count.

Transition rules (FindingRepeatPolicyV1):
    OPEN → REPEATED        when independent occurrences ≥ repeated_min_independent_occurrences
    REPEATED → RESEARCH_REQUIRED when independent occurrences ≥ research_required_min_occurrences
                              AND distinct business dates ≥ min_distinct_business_dates
    No shortcut OPEN → RESEARCH_REQUIRED (must pass through REPEATED first)
"""
from __future__ import annotations

from dataclasses import dataclass


# ======================================================================
# Confidence ranking for threshold comparison
# ======================================================================

_CONFIDENCE_RANK: dict[str, int] = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
}

PROMOTION_DISABLED = "PROMOTION_DISABLED"


def _confidence_meets_threshold(confidence: str, minimum: str) -> bool:
    """Return True if confidence meets or exceeds the minimum threshold."""
    rank = _CONFIDENCE_RANK.get(confidence.upper(), 0)
    min_rank = _CONFIDENCE_RANK.get(minimum.upper(), 0)
    return rank >= min_rank


# ======================================================================
# Configuration — NO production defaults
# ======================================================================

@dataclass(frozen=True)
class FindingRepeatPolicyConfig:
    """Tunable thresholds. NO implicit defaults.

    If config is absent/disabled, promotion is DISABLED.

    All thresholds default to 0 = disabled.
    """

    repeated_min_independent_occurrences: int = 0   # 0 = disabled
    research_required_min_occurrences: int = 0      # 0 = disabled
    min_distinct_business_dates: int = 0
    min_confidence: str = "MEDIUM"


# ======================================================================
# Policy evaluation
# ======================================================================

# Possible return values
KEEP_OPEN = "KEEP_OPEN"
MARK_REPEATED = "MARK_REPEATED"
MARK_RESEARCH_REQUIRED = "MARK_RESEARCH_REQUIRED"
NO_CHANGE = "NO_CHANGE"


class FindingRepeatPolicyV1:
    """Deterministic repeat-policy evaluator.

    Each call receives the *complete* occurrence list for a single finding
    and its current status, and returns a recommended action.

    Parameters:
        config:         Tunable thresholds (all 0 = disabled)
        occurrences:    List of dicts, each with at least:
                          - confidence: str (LOW/MEDIUM/HIGH)
                          - business_date: str | None (YYYY-MM-DD)
                          - analysis_run_id: str | None (UUID)
                          - maturity: str (PROVISIONAL/FINAL)
        current_status: Current finding status (OPEN, REPEATED, RESEARCH_REQUIRED, CLOSED)

    Returns:
        One of: KEEP_OPEN, MARK_REPEATED, MARK_RESEARCH_REQUIRED,
                NO_CHANGE, PROMOTION_DISABLED
    """

    @classmethod
    def evaluate(
        cls,
        config: FindingRepeatPolicyConfig | None,
        occurrences: list[dict],
        current_status: str,
    ) -> str:
        """Evaluate the repeat policy and return a recommended action.

        Independent occurrence counting algorithm:
            1. Filter occurrences by min_confidence.
            2. Group qualifying occurrences by business_date.
            3. For each business_date, pick FINAL if exists, else
               PROVISIONAL.  This gives at most one observation per date.
            4. For occurrences without a business_date, group by
               analysis_run_id (only distinct run_ids count).
            5. The total distinct independent observations is the sum.
            6. Apply thresholds.

        If config is None or any threshold is 0 → return PROMOTION_DISABLED.
        """
        # No config or all thresholds zero → promotion disabled
        if config is None:
            return PROMOTION_DISABLED

        if (
            config.repeated_min_independent_occurrences == 0
            and config.research_required_min_occurrences == 0
            and config.min_distinct_business_dates == 0
        ):
            return PROMOTION_DISABLED

        # Terminal state — no transitions from CLOSED
        if current_status == "CLOSED":
            return NO_CHANGE

        # Already at RESEARCH_REQUIRED — no further promotion
        if current_status == "RESEARCH_REQUIRED":
            return NO_CHANGE

        # Filter occurrences by confidence threshold
        qualifying = [
            occ for occ in occurrences
            if _confidence_meets_threshold(
                occ.get("confidence", "LOW"),
                config.min_confidence,
            )
        ]

        # ── Count independent occurrences ────────────────────────────
        # Step 1: Group by business_date
        by_date: dict[str, list[dict]] = {}
        no_date: list[dict] = []

        for occ in qualifying:
            bd = occ.get("business_date")
            if bd:
                by_date.setdefault(str(bd), []).append(occ)
            else:
                no_date.append(occ)

        # Step 2: For each business_date, pick FINAL if exists, else PROVISIONAL
        independent_count = 0
        distinct_dates: set[str] = set()

        for date_key, date_occs in by_date.items():
            # Prefer FINAL over PROVISIONAL
            final_occs = [o for o in date_occs if o.get("maturity", "").upper() == "FINAL"]
            if final_occs:
                # This date contributes exactly 1 independent observation
                independent_count += 1
                distinct_dates.add(date_key)
            else:
                # All PROVISIONAL for this date → still 1 independent observation
                independent_count += 1
                distinct_dates.add(date_key)

        # Step 3: For occurrences without business_date, count by distinct
        # analysis_run_id (retry/replay/repaired same run_id = NOT independent)
        seen_run_ids: set[str] = set()
        for occ in no_date:
            run_id = occ.get("analysis_run_id")
            if run_id:
                run_id_str = str(run_id)
                if run_id_str not in seen_run_ids:
                    seen_run_ids.add(run_id_str)
                    independent_count += 1
            else:
                # No business_date AND no analysis_run_id — count as independent
                independent_count += 1

        qualifying_count = independent_count
        distinct_dates_count = len(distinct_dates)

        # ------------------------------------------------------------------
        # Evaluation logic
        # ------------------------------------------------------------------

        if current_status == "OPEN":
            # Check if we should promote to REPEATED
            if config.repeated_min_independent_occurrences > 0:
                if qualifying_count >= config.repeated_min_independent_occurrences:
                    return MARK_REPEATED
            return KEEP_OPEN

        if current_status == "REPEATED":
            # Check if we should promote to RESEARCH_REQUIRED
            if (
                config.research_required_min_occurrences > 0
                and config.min_distinct_business_dates > 0
            ):
                if (
                    qualifying_count >= config.research_required_min_occurrences
                    and distinct_dates_count >= config.min_distinct_business_dates
                ):
                    return MARK_RESEARCH_REQUIRED
            return NO_CHANGE

        # Unknown status — no action
        return NO_CHANGE

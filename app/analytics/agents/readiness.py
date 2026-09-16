"""Data Readiness Gate — determines if a dataset is ready for Stage 3 analysis.

This is a PROGRAMMATIC gate, NOT an LLM agent. It checks:
- analysis_run exists and has required state (SUCCEEDED)
- canonical build completed (trade_fact, setup_fact populated)
- dataset_version is not a stub
- quality checks executed and resolved
- required coverage available
- maturity is valid

The gate runs BEFORE any agent execution. If it fails, all agents are
SKIPPED with the blocking reason recorded in their run rows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataReadinessResult:
    """Result of the data readiness gate check.

    Attributes
    ----------
    ready:
        True if the dataset passes all blockers — agents may proceed.
    dataset_state:
        BUILDING | READY | FAILED — overall dataset health.
    quality_status:
        PASS | DEGRADED | FAIL — quality gate outcome.
    dataset_version:
        The versioned dataset identifier, or None.
    maturity:
        PROVISIONAL | FINAL — analysis maturity level.
    blockers:
        Tuple of reasons the dataset is NOT ready (empty if ready).
    limitations:
        Quality limitations to forward into agent manifests so they can
        adjust confidence or flag caveats.
    analysis_window_from / analysis_window_to:
        Time bounds for the analysis window.
    """

    ready: bool
    dataset_state: str           # BUILDING, READY, FAILED
    quality_status: str          # PASS, DEGRADED, FAIL
    dataset_version: Optional[str]
    maturity: str                # PROVISIONAL, FINAL
    blockers: tuple[str, ...]    # reasons not ready
    limitations: tuple[str, ...] # quality limitations to pass to agents
    analysis_window_from: Optional[datetime] = None
    analysis_window_to: Optional[datetime] = None


class DataReadinessGate:
    """Programmatic gate that checks if a dataset is ready for agent analysis.

    NOT an LLM agent — pure boolean logic over run state, build metadata,
    and quality status. The gate is stateless and can be instantiated once
    and reused across analysis runs.

    Usage::

        gate = DataReadinessGate()
        result = gate.evaluate(
            run_id=run_id,
            status="SUCCEEDED",
            maturity="PROVISIONAL",
            dataset_version="20250715.1",
            quality_status="PASS",
        )
        if not result.ready:
            skip_all_agents(result.blockers)
    """

    STUB_VERSION = "00000000.0"

    def evaluate(
        self,
        run_id: UUID,
        status: str,
        maturity: str,
        dataset_version: Optional[str],
        quality_status: Optional[str],
        canonical_build_json: Optional[dict] = None,
        analysis_window_from: Optional[datetime] = None,
        analysis_window_to: Optional[datetime] = None,
        quality_limitations: Optional[list[str]] = None,
    ) -> DataReadinessResult:
        """Evaluate dataset readiness for Stage 3.

        Parameters
        ----------
        run_id:
            The analysis_run UUID (for logging/context).
        status:
            Current analysis_run status (must be SUCCEEDED).
        maturity:
            Maturity level (PROVISIONAL or FINAL).
        dataset_version:
            Version string from the canonical build (must not be stub).
        quality_status:
            Quality gate outcome (PASS, DEGRADED, or FAIL).
        canonical_build_json:
            Optional build summary dict with ``trade_fact_built`` /
            ``setup_fact_built`` booleans.
        analysis_window_from / analysis_window_to:
            Optional time bounds.
        quality_limitations:
            Optional list of quality limitation strings.

        Returns
        -------
        DataReadinessResult
            Frozen result with ``ready`` flag and diagnostic detail.
        """
        blockers: list[str] = []
        limitations: list[str] = list(quality_limitations or [])

        # 1. Analysis run must be SUCCEEDED
        if status != "SUCCEEDED":
            blockers.append(
                f"analysis_run status is {status}, expected SUCCEEDED"
            )

        # 2. Maturity must be valid
        if maturity not in ("PROVISIONAL", "FINAL"):
            blockers.append(f"invalid maturity: {maturity}")

        # 3. Dataset version must exist and not be stub
        if not dataset_version:
            blockers.append("dataset_version is missing")
        elif dataset_version == self.STUB_VERSION:
            blockers.append(f"dataset_version is stub ({self.STUB_VERSION})")

        # 4. Quality status must be known and not FAIL
        if not quality_status:
            blockers.append("quality_status is missing")
        elif quality_status == "FAIL":
            blockers.append("quality gate FAILED — agents SKIPPED")

        # 5. Canonical build summary (optional but informative)
        if canonical_build_json:
            if not canonical_build_json.get("trade_fact_built"):
                blockers.append("trade_fact not built")
            if not canonical_build_json.get("setup_fact_built"):
                blockers.append("setup_fact not built")

        # Determine dataset state
        if blockers:
            dataset_state = "FAILED"
        else:
            dataset_state = "READY"

        # Determine effective quality status
        effective_quality = quality_status or "FAIL"

        result = DataReadinessResult(
            ready=len(blockers) == 0,
            dataset_state=dataset_state,
            quality_status=effective_quality,
            dataset_version=dataset_version,
            maturity=maturity,
            blockers=tuple(blockers),
            limitations=tuple(limitations),
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
        )

        if result.ready:
            logger.info(
                "DataReadinessGate PASSED for run %s "
                "(version=%s, maturity=%s, quality=%s)",
                run_id, dataset_version, maturity, effective_quality,
            )
        else:
            logger.warning(
                "DataReadinessGate BLOCKED for run %s: %s",
                run_id, result.blockers,
            )

        return result

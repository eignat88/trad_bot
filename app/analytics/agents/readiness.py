"""Data Readiness Gate — determines if a dataset is ready for Stage 3 analysis.

This is a PROGRAMMATIC gate, NOT an LLM agent. It checks:
- analysis_run exists and has required state (SUCCEEDED)
- publication exists and has status READY
- canonical build completed (trade_fact, setup_fact populated)
- dataset_version is not a stub
- quality checks executed and resolved
- required coverage available
- maturity is valid

The gate runs BEFORE any agent execution. If it fails, all agents are
SKIPPED with the blocking reason recorded in their run rows.

Two entry points
-----------------
``evaluate()``   — inner business-rule validator.  Accepts resolved data
                    directly; kept backward-compatible for unit tests and
                    callers that already hold the authoritative state.

``evaluate_from_db()`` — **preferred orchestrator entry point**.  Loads
                    authoritative state from the repository, then delegates
                    to ``evaluate()``.  This prevents the orchestrator from
                    passing stale or caller-supplied data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID

logger = logging.getLogger(__name__)


# ── Repository protocol (subset needed by evaluate_from_db) ───────────

@runtime_checkable
class ReadinessRepositoryProtocol(Protocol):
    """Minimal repository interface required by ``evaluate_from_db``."""

    def get_analysis_run(self, run_id: UUID) -> Any: ...
    def get_dataset_publication(
        self, analysis_run_id: UUID, maturity: str
    ) -> Optional[dict]: ...
    def get_quality_results(self, run_id: UUID) -> list[Any]: ...


# ── Result ───────────────────────────────────────────────────────────

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


# ── Gate ─────────────────────────────────────────────────────────────

class DataReadinessGate:
    """Programmatic gate that checks if a dataset is ready for agent analysis.

    NOT an LLM agent — pure boolean logic over run state, build metadata,
    and quality status. The gate is stateless and can be instantiated once
    and reused across analysis runs.

    Usage (direct)::

        gate = DataReadinessGate()
        result = gate.evaluate(
            run_id=run_id,
            status="SUCCEEDED",
            maturity="PROVISIONAL",
            publication_status="READY",
            dataset_version="20250715.1",
            quality_status="PASS",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": True},
        )
        if not result.ready:
            skip_all_agents(result.blockers)

    Usage (DB-backed, preferred for orchestrators)::

        gate = DataReadinessGate()
        result = gate.evaluate_from_db(
            repository=repo,
            analysis_run_id=run_id,
            maturity="PROVISIONAL",
        )
        if not result.ready:
            skip_all_agents(result.blockers)
    """

    STUB_VERSION = "00000000.0"

    # ── evaluate_from_db (preferred entry point) ───────────────────

    def evaluate_from_db(
        self,
        repository: ReadinessRepositoryProtocol,
        analysis_run_id: UUID,
        maturity: str,
    ) -> DataReadinessResult:
        """Load authoritative state from the DB and evaluate readiness.

        This is the **preferred** entry point for orchestrators.  It ensures
        the gate inspects the actual persisted state rather than whatever
        the caller happens to hold.

        Parameters
        ----------
        repository:
            Must implement ``get_analysis_run``, ``get_dataset_publication``,
            and ``get_quality_results``.
        analysis_run_id:
            The analysis_run UUID.
        maturity:
            PROVISIONAL or FINAL.

        Returns
        -------
        DataReadinessResult
        """

        # 1. Load analysis run from DB
        analysis_run = repository.get_analysis_run(analysis_run_id)
        if analysis_run is None:
            # Cannot even load the run — immediate block
            return DataReadinessResult(
                ready=False,
                dataset_state="FAILED",
                quality_status="FAIL",
                dataset_version=None,
                maturity=maturity,
                blockers=(f"analysis_run {analysis_run_id} not found",),
                limitations=(),
            )

        # Extract authoritative fields from the run object
        run_status = (
            analysis_run.status.value
            if hasattr(analysis_run.status, "value")
            else str(analysis_run.status)
        )

        # 2. Load dataset publication
        publication = repository.get_dataset_publication(analysis_run_id, maturity)

        if publication is None:
            # No publication yet — block with clear reason
            return DataReadinessResult(
                ready=False,
                dataset_state="FAILED",
                quality_status="FAIL",
                dataset_version=None,
                maturity=maturity,
                blockers=(
                    f"dataset_publication not found for run {analysis_run_id} "
                    f"maturity={maturity}",
                ),
                limitations=(),
            )

        # 3. Extract publication fields
        publication_status = publication.get("status", "BUILDING")
        quality_status = publication.get("quality_status")
        dataset_version = publication.get("dataset_version")
        canonical_build_json = publication.get("canonical_build_json")
        analysis_window_from = publication.get("analysis_window_from")
        analysis_window_to = publication.get("analysis_window_to")

        # 4. Load quality results and derive limitations
        quality_results = repository.get_quality_results(analysis_run_id)
        quality_limitations: list[str] = []
        for qr in quality_results:
            details = qr.details if hasattr(qr, "details") else (qr.get("details") if isinstance(qr, dict) else None)
            if details and isinstance(details, dict):
                limitation = details.get("limitation") or details.get("quality_limitation")
                if limitation:
                    quality_limitations.append(str(limitation))

        # 5. Derive quality_status from publication (authoritative)
        #    — already extracted above

        # 6. Delegate to inner business-rule evaluator
        return self.evaluate(
            run_id=analysis_run_id,
            status=run_status,
            maturity=maturity,
            publication_status=publication_status,
            dataset_version=dataset_version,
            quality_status=quality_status,
            canonical_build_json=canonical_build_json,
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
            quality_limitations=quality_limitations or None,
        )

    # ── evaluate (inner business-rule validator) ───────────────────

    def evaluate(
        self,
        run_id: UUID,
        status: str,
        maturity: str,
        dataset_version: Optional[str],
        quality_status: Optional[str],
        publication_status: Optional[str] = None,
        canonical_build_json: Optional[dict] = None,
        analysis_window_from: Optional[datetime] = None,
        analysis_window_to: Optional[datetime] = None,
        quality_limitations: Optional[list[str]] = None,
    ) -> DataReadinessResult:
        """Evaluate dataset readiness for Stage 3.

        This is the inner business-rule validator.  Callers that already hold
        authoritative data may call this directly; otherwise prefer
        ``evaluate_from_db()``.

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
        publication_status:
            Publication lifecycle status.  **REQUIRED** — must be READY.
            When None or not READY, the gate blocks.
        canonical_build_json:
            Build summary dict with ``trade_fact_built`` /
            ``setup_fact_built`` booleans.  **REQUIRED** — when None or
            missing, the gate blocks.
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

        # 3. Publication status must be READY
        if not publication_status:
            blockers.append(
                "publication_status is missing — dataset_publication "
                "must exist and have status READY"
            )
        elif publication_status != "READY":
            blockers.append(
                f"publication_status is {publication_status}, expected READY"
            )

        # 4. Dataset version must exist and not be stub
        if not dataset_version:
            blockers.append("dataset_version is missing")
        elif dataset_version == self.STUB_VERSION:
            blockers.append(f"dataset_version is stub ({self.STUB_VERSION})")

        # 5. Quality status must be known and not FAIL
        if not quality_status:
            blockers.append("quality_status is missing")
        elif quality_status == "FAIL":
            blockers.append("quality gate FAILED — agents SKIPPED")

        # 6. Canonical build summary — REQUIRED
        if not canonical_build_json:
            blockers.append(
                "canonical_build_json is missing — build must complete "
                "before agents can proceed"
            )
        else:
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
                "(version=%s, maturity=%s, quality=%s, pub_status=%s)",
                run_id, dataset_version, maturity, effective_quality,
                publication_status,
            )
        else:
            logger.warning(
                "DataReadinessGate BLOCKED for run %s: %s",
                run_id, result.blockers,
            )

        return result

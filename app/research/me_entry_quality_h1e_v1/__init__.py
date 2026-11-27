"""Issue #324 — H1-E: Prospective H1 4H/1D Classification Observer V1
(Phase A — OFFLINE IMPLEMENTATION ONLY).

Isolated, prospective-only RESEARCH observer that will eventually record
causal 4H/1D regime, BOS and coverage for NEW upstream events from the
independent #305 pre-scoring source
``research_me_upstream_coverage.source_event`` — after its own separately
approved H1-E freeze.  This is NOT a trade signal and NOT an OOS
profitability verdict.

Phase A scope guarantees (hard):

* RESEARCH_ONLY / NO_DEPLOY / NO_PRODUCTION_DB_WRITES / NO_DDL_APPLY /
  NO_LIVE_BYBIT_FETCH / NO_EXISTING_SERVICE_RESTART / NO_BACKFILL /
  NO_OOS_ACTIVATION;
* only events AFTER the future, separately approved H1-E ``freeze_ts``
  (``signal_time > freeze_ts`` strictly) are ever admitted; Phase A mints
  NO freeze — synthetic fixture freezes are refused outside offline tests;
* the historical 2026-11-25 SOXSUSDT event is a QA example only and is
  excluded from every prospective denominator;
* ``source_event_key`` / ``source_identity`` / ``signal_time`` (µs) /
  ``lineage_hash`` are preserved VERBATIM from #305 — never rebuilt from
  mutable presentation fields;
* the classification ASOF is the ORIGINAL ``signal_time`` (no look-ahead);
  only candles with ``close_us <= signal_time_us`` (inclusive at the exact
  close boundary) are admissible;
* fail-closed everywhere: missing candles, limiter/breaker denial, 429 /
  10006, API failure, timeout, invalid OHLCV, 59/60 coverage, identity
  conflicts and snapshot tamper are explicit non-eligible results with
  evidence — never a market mode;
* no imports/wiring from scanner / paper / live execution / evaluator
  contours; no production DDL execution; no service or environment
  changes; no systemd; no activation flags.

Modules (all pure or injectable — zero network, zero production DB in
Phase A):

* ``protocol``          — frozen H1-E protocol identity + preserved #305
  facts (freeze, UNVERIFIED window, SOXSUSDT QA exclusion);
* ``contracts``         — immutable event/freeze/admission contracts;
* ``source_readiness``  — ``H1E_SOURCE_READINESS_V1`` readiness report;
* ``capture``           — ``H1E_PROSPECTIVE_EVENT_CAPTURE_V1`` bounded
  worker with cursor / idempotency / atomic checkpoint;
* ``classification``    — ``H1E_HTF_ASOF_CLASSIFICATION_V1`` ASOF 4H/1D
  classification via frozen H1-C/H1-D contracts + provenance snapshots;
* ``ledger``            — ``H1E_PROSPECTIVE_LEDGER_V1`` append-only ledger
  reference semantics (DDL proposal — DO NOT APPLY);
* ``coverage_health``   — ``H1E_COVERAGE_HEALTH_V1`` five-dimension
  reconciliation;
* ``oos_readiness``     — ``H1E_OOS_READINESS_V1`` pre-registration
  template (proposal only, no outcome computation).

Status: ``H1E_PHASE_A_OFFLINE_IMPLEMENTATION / NO_DEPLOY / NOT_ACTIVATED /
H1_OOS_HOLD``.  Phase B/C/D/E and any merge/deploy require separate
explicit owner authorization.
"""
from __future__ import annotations

from .capture import (
    CaptureConfig,
    CaptureCursor,
    ConflictRecord,
    CycleReport,
    H1E_CAPTURE_VERSION,
    LedgerWrite,
    PendingRetry,
    PendingRetryUpdate,
    RejectionRecord,
    run_capture_cycle,
)
from .classification import (
    ClassificationOutcome,
    ClassificationRecord,
    H1E_CLASSIFICATION_VERSION,
    HtfPayload,
    NONELIGIBLE_RETRY_BUDGET_EXHAUSTED,
    RetryPolicy,
    classify_h1e_event,
    terminal_noneligible_record,
)
from .contracts import (
    AdmissionDecision,
    H1EFreeze,
    H1ESourceEvent,
    admit_source_row,
    ledger_identity,
    preserved_identity_hash,
)
from .coverage_health import (
    ALL_DIMENSIONS,
    DimensionReport,
    H1E_COVERAGE_HEALTH_VERSION,
    HealthReport,
    LedgerEventView,
    SourceEventView,
    reconcile,
)
from .ledger import (
    H1E_LEDGER_VERSION,
    ActivationFingerprintMismatch,
    FreezeTsMismatch,
    LedgerEntry,
    LedgerIdentityConflict,
    InMemoryH1EStore,
    NoApprovedActivation,
    PreFreezeWrite,
)
from .oos_readiness import (
    H1E_OOS_READINESS_VERSION,
    build_preregistration_template,
)
from .protocol import (
    EXPERIMENT_ID,
    H1E_FREEZE_NOT_MINTED,
    H1E_PROTOCOL_VERSION,
    PHASE_A_STATUS,
    protocol_fingerprint,
)
from .source_readiness import (
    H1E_SOURCE_READINESS_VERSION,
    ReadinessCheck,
    ReadinessReport,
    build_readiness_report,
)

__all__ = [
    "ALL_DIMENSIONS",
    "ActivationFingerprintMismatch",
    "AdmissionDecision",
    "CaptureConfig",
    "CaptureCursor",
    "ClassificationOutcome",
    "ClassificationRecord",
    "ConflictRecord",
    "CycleReport",
    "DimensionReport",
    "EXPERIMENT_ID",
    "FreezeTsMismatch",
    "H1E_CAPTURE_VERSION",
    "H1E_CLASSIFICATION_VERSION",
    "H1E_COVERAGE_HEALTH_VERSION",
    "H1E_FREEZE_NOT_MINTED",
    "H1E_LEDGER_VERSION",
    "H1E_OOS_READINESS_VERSION",
    "H1E_PROTOCOL_VERSION",
    "H1E_SOURCE_READINESS_VERSION",
    "H1EFreeze",
    "H1ESourceEvent",
    "HealthReport",
    "HtfPayload",
    "InMemoryH1EStore",
    "LedgerEntry",
    "LedgerEventView",
    "LedgerIdentityConflict",
    "LedgerWrite",
    "NONELIGIBLE_RETRY_BUDGET_EXHAUSTED",
    "NoApprovedActivation",
    "PHASE_A_STATUS",
    "PendingRetry",
    "PendingRetryUpdate",
    "PreFreezeWrite",
    "ReadinessCheck",
    "ReadinessReport",
    "RejectionRecord",
    "RetryPolicy",
    "SourceEventView",
    "admit_source_row",
    "build_preregistration_template",
    "build_readiness_report",
    "classify_h1e_event",
    "ledger_identity",
    "preserved_identity_hash",
    "protocol_fingerprint",
    "reconcile",
    "run_capture_cycle",
    "terminal_noneligible_record",
]

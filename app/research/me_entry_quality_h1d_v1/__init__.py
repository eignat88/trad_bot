"""Issue #314 — H1-D: historical HTF fetch & real event classification
(RESEARCH_ONLY, NO_DEPLOY).

Bounded, research-only historical Bybit V5 kline fetch for the 4H
(``interval=240``) and native 1D (``interval=D``) timeframes, causal-ASOF
coverage at ``source_event.signal_time`` (µs precision), immutable
provenance snapshots with offline byte-identical replay, and real-event
classification of #305 source events through the FROZEN H1-C contracts
(``build_source_identity()`` / ``provide_htf_candles()`` / ``run_h1_event()``
reused UNCHANGED).

Scope guarantees (Issue #314 / #315 hard gates):

* RESEARCH_ONLY / READ_ONLY_SOURCE — not wired into any scanner,
  orchestrator, trading gate, service unit, migration, observer or OOS
  registry; no production call path is modified and
  ``BybitClient.get_klines_range()`` semantics are untouched (native ``D``
  support lives ONLY in :mod:`htf_fetch` here);
* the only request path is ``BybitClient._public_get()`` — its shared
  global limiter, retry budget and circuit breaker are reused; every
  429 / ``retCode=10006`` / limiter / breaker denial FAILS CLOSED with an
  explicit error and rows are never fabricated;
* classification ASOF = ``source_event.signal_time`` (NEVER ``captured_at``),
  only FULLY CLOSED candles on the UTC 4H/1D grids, canonical 60/60 window,
  no synthetic filling and no look-ahead;
* provenance snapshots are immutable (canonical SHA-256 + tamper detection)
  and replay is byte-identical with ZERO HTTP;
* real Bybit fetch requires SEPARATE OWNER APPROVAL AFTER REVIEW — the
  runner refuses without an explicit authorization flag AND an
  owner-approval sentinel file; the #305 ``freeze_ts`` is untouched and
  there is no backfill, no OOS activation and NO_DEPLOY.

Modules:

* ``htf_fetch``       — ``H1D_HTF_4H_FETCH_V1`` / ``H1D_HTF_1D_FETCH_V1``
  bounded research fetch (fail-closed errors, bounded pagination);
* ``coverage``        — ``H1D_CAUSAL_ASOF_COVERAGE_V1`` causal ASOF (µs)
  canonical 60/60 coverage with exact diagnostics;
* ``provenance``      — ``H1D_PROVENANCE_REPLAY_V1`` immutable snapshots,
  tamper detection and offline byte-identical replay;
* ``classification``  — ``H1D_REAL_EVENT_CLASSIFICATION_V1`` per-TF
  regime/BOS, combined state, coverage, eligibility + diagnostics;
* ``runner``          — read-only CLI (offline ``--replay`` default;
  ``--fetch`` guarded by explicit authorization + sentinel file).
"""
from __future__ import annotations

from .classification import (
    H1D_CLASSIFICATION_VERSION,
    H1DClassification,
    classify_event,
)
from .coverage import (
    CANONICAL_LOOKBACK,
    COVERAGE_COMPLETE,
    COVERAGE_INCOMPLETE,
    COVERAGE_INVALID,
    H1D_COVERAGE_VERSION,
    CoverageResult,
    canonical_window_bounds,
    evaluate_coverage,
    is_closed_at,
    latest_closed_open_ms,
)
from .htf_fetch import (
    DEFAULT_CALL_TIMEOUT_S,
    HARD_REQUEST_CAP,
    H1D_HTF_1D_FETCH_VERSION,
    H1D_HTF_4H_FETCH_VERSION,
    H1D_HTF_FETCH_ADAPTER_VERSION,
    HtfFetchApiError,
    HtfFetchBreakerDeniedError,
    HtfFetchError,
    HtfFetchLimiterDeniedError,
    HtfFetchRateLimitError,
    HtfFetchResult,
    HtfFetchRowConflictError,
    HtfFetchRequestCapExceeded,
    HtfFetchTimeoutError,
    fetch_htf_history,
    normalize_kline_row,
)
from .provenance import (
    FIXTURE_LABEL_REAL,
    FIXTURE_LABEL_SYNTHETIC,
    H1D_PROVENANCE_VERSION,
    ProvenanceError,
    ProvenanceTamperError,
    Snapshot,
    TimeframePayload,
    build_snapshot,
    canonical_output_bytes,
    load_snapshot,
    payload_sha256,
    replay_classification,
    save_snapshot,
    snapshot_from_dict,
)

__all__ = [
    "CANONICAL_LOOKBACK",
    "COVERAGE_COMPLETE",
    "COVERAGE_INCOMPLETE",
    "COVERAGE_INVALID",
    "CoverageResult",
    "DEFAULT_CALL_TIMEOUT_S",
    "FIXTURE_LABEL_REAL",
    "FIXTURE_LABEL_SYNTHETIC",
    "H1D_CLASSIFICATION_VERSION",
    "H1DClassification",
    "H1D_COVERAGE_VERSION",
    "H1D_HTF_1D_FETCH_VERSION",
    "H1D_HTF_4H_FETCH_VERSION",
    "H1D_HTF_FETCH_ADAPTER_VERSION",
    "H1D_PROVENANCE_VERSION",
    "HARD_REQUEST_CAP",
    "HtfFetchApiError",
    "HtfFetchBreakerDeniedError",
    "HtfFetchError",
    "HtfFetchLimiterDeniedError",
    "HtfFetchRateLimitError",
    "HtfFetchResult",
    "HtfFetchRowConflictError",
    "HtfFetchRequestCapExceeded",
    "HtfFetchTimeoutError",
    "ProvenanceError",
    "ProvenanceTamperError",
    "Snapshot",
    "TimeframePayload",
    "build_snapshot",
    "canonical_output_bytes",
    "canonical_window_bounds",
    "classify_event",
    "evaluate_coverage",
    "fetch_htf_history",
    "is_closed_at",
    "latest_closed_open_ms",
    "load_snapshot",
    "normalize_kline_row",
    "payload_sha256",
    "replay_classification",
    "save_snapshot",
    "snapshot_from_dict",
]

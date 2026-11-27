"""Issue #324 / H1-E.3 — ``H1E_HTF_ASOF_CLASSIFICATION_V1``.

ASOF 4H/1D classification of ONE prospective source event over **60 closed
candles per timeframe**, reusing the frozen contracts UNCHANGED:

* H1-D ``240`` / native ``D`` causal-ASOF coverage
  (``me_entry_quality_h1d_v1.coverage``);
* H1-C ``run_h1_event()`` pipeline (``me_entry_quality_h1c_v1.pipeline``);
* H1-D ``H1D_HTF_SNAPSHOT_2`` provenance
  (``me_entry_quality_h1d_v1.provenance``).

Hard rules (Issue #324 spec):

* classification ASOF = the ORIGINAL ``signal_time`` at microsecond
  precision — NEVER ``captured_at`` / delivery time (no look-ahead);
* HTF candles are admissible iff ``close_us <= signal_time_us``
  (inclusive at the exact close boundary — the frozen H1-C/H1-D contract);
* limiter denial, HTTP 429 / ``retCode=10006``, API failure, timeout,
  malformed / duplicate / off-grid / invalid-OHLCV candles, 59/60 coverage,
  snapshot tamper and lineage conflict are EXPLICIT non-eligible results
  with evidence — a failed source NEVER becomes a market mode;
* all nine valid 4H×1D regime combinations are retained verbatim — never
  merged, never averaged;
* transport is INJECTABLE (offline mock in Phase A; the real Bybit adapter
  is Phase B and is deliberately absent here).  Retry work is bounded;
  limiter / breaker denials DEFER within a bounded budget and then fail
  closed with an explicit reason.

Pure except for the caller-supplied transport and ``sleep`` callable — no
network, no database, no clock, no wall-time reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.research.me_entry_quality_h1_v1.classifier import (
    REGIME_INVALID_SOURCE,
)
from app.research.me_entry_quality_h1c_v1.event_adapter import SourceEventRecord
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    build_source_identity,
)
from app.research.me_entry_quality_h1d_v1.classification import (
    H1DClassification,
    classify_event,
)
from app.research.me_entry_quality_h1d_v1.coverage import (
    CANONICAL_LOOKBACK,
    COVERAGE_COMPLETE,
    TIMEFRAME_1D,
    TIMEFRAME_4H,
    TIMEFRAME_MS,
    is_closed_at,
)
from app.research.me_entry_quality_h1d_v1.provenance import (
    FIXTURE_LABEL_REAL,
    FIXTURE_LABEL_SYNTHETIC,
    Snapshot,
    build_snapshot,
    canonical_output_bytes,
    snapshot_from_dict,
)
from app.research.me_entry_quality_h1e_v1.contracts import H1ESourceEvent

H1E_CLASSIFICATION_VERSION = "H1E_HTF_ASOF_CLASSIFICATION_V1"

# ─── transport contract (INJECTABLE — Phase A uses offline mocks only) ──────

#: The frozen H1-D interval ids: ``240`` (4H) and native ``D`` (1D).
INTERVAL_4H = "240"
INTERVAL_1D = "D"


class HtfLimiterDeniedError(RuntimeError):
    """The SHARED Bybit limiter denied the request (bounded deferral)."""


class HtfRateLimitError(RuntimeError):
    """HTTP 429 or ``retCode=10006`` rate limit (bounded deferral)."""


class HtfBreakerDeniedError(RuntimeError):
    """The circuit breaker is open (bounded deferral)."""


class HtfTimeoutError(RuntimeError):
    """The HTF fetch timed out (bounded deferral)."""


class HtfTransportApiError(RuntimeError):
    """A non-retryable transport/API failure (terminal, fail closed)."""


#: Errors that may be retried/deferred within a bounded budget.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    HtfLimiterDeniedError,
    HtfRateLimitError,
    HtfBreakerDeniedError,
    HtfTimeoutError,
)


@dataclass(frozen=True)
class HtfPayload:
    """One delivered HTF payload from the injected transport."""

    rows: tuple[Mapping[str, Any], ...]
    source_kind: str
    source_label: str
    fixture_label: str
    fetched_at_ms: int | None = None

    def __post_init__(self) -> None:
        if self.fixture_label not in (FIXTURE_LABEL_SYNTHETIC, FIXTURE_LABEL_REAL):
            raise ValueError(f"unknown fixture_label {self.fixture_label!r}")


class HtfTransport(Protocol):
    """Minimal HTF window provider (4H ``240`` / 1D ``D``).

    ``fetch_htf_window`` returns the raw candle rows for the canonical
    window ending at the last candle closed at ``asof_us``.  Implementations
    MUST NOT return rows with ``close_us > asof_us`` (the classifier drops
    and reports them defensively anyway).
    """

    def fetch_htf_window(
        self,
        *,
        symbol: str,
        interval: str,
        asof_us: int,
        lookback: int = CANONICAL_LOOKBACK,
    ) -> HtfPayload: ...


# ─── outcome contract ───────────────────────────────────────────────────────

REASON_ELIGIBLE = "H1E_ELIGIBLE"

#: terminal non-eligibility codes (explicit, never a market mode)
NONELIGIBLE_HTF_LIMITER_DENIED = "H1E_HTF_LIMITER_DENIED"
NONELIGIBLE_HTF_RATE_LIMITED = "H1E_HTF_RATE_LIMITED"
NONELIGIBLE_HTF_BREAKER_DENIED = "H1E_HTF_BREAKER_DENIED"
NONELIGIBLE_HTF_TIMEOUT = "H1E_HTF_TIMEOUT"
NONELIGIBLE_HTF_API_ERROR = "H1E_HTF_API_ERROR"
NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED = "H1E_HTF_RETRY_BUDGET_EXHAUSTED"
NONELIGIBLE_HTF_INCOMPLETE_COVERAGE = "H1E_HTF_INCOMPLETE_COVERAGE"
NONELIGIBLE_HTF_INVALID_SOURCE = "H1E_HTF_INVALID_SOURCE"
NONELIGIBLE_SNAPSHOT_TAMPER = "H1E_SNAPSHOT_TAMPER"
NONELIGIBLE_CLASSIFIER_ERROR = "H1E_CLASSIFIER_ERROR"
#: capture-level deferral budget exhausted (terminal fail-closed)
NONELIGIBLE_RETRY_BUDGET_EXHAUSTED = "H1E_RETRY_BUDGET_EXHAUSTED"

STATUS_CLASSIFIED = "CLASSIFIED"
STATUS_DEFERRED = "DEFERRED"


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry / deferral governance (mock transports only)."""

    max_attempts_per_cycle: int = 2
    backoff_base_s: float = 0.5
    max_deferrals: int = 3


DEFAULT_RETRY_POLICY = RetryPolicy()


@dataclass(frozen=True)
class ClassificationRecord:
    """Immutable H1-E classification outcome for ONE source event."""

    version: str
    source_event_key: str
    source_identity: str
    lineage_hash: str
    signal_time_us: int
    asof_us: int
    status: str  # CLASSIFIED | DEFERRED
    eligible: bool
    eligibility_reason: str
    non_eligible_codes: tuple[str, ...]
    regime_4h: str | None
    bos_4h: str | None
    regime_1d: str | None
    bos_1d: str | None
    combined_regime: str | None
    coverage_4h_status: str
    coverage_1d_status: str
    coverage_4h_present: int
    coverage_1d_present: int
    coverage_4h_missing_open_ms: tuple[int, ...]
    coverage_1d_missing_open_ms: tuple[int, ...]
    snapshot_sha256: str
    h1d_diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def final(self) -> bool:
        return self.status == STATUS_CLASSIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "lineage_hash": self.lineage_hash,
            "signal_time_us": self.signal_time_us,
            "asof_us": self.asof_us,
            "status": self.status,
            "eligible": self.eligible,
            "eligibility_reason": self.eligibility_reason,
            "non_eligible_codes": list(self.non_eligible_codes),
            "regime_4h": self.regime_4h,
            "bos_4h": self.bos_4h,
            "regime_1d": self.regime_1d,
            "bos_1d": self.bos_1d,
            "combined_regime": self.combined_regime,
            "coverage_4h_status": self.coverage_4h_status,
            "coverage_1d_status": self.coverage_1d_status,
            "coverage_4h_present": self.coverage_4h_present,
            "coverage_1d_present": self.coverage_1d_present,
            "coverage_4h_missing_open_ms": list(self.coverage_4h_missing_open_ms),
            "coverage_1d_missing_open_ms": list(self.coverage_1d_missing_open_ms),
            "snapshot_sha256": self.snapshot_sha256,
        }


@dataclass(frozen=True)
class ClassificationOutcome:
    """Deterministic result of :func:`classify_h1e_event`."""

    record: ClassificationRecord
    snapshot: Snapshot | None
    attempts: int
    deferred_reason: str = ""

    @property
    def final(self) -> bool:
        return self.record.final


# ─── the classifier ─────────────────────────────────────────────────────────


def _noneligible_record(
    event: H1ESourceEvent,
    *,
    code: str,
    detail: str,
    status: str = STATUS_CLASSIFIED,
    attempts: int = 0,
) -> ClassificationRecord:
    return ClassificationRecord(
        version=H1E_CLASSIFICATION_VERSION,
        source_event_key=event.source_event_key,
        source_identity=event.source_identity,
        lineage_hash=event.lineage_hash,
        signal_time_us=event.signal_time_us,
        asof_us=event.signal_time_us,
        status=status,
        eligible=False,
        eligibility_reason=f"{code}:{detail}" if detail else code,
        non_eligible_codes=(code,),
        regime_4h=None,
        bos_4h=None,
        regime_1d=None,
        bos_1d=None,
        combined_regime=None,
        coverage_4h_status="",
        coverage_1d_status="",
        coverage_4h_present=0,
        coverage_1d_present=0,
        coverage_4h_missing_open_ms=(),
        coverage_1d_missing_open_ms=(),
        snapshot_sha256="",
        h1d_diagnostics={"attempts": attempts},
    )


def terminal_noneligible_record(
    event: H1ESourceEvent,
    *,
    code: str,
    detail: str = "",
    attempts: int = 1,
) -> ClassificationRecord:
    """Terminal fail-closed record (bounded-work budget exhaustion etc.).

    Used by the capture worker when a deferred event exhausts its durable
    retry budget WITHOUT another classifier invocation — explicit
    non-eligible outcome, never a market mode.
    """
    return _noneligible_record(
        event, code=code, detail=detail, attempts=attempts)


def _drop_future_rows(
    rows: Sequence[Mapping[str, Any]], *, interval: str, asof_us: int,
) -> tuple[tuple[Mapping[str, Any], ...], int]:
    """Defensive ASOF truncation: keep only rows with ``close_us <= asof_us``.

    The frozen H1-C/H1-D contract is INCLUSIVE at the exact close boundary.
    Rows beyond it are dropped and counted — they can never influence the
    classification (no look-ahead).
    """
    timeframe_ms = TIMEFRAME_MS[TIMEFRAME_4H if interval == INTERVAL_4H else TIMEFRAME_1D]
    kept: list[Mapping[str, Any]] = []
    dropped = 0
    for row in rows:
        open_ms = row.get("open_ms")
        if isinstance(open_ms, int) and not isinstance(open_ms, bool):
            if not is_closed_at(open_ms, timeframe_ms, asof_us):
                dropped += 1
                continue
        kept.append(row)
    return tuple(kept), dropped


def classify_h1e_event(
    event: H1ESourceEvent,
    transport: HtfTransport,
    *,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] | None = None,
    prior_deferrals: int = 0,
    canonical_lookback: int = CANONICAL_LOOKBACK,
) -> ClassificationOutcome:
    """Classify ONE prospective event at its original ``signal_time``.

    Bounded work: at most ``retry_policy.max_attempts_per_cycle`` transport
    attempts per timeframe per cycle; retryable failures back off via the
    injected ``sleep`` and then DEFER (``status=DEFERRED``) within
    ``retry_policy.max_deferrals`` total deferrals, after which the outcome
    is terminal non-eligible ``H1E_HTF_RETRY_BUDGET_EXHAUSTED``.  A
    non-retryable failure is terminal immediately.  No row is ever
    fabricated and no failed source becomes a market mode.
    """
    if not isinstance(event, H1ESourceEvent):
        raise ValueError("event must be an H1ESourceEvent")
    asof_us = int(event.signal_time_us)

    def _fetch(interval: str) -> HtfPayload:
        last_error: BaseException | None = None
        for attempt in range(1, max(1, int(retry_policy.max_attempts_per_cycle)) + 1):
            try:
                return transport.fetch_htf_window(
                    symbol=event.symbol,
                    interval=interval,
                    asof_us=asof_us,
                    lookback=int(canonical_lookback),
                )
            except RETRYABLE_ERRORS as exc:
                last_error = exc
                if attempt < retry_policy.max_attempts_per_cycle and sleep is not None:
                    sleep(retry_policy.backoff_base_s * attempt)
            except HtfTransportApiError:
                raise
        raise last_error  # type: ignore[misc]

    def _terminal_for(exc: BaseException) -> str:
        if isinstance(exc, HtfLimiterDeniedError):
            return NONELIGIBLE_HTF_LIMITER_DENIED
        if isinstance(exc, HtfRateLimitError):
            return NONELIGIBLE_HTF_RATE_LIMITED
        if isinstance(exc, HtfBreakerDeniedError):
            return NONELIGIBLE_HTF_BREAKER_DENIED
        if isinstance(exc, HtfTimeoutError):
            return NONELIGIBLE_HTF_TIMEOUT
        return NONELIGIBLE_HTF_API_ERROR

    def _defer_or_terminal(exc: BaseException) -> ClassificationOutcome:
        code = _terminal_for(exc)
        if prior_deferrals < retry_policy.max_deferrals:
            record = _noneligible_record(
                event,
                code=code,
                detail=str(exc),
                status=STATUS_DEFERRED,
                attempts=prior_deferrals + 1,
            )
            return ClassificationOutcome(
                record=record,
                snapshot=None,
                attempts=prior_deferrals + 1,
                deferred_reason=code,
            )
        record = _noneligible_record(
            event,
            code=NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED,
            detail=f"{code}: {exc}",
            attempts=prior_deferrals + 1,
        )
        return ClassificationOutcome(
            record=record, snapshot=None, attempts=prior_deferrals + 1)

    try:
        payload_4h = _fetch(INTERVAL_4H)
        payload_1d = _fetch(INTERVAL_1D)
    except RETRYABLE_ERRORS as exc:
        return _defer_or_terminal(exc)
    except HtfTransportApiError as exc:
        record = _noneligible_record(
            event, code=NONELIGIBLE_HTF_API_ERROR, detail=str(exc))
        return ClassificationOutcome(record=record, snapshot=None, attempts=1)

    # ASOF truncation (defensive; inclusive close boundary) + provenance
    rows_4h, dropped_4h = _drop_future_rows(
        payload_4h.rows, interval=INTERVAL_4H, asof_us=asof_us)
    rows_1d, dropped_1d = _drop_future_rows(
        payload_1d.rows, interval=INTERVAL_1D, asof_us=asof_us)
    if not rows_4h or not rows_1d:
        record = _noneligible_record(
            event,
            code=NONELIGIBLE_HTF_INVALID_SOURCE,
            detail=(
                f"empty closed payload at asof_us={asof_us}: "
                f"4h={len(rows_4h)} 1d={len(rows_1d)} rows"
            ),
        )
        return ClassificationOutcome(record=record, snapshot=None, attempts=1)

    source_record: SourceEventRecord = event.to_source_event_record()
    try:
        source_4h = build_source_identity(
            payload_4h.source_kind,
            payload_4h.source_label,
            list(rows_4h),
            fetched_at_ms=payload_4h.fetched_at_ms,
        )
        source_1d = build_source_identity(
            payload_1d.source_kind,
            payload_1d.source_label,
            list(rows_1d),
            fetched_at_ms=payload_1d.fetched_at_ms,
        )
    except ValueError as exc:
        record = _noneligible_record(
            event, code=NONELIGIBLE_HTF_INVALID_SOURCE, detail=str(exc))
        return ClassificationOutcome(record=record, snapshot=None, attempts=1)

    # Provenance snapshot FIRST — its immutability contract is verified
    # round-trip (tamper detection) before anything derived from it is used.
    try:
        snapshot = build_snapshot(
            record=source_record,
            rows_4h=[dict(row) for row in rows_4h],
            rows_1d=[dict(row) for row in rows_1d],
            interval_4h=INTERVAL_4H,
            interval_1d=INTERVAL_1D,
            source_kind=payload_4h.source_kind,
            source_label=payload_4h.source_label,
            source_label_4h=payload_4h.source_label,
            source_label_1d=payload_1d.source_label,
            fixture_label=payload_4h.fixture_label,
            fetched_at_ms=payload_4h.fetched_at_ms,
        )
        # fail-closed tamper self-check (round-trip through the strict loader)
        snapshot_from_dict(snapshot.to_dict())
    except Exception as exc:  # noqa: BLE001 — deterministic fail-closed code
        record = _noneligible_record(
            event, code=NONELIGIBLE_SNAPSHOT_TAMPER, detail=str(exc))
        return ClassificationOutcome(record=record, snapshot=None, attempts=1)

    try:
        h1d: H1DClassification = classify_event(
            source_record,
            [dict(row) for row in rows_4h],
            [dict(row) for row in rows_1d],
            source_4h=source_4h,
            source_1d=source_1d,
            canonical_lookback=int(canonical_lookback),
        )
    except Exception as exc:  # noqa: BLE001 — deterministic fail-closed code
        record = _noneligible_record(
            event, code=NONELIGIBLE_CLASSIFIER_ERROR, detail=str(exc))
        return ClassificationOutcome(
            record=record, snapshot=snapshot, attempts=1)

    result = h1d.result
    codes: list[str] = []
    for label, coverage in (("4h", h1d.coverage_4h), ("1d", h1d.coverage_1d)):
        if coverage.status == COVERAGE_COMPLETE:
            continue
        code = (
            NONELIGIBLE_HTF_INVALID_SOURCE
            if coverage.status == "INVALID_SOURCE"
            else NONELIGIBLE_HTF_INCOMPLETE_COVERAGE
        )
        if code not in codes:
            codes.append(code)
    if result is None or not h1d.eligible:
        if not codes:
            if (
                result is not None
                and (
                    result.htf_4h.regime == REGIME_INVALID_SOURCE
                    or result.htf_1d.regime == REGIME_INVALID_SOURCE
                )
            ):
                codes.append(NONELIGIBLE_HTF_INVALID_SOURCE)
            else:
                codes.append(NONELIGIBLE_HTF_INCOMPLETE_COVERAGE)

    # "never infer bearish/bullish/neutral from invalid data": regime / BOS
    # outputs exist ONLY for eligible classifications; non-eligible outcomes
    # carry the diagnostic regimes in ``h1d_diagnostics`` exclusively.
    if h1d.eligible and result is not None:
        regime_4h: str | None = result.htf_4h.regime
        bos_4h: str | None = result.htf_4h.bos
        regime_1d: str | None = result.htf_1d.regime
        bos_1d: str | None = result.htf_1d.bos
        combined_regime: str | None = result.combined_regime
    else:
        regime_4h = bos_4h = regime_1d = bos_1d = combined_regime = None

    record = ClassificationRecord(
        version=H1E_CLASSIFICATION_VERSION,
        source_event_key=event.source_event_key,
        source_identity=event.source_identity,
        lineage_hash=event.lineage_hash,
        signal_time_us=event.signal_time_us,
        asof_us=asof_us,
        status=STATUS_CLASSIFIED,
        eligible=bool(h1d.eligible),
        eligibility_reason=(
            REASON_ELIGIBLE if h1d.eligible else h1d.eligibility_reason
        ),
        non_eligible_codes=tuple(codes),
        regime_4h=regime_4h,
        bos_4h=bos_4h,
        regime_1d=regime_1d,
        bos_1d=bos_1d,
        combined_regime=combined_regime,
        coverage_4h_status=h1d.coverage_4h.status,
        coverage_1d_status=h1d.coverage_1d.status,
        coverage_4h_present=h1d.coverage_4h.present_buckets,
        coverage_1d_present=h1d.coverage_1d.present_buckets,
        coverage_4h_missing_open_ms=tuple(h1d.coverage_4h.missing_open_ms),
        coverage_1d_missing_open_ms=tuple(h1d.coverage_1d.missing_open_ms),
        snapshot_sha256=snapshot.snapshot_sha256,
        h1d_diagnostics={
            "h1d_eligibility_reason": h1d.eligibility_reason,
            "diagnostic_regime_4h": None if result is None else result.htf_4h.regime,
            "diagnostic_regime_1d": None if result is None else result.htf_1d.regime,
            "diagnostic_combined_regime": (
                None if result is None else result.combined_regime),
            "dropped_future_rows_4h": dropped_4h,
            "dropped_future_rows_1d": dropped_1d,
            "coverage_4h_reason": h1d.coverage_4h.reason,
            "coverage_1d_reason": h1d.coverage_1d.reason,
        },
    )
    return ClassificationOutcome(
        record=record, snapshot=snapshot, attempts=prior_deferrals + 1)


def replay_canonical_bytes(snapshot: Snapshot) -> bytes:
    """Byte-identical offline replay output (ZERO HTTP, ZERO clock)."""
    from app.research.me_entry_quality_h1d_v1.provenance import (
        replay_classification,
    )

    return canonical_output_bytes(replay_classification(snapshot))


__all__ = [
    "ClassificationOutcome",
    "ClassificationRecord",
    "DEFAULT_RETRY_POLICY",
    "H1E_CLASSIFICATION_VERSION",
    "HtfBreakerDeniedError",
    "HtfLimiterDeniedError",
    "HtfPayload",
    "HtfRateLimitError",
    "HtfTimeoutError",
    "HtfTransport",
    "HtfTransportApiError",
    "INTERVAL_1D",
    "INTERVAL_4H",
    "NONELIGIBLE_CLASSIFIER_ERROR",
    "NONELIGIBLE_HTF_API_ERROR",
    "NONELIGIBLE_HTF_BREAKER_DENIED",
    "NONELIGIBLE_HTF_INCOMPLETE_COVERAGE",
    "NONELIGIBLE_HTF_INVALID_SOURCE",
    "NONELIGIBLE_HTF_LIMITER_DENIED",
    "NONELIGIBLE_HTF_RATE_LIMITED",
    "NONELIGIBLE_HTF_RETRY_BUDGET_EXHAUSTED",
    "NONELIGIBLE_HTF_TIMEOUT",
    "NONELIGIBLE_RETRY_BUDGET_EXHAUSTED",
    "NONELIGIBLE_SNAPSHOT_TAMPER",
    "REASON_ELIGIBLE",
    "RetryPolicy",
    "STATUS_CLASSIFIED",
    "STATUS_DEFERRED",
    "classify_h1e_event",
    "replay_canonical_bytes",
    "terminal_noneligible_record",
]

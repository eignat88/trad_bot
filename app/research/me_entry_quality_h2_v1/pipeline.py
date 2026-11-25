"""Issue #298 / H2 — pure delayed-entry pipeline (research only).

Orchestrates, for ONE #305 ``source_event`` row and ONE evaluation ASOF,
all pure stages:

1. event identity — REUSED unchanged from H1-C
   (:func:`app.research.me_entry_quality_h1c_v1.event_adapter.adapt_source_events`):
   ``source_event_key`` / ``source_identity`` / ``signal_time`` /
   ``activation_freeze_ts`` / ``lineage_hash`` are carried VERBATIM, the
   original ASOF is ``signal_time`` (NEVER ``captured_at``) and a
   redelivered ``source_event_key`` is rejected explicitly;
2. original feature snapshot (:class:`SourceFeatureSnapshot`) — verbatim;
3. :func:`provide_5m_candles` — caller-supplied 5m rows, validated and
   ASOF-truncated (only candles CLOSED at ``evaluation_asof_ms`` are used);
4. recomputed Wilder RSI(14) on the closed-candle series (parity with the
   original detector — see :mod:`rsi_confirmation`);
5. the per-variant delayed-entry state machine
   (``PENDING_CONFIRMATION`` / ``CONFIRMED_DELAYED`` / ``EXPIRED`` /
   ``INVALID_SOURCE`` / ``INCOMPLETE_COVERAGE`` / ``NOT_READY``).

Causal guarantees (enforced by tests):

* post-signal observations are 5m candles closing STRICTLY AFTER the exact
  ``signal_time`` instant (microsecond comparison — even at the same
  millisecond);
* only fully closed candles participate (``open + 300_000 <=
  evaluation_asof_ms``, inclusive at the close boundary);
* appending future candles NEVER changes a result computed at an earlier
  ASOF — the whole chain is a pure function of
  ``(row, rows_5m, source_identity, policies, evaluation_asof_ms)``;
* the recomputed closed-candle RSI never replaces the original
  ``rsi_14`` snapshot;
* a confirmed delayed entry NEVER carries the original price as an
  execution price and NEVER assumes a fill at the confirmation close.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.models import Candle
from app.research.me_entry_quality_h1c_v1.event_adapter import (
    EventRejection,
    SourceEventRecord,
    adapt_source_events,
    parse_utc_us,
)
from app.research.me_entry_quality_h2_v1.candle_provider import (
    CANDLE_5M_MS,
    CANONICAL_LOOKBACK_5M,
    INCOMPLETE_COVERAGE_REASONS,
    TIMEFRAME_5M,
    Candle5mSet,
    CandleSourceIdentity,
    canonical_window_coverage,
    provide_5m_candles,
)
from app.research.me_entry_quality_h2_v1.result import (
    COVERAGE_COMPLETE,
    COVERAGE_NONE,
    COVERAGE_PARTIAL,
    ENTRY_PRICE_UNDETERMINED,
    ENTRY_REFERENCE_LABEL,
    EXPIRY_REASON_WINDOW_EXHAUSTED,
    H2FeatureError,
    H2Policy,
    H2VariantResult,
    ORIGINAL_PRICE_BASIS,
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
    STATE_PENDING_CONFIRMATION,
    VARIANT_PEAK_REVERSAL,
    VARIANT_TURN_1,
    VARIANT_TURN_2,
    SourceFeatureSnapshot,
)
from app.research.me_entry_quality_h2_v1.rsi_confirmation import (
    peak_reversal_confirmation,
    rsi_wilder_series,
    turn_1_confirmation,
    turn_2_confirmation,
)

H2_PIPELINE_VERSION = "H2_PIPELINE_1.1.0"

REASON_OK = "OK"
REASON_EVENT_REJECTED = "SOURCE_EVENT_REJECTED"
REASON_INVALID_SOURCE = "INVALID_SOURCE"
REASON_INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
REASON_NOT_READY = "NOT_READY"
REASON_AWAITING_OBSERVATION = "AWAITING_POST_SIGNAL_OBSERVATION"

REASON_INVALID_SOURCE_REFERENCE_PRICE = "INVALID_SOURCE_REFERENCE_PRICE"
REASON_INSUFFICIENT_RSI_WARMUP = "INSUFFICIENT_RSI_WARMUP"
REASON_MISSING_BASE_CANDLE = "MISSING_BASE_CANDLE"
#: canonical cohort window (event-anchored) not fully covered by the source
REASON_CANONICAL_WINDOW_INCOMPLETE = "CANONICAL_WINDOW_INCOMPLETE"
#: #305 prospective-cohort timing contract, enforced at the H2 boundary for
#: caller-supplied rows (the database CHECK does not protect them):
#: ``signal_time > activation_freeze_ts`` STRICTLY (never equal, never
#: before — no retroactive admission) and ``captured_at`` must not precede
#: the freeze.  The stored #305 ``freeze_ts`` itself is never touched.
REASON_PRE_FREEZE_SIGNAL_TIME = "PRE_FREEZE_SIGNAL_TIME"
REASON_PRE_FREEZE_CAPTURED_AT = "PRE_FREEZE_CAPTURED_AT"
REASON_INVALID_SOURCE_TIMING = "INVALID_SOURCE_TIMING"


@dataclass(frozen=True)
class H2EventOutcome:
    """Deterministic pipeline outcome for ONE adapted source event."""

    record: SourceEventRecord | None
    results: tuple[H2VariantResult, ...]
    ok: bool
    reason: str
    detail: str
    candle_set: Candle5mSet | None = None
    adapter_rejections: tuple[EventRejection, ...] = ()
    pipeline_version: str = H2_PIPELINE_VERSION
    #: full candle-source provenance block (H1-C contract) for the OHLCV
    #: data actually used — identity, payload hash, row count, coverage,
    #: canonical window, provider reason.  ``None`` only when the event was
    #: rejected before any candle data was evaluated.
    source: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_version": self.pipeline_version,
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "record": None if self.record is None else self.record.to_dict(),
            "source": self.source,
            "results": [r.to_dict() for r in self.results],
            "adapter_rejections": [r.to_dict() for r in self.adapter_rejections],
        }


def _extract_features(row: Mapping[str, Any]) -> Mapping[str, Any]:
    features = row.get("features")
    if isinstance(features, Mapping):
        return features
    lineage = row.get("lineage")
    if isinstance(lineage, Mapping):
        nested = lineage.get("source_features")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _extract_original_price(
    row: Mapping[str, Any],
) -> tuple[float, str] | None:
    """``(float64 view, exact decimal text)`` of the optional source price.

    ``source_reference_price`` is PostgreSQL ``NUMERIC`` and arrives as
    :class:`decimal.Decimal` from the database driver — accepted alongside
    ``int`` and ``float``.  ``bool``, strings and other types are rejected;
    NaN / Infinity / -Infinity are rejected; the price must be strictly
    positive.  An ABSENT optional price stays absent (``None``) and never
    becomes a fabricated price.

    Precision policy (:data:`ORIGINAL_PRICE_PRECISION_POLICY`): the exact
    source value is preserved VERBATIM as decimal text (``Decimal``/``int``
    via exact decimal formatting, ``float`` via its shortest round-trip
    repr) and is never rounded; the ``float`` view is only for research
    arithmetic and must be a finite float64 (a value outside that range is
    rejected, never silently coerced).
    """
    value = row.get("source_reference_price")
    if value is None and isinstance(row.get("lineage"), Mapping):
        value = row["lineage"].get("source_reference_price")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE)
        if not value > 0:
            raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE)
        exact = format(value, "f")
    elif isinstance(value, int):
        if not value > 0:
            raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE)
        exact = str(value)
    else:  # float
        if not math.isfinite(value) or not value > 0:
            raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE)
        exact = repr(value)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE) from exc
    if not math.isfinite(number):
        raise H2FeatureError(REASON_INVALID_SOURCE_REFERENCE_PRICE)
    return number, exact


def _source_provenance(
    candle_set: Candle5mSet,
    canonical: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Full candle-source provenance block (H1-C contract, serialized).

    Identity / hash / row count / coverage reuse the existing H1-C
    ``HtfSourceIdentity`` + ``CoverageReport`` formats unchanged.  No OHLCV
    values are embedded — the source identity (payload hash, row count,
    label, fetch time), the actual closed-candle range and the coverage /
    canonical-window diagnostics fully identify the data used.
    """
    identity = candle_set.source_identity
    candles = candle_set.candles
    return {
        "source_kind": identity.source_kind,
        "source_label": identity.source_label,
        "payload_hash": identity.payload_hash,
        "row_count": identity.row_count,
        "fetched_at_ms": identity.fetched_at_ms,
        "provider_version": candle_set.provider_version,
        "timeframe": TIMEFRAME_5M,
        "evaluation_asof_ms": candle_set.evaluation_asof_ms,
        "closed_range": {
            "first_closed_open_ms": (
                int(candles[0].timestamp) if candles else None
            ),
            "last_closed_open_ms": (
                int(candles[-1].timestamp) if candles else None
            ),
            "closed_candle_count": len(candles),
            "dropped_unclosed_count": len(candle_set.dropped_unclosed),
            "latest_closed_open_ms": candle_set.latest_closed_open_ms,
        },
        "coverage": candle_set.coverage.to_dict(),
        "canonical_window": (
            None if canonical is None else dict(canonical)
        ),
        "source_ok": candle_set.ok,
        "source_reason": candle_set.reason,
        "source_detail": candle_set.detail,
    }


def _shell_result(
    *,
    policy: H2Policy,
    record: SourceEventRecord | None,
    status: str,
    symbol: str,
    original_price: float | None,
    original_price_exact: str | None,
    snapshot: SourceFeatureSnapshot | None,
    evaluation_asof_ms: int,
    reason: str,
    detail: str,
    coverage_status: str,
    coverage_detail: str = "",
    observed_window_candles: int = 0,
    expiry_reason: str | None = None,
    candle_source: Mapping[str, Any] | None = None,
) -> H2VariantResult:
    """One result with only source lineage + terminal status filled in."""
    if record is None:
        key = identity = lineage_hash = ""
        signal_us = signal_ms = freeze_ms = 0
    else:
        key = record.source_event_key
        identity = record.source_identity
        lineage_hash = record.lineage_hash
        signal_us = record.signal_time_us
        signal_ms = record.signal_time_ms
        freeze_ms = record.activation_freeze_ts_ms
    return H2VariantResult(
        variant_id=policy.variant_id,
        status=status,
        source_event_key=key,
        source_identity=identity,
        symbol=symbol,
        signal_time_us=signal_us,
        signal_time_ms=signal_ms,
        activation_freeze_ts_ms=freeze_ms,
        lineage_hash=lineage_hash,
        original_price=original_price,
        original_price_exact=original_price_exact,
        original_price_basis=(
            ORIGINAL_PRICE_BASIS if original_price is not None else ""
        ),
        source_rsi_14_snapshot=None if snapshot is None else snapshot.rsi_14,
        source_rsi_delta_3_snapshot=None if snapshot is None else snapshot.rsi_delta_3,
        source_entry_confirmation_rsi_rising=(
            None if snapshot is None else snapshot.entry_confirmation_rsi_rising
        ),
        evaluation_asof_ms=int(evaluation_asof_ms),
        policy=policy,
        policy_fingerprint=policy.fingerprint(),
        observed_window_candles=observed_window_candles,
        expiry_reason=expiry_reason,
        coverage_status=coverage_status,
        coverage_detail=coverage_detail,
        reason=reason,
        detail=detail,
        candle_source=None if candle_source is None else dict(candle_source),
    )


def _terminal_results(
    policies: Sequence[H2Policy],
    *,
    record: SourceEventRecord | None,
    status: str,
    symbol: str,
    original_price: float | None,
    original_price_exact: str | None = None,
    snapshot: SourceFeatureSnapshot | None,
    evaluation_asof_ms: int,
    reason: str,
    detail: str,
    coverage_status: str,
    coverage_detail: str = "",
    candle_source: Mapping[str, Any] | None = None,
) -> tuple[H2VariantResult, ...]:
    return tuple(
        _shell_result(
            policy=policy,
            record=record,
            status=status,
            symbol=symbol,
            original_price=original_price,
            original_price_exact=original_price_exact,
            snapshot=snapshot,
            evaluation_asof_ms=evaluation_asof_ms,
            reason=reason,
            detail=detail,
            coverage_status=coverage_status,
            coverage_detail=coverage_detail,
            candle_source=candle_source,
        )
        for policy in policies
    )


def _observation_indices(
    candles: Sequence[Candle], signal_time_us: int
) -> tuple[int, int]:
    """``(base_index, first_obs_index)`` for the original signal instant.

    ``O_1`` is the first 5m candle closing STRICTLY AFTER ``signal_time``
    (microsecond comparison); ``O_0`` is the immediately preceding closed
    candle (level reference).  Returns ``(-1, -1)`` when no closed candle
    closes after the signal yet.
    """
    for index, candle in enumerate(candles):
        close_us = (int(candle.timestamp) + CANDLE_5M_MS) * 1000
        if close_us > signal_time_us:
            return index - 1, index
    return -1, -1


def run_h2_event(
    row: Mapping[str, Any],
    *,
    rows_5m: Sequence[Any],
    source_identity: CandleSourceIdentity,
    policies: Sequence[H2Policy],
    evaluation_asof_ms: Any,
) -> H2EventOutcome:
    """Evaluate the H2 delayed-entry variants for ONE source event at ASOF.

    Pure: no I/O of any kind.  ``rows_5m`` are caller-supplied raw OHLCV
    rows (validated + ASOF-truncated internally); ``source_identity``
    proves their provenance.

    FAIL-CLOSED: a rejected event row, an invalid feature snapshot, an
    untrusted payload or an honestly incomplete range all yield explicit
    terminal states — never a silent drop and never a market mode.
    """
    if not isinstance(row, Mapping):
        raise ValueError("row must be a mapping")
    if not policies or any(not isinstance(p, H2Policy) for p in policies):
        raise ValueError("policies must be a non-empty sequence of H2Policy")
    if isinstance(evaluation_asof_ms, bool) or not isinstance(
        evaluation_asof_ms, int
    ):
        raise ValueError("evaluation_asof_ms must be a real integer")
    asof_ms = int(evaluation_asof_ms)
    if asof_ms < 0:
        raise ValueError("evaluation_asof_ms must be non-negative")

    # 1. identity — the H1-C adapter, reused unchanged.
    batch = adapt_source_events([row])
    record: SourceEventRecord | None = batch.records[0] if batch.records else None
    if record is None:
        rejection = batch.rejections[0] if batch.rejections else None
        reason = rejection.reason if rejection is not None else REASON_EVENT_REJECTED
        detail = rejection.detail if rejection is not None else "event rejected"
        return H2EventOutcome(
            record=None,
            results=_terminal_results(
                policies,
                record=None,
                status=STATE_INVALID_SOURCE,
                symbol=str(row.get("symbol") or ""),
                original_price=None,
                snapshot=None,
                evaluation_asof_ms=asof_ms,
                reason=reason,
                detail=detail,
                coverage_status=COVERAGE_NONE,
            ),
            ok=False,
            reason=REASON_EVENT_REJECTED,
            detail=f"{reason}: {detail}",
            adapter_rejections=batch.rejections,
        )

    if asof_ms < record.signal_time_ms:
        raise ValueError(
            "evaluation_asof_ms must not precede the original signal_time"
        )

    # 1b. prospective-cohort timing guard (review 6055546804, finding 4).
    # #305's database CHECK protects stored rows, but H2 accepts
    # caller-supplied mappings — so the temporal cohort contract is enforced
    # here at the H2 boundary, at FULL microsecond precision.  Strict
    # ``signal_time > activation_freeze_ts`` (never equal — no retroactive
    # admission of pre-freeze events); ``captured_at`` must not precede the
    # freeze.  The stored #305 ``freeze_ts`` is only compared, never changed.
    try:
        freeze_us = parse_utc_us(
            row.get("activation_freeze_ts"), field_name="activation_freeze_ts"
        )
        captured_us = parse_utc_us(row.get("captured_at"), field_name="captured_at")
    except ValueError as exc:
        return H2EventOutcome(
            record=record,
            results=_terminal_results(
                policies,
                record=record,
                status=STATE_INVALID_SOURCE,
                symbol=record.symbol,
                original_price=None,
                snapshot=None,
                evaluation_asof_ms=asof_ms,
                reason=REASON_INVALID_SOURCE_TIMING,
                detail=str(exc),
                coverage_status=COVERAGE_NONE,
            ),
            ok=False,
            reason=REASON_INVALID_SOURCE,
            detail=f"{REASON_INVALID_SOURCE_TIMING}: {exc}",
            adapter_rejections=batch.rejections,
        )
    if record.signal_time_us <= freeze_us:
        return H2EventOutcome(
            record=record,
            results=_terminal_results(
                policies,
                record=record,
                status=STATE_INVALID_SOURCE,
                symbol=record.symbol,
                original_price=None,
                snapshot=None,
                evaluation_asof_ms=asof_ms,
                reason=REASON_PRE_FREEZE_SIGNAL_TIME,
                detail=(
                    "#305 contract signal_time > activation_freeze_ts "
                    f"violated: signal_time_us={record.signal_time_us} <= "
                    f"activation_freeze_ts_us={freeze_us}"
                ),
                coverage_status=COVERAGE_NONE,
            ),
            ok=False,
            reason=REASON_INVALID_SOURCE,
            detail=REASON_PRE_FREEZE_SIGNAL_TIME,
            adapter_rejections=batch.rejections,
        )
    if captured_us < freeze_us:
        return H2EventOutcome(
            record=record,
            results=_terminal_results(
                policies,
                record=record,
                status=STATE_INVALID_SOURCE,
                symbol=record.symbol,
                original_price=None,
                snapshot=None,
                evaluation_asof_ms=asof_ms,
                reason=REASON_PRE_FREEZE_CAPTURED_AT,
                detail=(
                    f"captured_at_us={captured_us} precedes "
                    f"activation_freeze_ts_us={freeze_us}"
                ),
                coverage_status=COVERAGE_NONE,
            ),
            ok=False,
            reason=REASON_INVALID_SOURCE,
            detail=REASON_PRE_FREEZE_CAPTURED_AT,
            adapter_rejections=batch.rejections,
        )

    # 2. original feature snapshot — verbatim (#305 semantics).
    try:
        snapshot = SourceFeatureSnapshot.from_mapping(_extract_features(row))
        price = _extract_original_price(row)
    except H2FeatureError as exc:
        return H2EventOutcome(
            record=record,
            results=_terminal_results(
                policies,
                record=record,
                status=STATE_INVALID_SOURCE,
                symbol=record.symbol,
                original_price=None,
                snapshot=None,
                evaluation_asof_ms=asof_ms,
                reason=exc.reason_code,
                detail=str(exc),
                coverage_status=COVERAGE_NONE,
            ),
            ok=False,
            reason=REASON_INVALID_SOURCE,
            detail=str(exc),
            adapter_rejections=batch.rejections,
        )
    original_price = None if price is None else price[0]
    original_price_exact = None if price is None else price[1]

    # 3. caller-supplied 5m candles — validated + ASOF-truncated.
    candle_set = provide_5m_candles(
        rows_5m,
        evaluation_asof_ms=asof_ms,
        source_identity=source_identity,
    )
    if not candle_set.ok:
        status = (
            STATE_INCOMPLETE_COVERAGE
            if candle_set.reason in INCOMPLETE_COVERAGE_REASONS
            else STATE_INVALID_SOURCE
        )
        coverage = (
            COVERAGE_NONE
            if candle_set.reason == "EMPTY_SERIES"
            else COVERAGE_PARTIAL
        )
        provenance = _source_provenance(candle_set, None)
        return H2EventOutcome(
            record=record,
            results=_terminal_results(
                policies,
                record=record,
                status=status,
                symbol=record.symbol,
                original_price=original_price,
                original_price_exact=original_price_exact,
                snapshot=snapshot,
                evaluation_asof_ms=asof_ms,
                reason=candle_set.reason,
                detail=candle_set.detail,
                coverage_status=coverage,
                coverage_detail=candle_set.reason,
                candle_source=provenance,
            ),
            ok=False,
            reason=(
                REASON_INCOMPLETE_COVERAGE
                if status == STATE_INCOMPLETE_COVERAGE
                else REASON_INVALID_SOURCE
            ),
            detail=f"{candle_set.reason}: {candle_set.detail}",
            candle_set=candle_set,
            adapter_rejections=batch.rejections,
            source=provenance,
        )

    # 4-5. per-variant delayed-entry state machine.
    candles = candle_set.candles
    base_index, first_obs = _observation_indices(candles, record.signal_time_us)
    canonical = (
        canonical_window_coverage(candles, base_index=base_index).to_dict()
        if base_index >= 0
        else None
    )
    provenance = _source_provenance(candle_set, canonical)
    results: list[H2VariantResult] = []
    for policy in policies:
        results.append(
            _evaluate_variant(
                policy,
                record=record,
                snapshot=snapshot,
                original_price=original_price,
                original_price_exact=original_price_exact,
                candles=candles,
                base_index=base_index,
                first_obs=first_obs,
                asof_ms=asof_ms,
                canonical_report=canonical,
                candle_source=provenance,
            )
        )

    ok = all(
        r.status not in (STATE_INVALID_SOURCE, STATE_INCOMPLETE_COVERAGE)
        for r in results
    )
    if ok:
        reason, detail = REASON_OK, ""
    else:
        first_bad = next(
            r
            for r in results
            if r.status in (STATE_INVALID_SOURCE, STATE_INCOMPLETE_COVERAGE)
        )
        reason = (
            REASON_INCOMPLETE_COVERAGE
            if first_bad.status == STATE_INCOMPLETE_COVERAGE
            else REASON_INVALID_SOURCE
        )
        detail = f"{first_bad.reason}: {first_bad.detail}"
    return H2EventOutcome(
        record=record,
        results=tuple(results),
        ok=ok,
        reason=reason,
        detail=detail,
        candle_set=candle_set,
        adapter_rejections=batch.rejections,
        source=provenance,
    )


def _evaluate_variant(
    policy: H2Policy,
    *,
    record: SourceEventRecord,
    snapshot: SourceFeatureSnapshot,
    original_price: float | None,
    original_price_exact: str | None,
    candles: Sequence[Candle],
    base_index: int,
    first_obs: int,
    asof_ms: int,
    canonical_report: Mapping[str, Any] | None,
    candle_source: Mapping[str, Any] | None,
) -> H2VariantResult:
    def _terminal(
        status: str, reason: str, detail: str, **kwargs: Any
    ) -> H2VariantResult:
        return _shell_result(
            policy=policy,
            record=record,
            status=status,
            symbol=record.symbol,
            original_price=original_price,
            original_price_exact=original_price_exact,
            snapshot=snapshot,
            evaluation_asof_ms=asof_ms,
            reason=reason,
            detail=detail,
            coverage_status=kwargs.pop("coverage_status", COVERAGE_COMPLETE),
            candle_source=candle_source,
            **kwargs,
        )

    # An undefined peak rule is surfaced as NOT_READY — never approximated.
    if policy.variant_id == VARIANT_PEAK_REVERSAL and policy.peak_rule is None:
        return _terminal(
            STATE_NOT_READY,
            REASON_NOT_READY,
            "peak rule not prespecified for H2_RSI_PEAK_REVERSAL",
            coverage_status=COVERAGE_NONE,
        )

    if first_obs < 0:
        # No closed candle closes after the signal yet — nothing observed.
        return _terminal(
            STATE_PENDING_CONFIRMATION,
            REASON_AWAITING_OBSERVATION,
            "no post-signal candle is closed at this ASOF yet",
            observed_window_candles=0,
        )

    if base_index < 0:
        return _terminal(
            STATE_INCOMPLETE_COVERAGE,
            REASON_MISSING_BASE_CANDLE,
            "no closed candle at or before the original signal instant",
            coverage_status=COVERAGE_PARTIAL,
            coverage_detail=REASON_MISSING_BASE_CANDLE,
        )

    # mathematical RSI minimum (Wilder seed) — SEPARATE from the canonical
    # cohort window below (review 6055546804, finding 3)
    if base_index < policy.rsi_period:
        return _terminal(
            STATE_INCOMPLETE_COVERAGE,
            REASON_INSUFFICIENT_RSI_WARMUP,
            (
                f"RSI({policy.rsi_period}) seed needs {policy.rsi_period + 1} "
                f"closes before the first observation; only {base_index + 1} "
                "available"
            ),
            coverage_status=COVERAGE_PARTIAL,
            coverage_detail=REASON_INSUFFICIENT_RSI_WARMUP,
        )

    # canonical cohort eligibility: the full event-anchored history window
    # (CANONICAL_LOOKBACK_5M buckets ending at O_0) must be covered.  A
    # PARTIAL window can never yield an eligible result and is never
    # labeled COMPLETE.
    canonical_status = (
        None if canonical_report is None else canonical_report.get("status")
    )
    if canonical_status != COVERAGE_COMPLETE:
        present = 0 if canonical_report is None else canonical_report.get(
            "present_buckets", 0
        )
        expected = CANONICAL_LOOKBACK_5M
        missing_count = (
            expected - present
            if canonical_report is not None
            else expected
        )
        return _terminal(
            STATE_INCOMPLETE_COVERAGE,
            REASON_CANONICAL_WINDOW_INCOMPLETE,
            (
                f"canonical RSI history window ({expected} closed 5m buckets "
                f"ending at O_0) is not fully covered: {present}/{expected} "
                f"present, {missing_count} missing"
            ),
            coverage_status=(
                COVERAGE_NONE if not present else COVERAGE_PARTIAL
            ),
            coverage_detail=REASON_CANONICAL_WINDOW_INCOMPLETE,
        )

    # FIXED historical window policy (H2_RSI_WINDOW_POLICY): the Wilder
    # series is seeded at the canonical window start and extended causally
    # over the post-signal observations — earlier history supplied by the
    # caller is excluded, so the RSI never depends on the loaded prefix
    # length.
    window_start_index = base_index - (CANONICAL_LOOKBACK_5M - 1)
    closes = [float(c.close) for c in candles[window_start_index:]]
    series = rsi_wilder_series(closes, period=policy.rsi_period)
    base_pos = base_index - window_start_index

    if base_pos < policy.rsi_period:
        return _terminal(
            STATE_INCOMPLETE_COVERAGE,
            REASON_INSUFFICIENT_RSI_WARMUP,
            (
                f"RSI({policy.rsi_period}) is undefined at O_0 inside the "
                f"canonical window ({base_pos + 1} closes)"
            ),
            coverage_status=COVERAGE_PARTIAL,
            coverage_detail=REASON_INSUFFICIENT_RSI_WARMUP,
        )

    window = int(policy.observation_window)
    obs_indices = [
        first_obs + offset
        for offset in range(min(window, len(candles) - first_obs))
    ]
    obs_values: list[float] = [series[base_pos]]
    for index in obs_indices:
        value = series[index - window_start_index]
        assert value is not None  # guaranteed by the warmup check above
        obs_values.append(value)
    observed = len(obs_indices)

    if policy.variant_id == VARIANT_TURN_1:
        k = turn_1_confirmation(obs_values)
    elif policy.variant_id == VARIANT_TURN_2:
        k = turn_2_confirmation(obs_values)
    else:
        k = peak_reversal_confirmation(obs_values, peak_rule=policy.peak_rule)

    if k is not None:
        confirm_candle = candles[obs_indices[k - 1]]
        confirm_open_ms = int(confirm_candle.timestamp)
        confirm_time_ms = confirm_open_ms + CANDLE_5M_MS
        confirm_time_us = confirm_time_ms * 1000
        entry_delay_us = confirm_time_us - record.signal_time_us
        return H2VariantResult(
            variant_id=policy.variant_id,
            status=STATE_CONFIRMED_DELAYED,
            source_event_key=record.source_event_key,
            source_identity=record.source_identity,
            symbol=record.symbol,
            signal_time_us=record.signal_time_us,
            signal_time_ms=record.signal_time_ms,
            activation_freeze_ts_ms=record.activation_freeze_ts_ms,
            lineage_hash=record.lineage_hash,
            original_price=original_price,
            original_price_exact=original_price_exact,
            original_price_basis=(
                ORIGINAL_PRICE_BASIS if original_price is not None else ""
            ),
            source_rsi_14_snapshot=snapshot.rsi_14,
            source_rsi_delta_3_snapshot=snapshot.rsi_delta_3,
            source_entry_confirmation_rsi_rising=(
                snapshot.entry_confirmation_rsi_rising
            ),
            evaluation_asof_ms=asof_ms,
            policy=policy,
            policy_fingerprint=policy.fingerprint(),
            confirmation_candle_open_ms=confirm_open_ms,
            confirmation_time_ms=confirm_time_ms,
            confirmation_time_us=confirm_time_us,
            h2_rsi_at_confirmation=obs_values[k],
            entry_candidate_time_ms=confirm_time_ms,
            entry_candidate_time_us=confirm_time_us,
            entry_candidate_price=None,
            entry_price_basis=ENTRY_PRICE_UNDETERMINED,
            entry_reference_close_price=float(confirm_candle.close),
            entry_reference_label=ENTRY_REFERENCE_LABEL,
            entry_delay_ms=entry_delay_us // 1000,
            entry_delay_us=entry_delay_us,
            observed_window_candles=observed,
            coverage_status=COVERAGE_COMPLETE,
            reason=REASON_OK,
            detail="",
            candle_source=(
                None if candle_source is None else dict(candle_source)
            ),
        )

    if observed >= window:
        return _terminal(
            STATE_EXPIRED,
            REASON_AWAITING_OBSERVATION,
            "observation window fully closed without confirmation",
            observed_window_candles=observed,
            expiry_reason=EXPIRY_REASON_WINDOW_EXHAUSTED,
        )

    return _terminal(
        STATE_PENDING_CONFIRMATION,
        REASON_AWAITING_OBSERVATION,
        (
            f"{observed} of {window} post-signal candles closed at this ASOF; "
            "confirmation window still open"
        ),
        observed_window_candles=observed,
    )

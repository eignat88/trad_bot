"""Issue #298 / H2 — result model, policy fingerprint and source snapshot.

Research-only, pure: no I/O, no network, no database, no clock reads.

This module defines the immutable H2 contracts:

* :class:`H2Policy` — the per-variant parameter set with a deterministic
  parameter fingerprint.  EVERY parameter set used in research is a
  PRELIMINARY CANDIDATE (``PRELIMINARY_CANDIDATE_NOT_FROZEN``): the
  observation windows 3 and 6 are candidates, never approved defaults, and
  nothing here may be selected post-hoc against outcomes;
* :class:`SourceFeatureSnapshot` — the ORIGINAL detector features copied
  VERBATIM (``rsi_14``, ``rsi_delta_3``, ``entry_confirmation_rsi_rising``).
  The original RSI is a volatile first-capture snapshot and is NEVER
  recalculated, NEVER overwritten by H2's recomputed closed-candle RSI;
* :class:`H2VariantResult` — the per-(event, variant) delayed-entry state
  machine outcome with full source lineage.

Delayed-entry states::

    PENDING_CONFIRMATION -> CONFIRMED_DELAYED
                        \\-> EXPIRED
    INVALID_SOURCE / INCOMPLETE_COVERAGE / NOT_READY are terminal data /
    readiness outcomes (fail closed — never a market mode).

Execution-price honesty: a confirmed delayed entry records the EARLIEST
causally executable instant (the close of the confirming 5m candle) but
NEVER a fictional execution price.  The confirmation-candle close is
carried only as an explicitly labeled, non-executable reference.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.research.me_entry_quality_h2_v1.rsi_confirmation import (
    PEAK_RULES,
    RSI_PERIOD,
)

H2_PROTOCOL_VERSION = "H2_1.1.0"

#: Version of the RSI calculation semantics used by H2
#: (Wilder RSI(14), parity with ``app.indicators.technical.rsi_wilder``).
H2_RSI_CALC_VERSION = "WILDER_RSI__TECHNICAL_PARITY_V1"

#: Status of EVERY H2 parameter set at this stage.  Not a frozen policy:
#: freezing happens only after source coverage (#305) and the candle source
#: are approved in a separate gate.
H2_POLICY_STATUS = "PRELIMINARY_CANDIDATE_NOT_FROZEN"

# ─── variant ids ─────────────────────────────────────────────────────────────
VARIANT_TURN_1 = "H2_RSI_TURN_1"
VARIANT_TURN_2 = "H2_RSI_TURN_2"
VARIANT_PEAK_REVERSAL = "H2_RSI_PEAK_REVERSAL"
H2_VARIANT_IDS: tuple[str, ...] = (
    VARIANT_TURN_1,
    VARIANT_TURN_2,
    VARIANT_PEAK_REVERSAL,
)

# ─── delayed-entry states ────────────────────────────────────────────────────
STATE_PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
STATE_CONFIRMED_DELAYED = "CONFIRMED_DELAYED"
STATE_EXPIRED = "EXPIRED"
STATE_INVALID_SOURCE = "INVALID_SOURCE"
STATE_INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
STATE_NOT_READY = "NOT_READY"

# ─── execution-price honesty ─────────────────────────────────────────────────
#: A delayed entry has NO modeled fill: the entry price is explicitly
#: UNDETERMINED.  No execution may be assumed at the confirmation close.
ENTRY_PRICE_UNDETERMINED = "UNDETERMINED_NO_EXECUTION_MODEL"
#: The only price H2 carries for a confirmed entry is the confirmation
#: candle close — a REFERENCE, never an executable price.
ENTRY_REFERENCE_LABEL = "CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE"
#: The original source price is preserved verbatim and is NOT an execution
#: price for the delayed entry (the delayed entry happens later).
ORIGINAL_PRICE_BASIS = "SOURCE_REFERENCE_PRICE_VERBATIM_NOT_EXECUTABLE"

#: Explicit conversion policy for the original source price (PostgreSQL
#: ``NUMERIC`` arrives as :class:`decimal.Decimal`): the source value is
#: preserved VERBATIM as exact decimal text in ``original_price_exact`` and
#: is never rounded or replaced; ``original_price`` is only the documented
#: IEEE-754 float64 view used for research arithmetic.  A value that has no
#: finite float64 view is rejected outright — never silently coerced.
ORIGINAL_PRICE_PRECISION_POLICY = "SOURCE_VALUE_VERBATIM__FLOAT64_VIEW_V1"

#: Fixed historical-window policy for H2's recomputed Wilder RSI: exactly
#: :data:`CANONICAL_LOOKBACK` closed 5m buckets ENDING AT the base candle
#: ``O_0``, Wilder-seeded at the window start, then extended causally over
#: the post-signal observations.  Earlier history supplied by the caller is
#: EXCLUDED, so the same event can never get different RSI values just
#: because a longer historical prefix was loaded (and a missing window
#: bucket is an ``INCOMPLETE_COVERAGE`` failure, never a silent reseed).
H2_RSI_WINDOW_POLICY = "CANONICAL_120_CLOSED_5M_ANCHORED_AT_BASE_CANDLE_V1"

# ─── expiry / coverage ───────────────────────────────────────────────────────
EXPIRY_REASON_WINDOW_EXHAUSTED = "OBSERVATION_WINDOW_EXHAUSTED_NO_CONFIRMATION"

COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_NONE = "NONE"

# ─── feature validation reason codes (#305 semantics, reused verbatim) ───────
REASON_INCOMPLETE_FEATURES = "INCOMPLETE_FEATURES"
REASON_FEATURE_RSI14_INVALID_TYPE = "FEATURE_RSI14_INVALID_TYPE"
REASON_FEATURE_RSI14_NOT_FINITE = "FEATURE_RSI14_NOT_FINITE"
REASON_FEATURE_RSI14_OUT_OF_RANGE = "FEATURE_RSI14_OUT_OF_RANGE"
REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE = "FEATURE_RSI_DELTA_3_INVALID_TYPE"
REASON_FEATURE_RSI_DELTA_3_NOT_FINITE = "FEATURE_RSI_DELTA_3_NOT_FINITE"
REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE = (
    "FEATURE_CONFIRMATION_FLAG_INVALID_TYPE"
)

RSI_MIN = 0.0
RSI_MAX = 100.0

REQUIRED_FEATURES = frozenset({
    "rsi_14",
    "rsi_delta_3",
    "entry_confirmation_rsi_rising",
})


class H2PolicyError(ValueError):
    """Invalid policy parameter (fail closed — never guess)."""


class H2FeatureError(ValueError):
    """Invalid / incomplete original feature snapshot.

    Deterministic: ``str(exc)`` equals its ``reason_code``.
    """

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class H2Policy:
    """Parameter set for ONE H2 variant (preliminary candidate, unfrozen).

    ``observation_window`` counts post-signal CLOSED 5m candles (candidates:
    3 and 6 — NOT approved defaults).  For :data:`VARIANT_PEAK_REVERSAL`
    ``peak_rule`` must be an explicitly prespecified rule
    (:data:`PEAK_RULE_V1`) or ``None``, in which case the variant evaluates
    to ``NOT_READY`` — an undefined peak rule is never approximated.
    """

    variant_id: str
    observation_window: int
    rsi_period: int = RSI_PERIOD
    peak_rule: str | None = None
    policy_status: str = H2_POLICY_STATUS

    def __post_init__(self) -> None:
        if self.variant_id not in H2_VARIANT_IDS:
            raise H2PolicyError(f"unknown variant {self.variant_id!r}")
        if not _is_int(self.observation_window) or self.observation_window < 1:
            raise H2PolicyError("observation_window must be a positive integer")
        if not _is_int(self.rsi_period) or self.rsi_period < 1:
            raise H2PolicyError("rsi_period must be a positive integer")
        if self.variant_id == VARIANT_PEAK_REVERSAL:
            if self.peak_rule is not None and self.peak_rule not in PEAK_RULES:
                raise H2PolicyError(f"unknown peak rule {self.peak_rule!r}")
        elif self.peak_rule is not None:
            raise H2PolicyError("peak_rule is only defined for PEAK_REVERSAL")
        if self.policy_status != H2_POLICY_STATUS:
            raise H2PolicyError(
                "H2 policies are not frozen yet; policy_status must stay "
                f"{H2_POLICY_STATUS!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "observation_window": int(self.observation_window),
            "rsi_period": int(self.rsi_period),
            "peak_rule": self.peak_rule,
            "policy_status": self.policy_status,
            "rsi_calc_version": H2_RSI_CALC_VERSION,
            "rsi_window_policy": H2_RSI_WINDOW_POLICY,
        }

    def fingerprint(self) -> str:
        """Deterministic SHA-256 of the full parameter set (incl. calc
        version and unfrozen status)."""
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _require_finite_number(
    value: Any, *, invalid_type_code: str, not_finite_code: str
) -> float:
    """#305 semantics: wrong TYPE -> INVALID_TYPE; NaN/Inf -> NOT_FINITE."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise H2FeatureError(invalid_type_code)
    if not _is_finite_number(value):
        raise H2FeatureError(not_finite_code)
    return float(value)


@dataclass(frozen=True)
class SourceFeatureSnapshot:
    """ORIGINAL #305 detector features — verbatim, never recalculated.

    ``rsi_14`` here is the volatile first-capture detector RSI (it may come
    from a still-forming candle).  It is kept DISTINCT from H2's
    independently recomputed closed-candle RSI and is never replaced by it.
    """

    rsi_14: float
    rsi_delta_3: float
    entry_confirmation_rsi_rising: bool

    @classmethod
    def from_mapping(cls, features: Any) -> "SourceFeatureSnapshot":
        """Validate + copy the original feature values verbatim.

        Type/value validation ONLY (fail closed with deterministic
        #305-compatible reason codes) — values are never coerced,
        recalculated or replaced.
        """
        if not isinstance(features, Mapping) or not REQUIRED_FEATURES.issubset(
            features
        ):
            raise H2FeatureError(REASON_INCOMPLETE_FEATURES)

        raw_rsi = features["rsi_14"]
        rsi = _require_finite_number(
            raw_rsi,
            invalid_type_code=REASON_FEATURE_RSI14_INVALID_TYPE,
            not_finite_code=REASON_FEATURE_RSI14_NOT_FINITE,
        )
        if not RSI_MIN <= rsi <= RSI_MAX:
            raise H2FeatureError(REASON_FEATURE_RSI14_OUT_OF_RANGE)

        raw_delta = _require_finite_number(
            features["rsi_delta_3"],
            invalid_type_code=REASON_FEATURE_RSI_DELTA_3_INVALID_TYPE,
            not_finite_code=REASON_FEATURE_RSI_DELTA_3_NOT_FINITE,
        )

        flag = features["entry_confirmation_rsi_rising"]
        if not isinstance(flag, bool):
            raise H2FeatureError(REASON_FEATURE_CONFIRMATION_FLAG_INVALID_TYPE)

        return cls(
            rsi_14=rsi,
            rsi_delta_3=raw_delta,
            entry_confirmation_rsi_rising=flag,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rsi_14": self.rsi_14,
            "rsi_delta_3": self.rsi_delta_3,
            "entry_confirmation_rsi_rising": self.entry_confirmation_rsi_rising,
            "verbatim": True,
        }


@dataclass(frozen=True)
class H2VariantResult:
    """Immutable H2 delayed-entry outcome for ONE (source event, variant).

    Everything source-related is carried VERBATIM (see
    :meth:`lineage_payload`).  ``h2_rsi_at_confirmation`` is H2's
    independently recomputed closed-candle RSI and is always a separate
    field from ``source_rsi_14_snapshot``.
    """

    variant_id: str
    status: str
    # ── source lineage (verbatim) ──
    source_event_key: str
    source_identity: str
    symbol: str
    signal_time_us: int
    signal_time_ms: int
    activation_freeze_ts_ms: int
    lineage_hash: str
    # ── original signal facts (verbatim; NOT execution prices) ──
    original_price: float | None
    original_price_basis: str
    #: exact source value (PostgreSQL NUMERIC) preserved verbatim as decimal
    #: text — see :data:`ORIGINAL_PRICE_PRECISION_POLICY`
    original_price_exact: str | None
    source_rsi_14_snapshot: float | None
    source_rsi_delta_3_snapshot: float | None
    source_entry_confirmation_rsi_rising: bool | None
    # ── evaluation ──
    evaluation_asof_ms: int
    policy: H2Policy
    policy_fingerprint: str
    rsi_calc_version: str = H2_RSI_CALC_VERSION
    # ── confirmation / delayed entry ──
    confirmation_candle_open_ms: int | None = None
    confirmation_time_ms: int | None = None
    confirmation_time_us: int | None = None
    h2_rsi_at_confirmation: float | None = None
    entry_candidate_time_ms: int | None = None
    entry_candidate_time_us: int | None = None
    entry_candidate_price: float | None = None
    entry_price_basis: str = ""
    entry_reference_close_price: float | None = None
    entry_reference_label: str = ""
    entry_delay_ms: int | None = None
    entry_delay_us: int | None = None
    # ── lifecycle diagnostics ──
    observed_window_candles: int = 0
    expiry_reason: str | None = None
    coverage_status: str = COVERAGE_NONE
    coverage_detail: str = ""
    reason: str = ""
    detail: str = ""
    #: full 5m candle-source provenance (identity + hash + row_count +
    #: coverage + canonical window + provider reason) used for this result —
    #: the H1-C provenance contract, serialized for auditability
    candle_source: dict[str, Any] | None = None

    def lineage_payload(self) -> dict[str, Any]:
        """Exactly the preserved source lineage fields (verbatim)."""
        return {
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "signal_time_ms": self.signal_time_ms,
            "signal_time_us": self.signal_time_us,
            "activation_freeze_ts_ms": self.activation_freeze_ts_ms,
            "lineage_hash": self.lineage_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": H2_PROTOCOL_VERSION,
            "variant_id": self.variant_id,
            "status": self.status,
            "lineage": self.lineage_payload(),
            "symbol": self.symbol,
            "evaluation_asof_ms": self.evaluation_asof_ms,
            "original_price": self.original_price,
            "original_price_exact": self.original_price_exact,
            "original_price_basis": self.original_price_basis,
            "original_price_precision_policy": (
                ORIGINAL_PRICE_PRECISION_POLICY
                if self.original_price is not None
                or self.original_price_exact is not None
                else ""
            ),
            "source_rsi_14_snapshot": self.source_rsi_14_snapshot,
            "source_rsi_delta_3_snapshot": self.source_rsi_delta_3_snapshot,
            "source_entry_confirmation_rsi_rising": (
                self.source_entry_confirmation_rsi_rising
            ),
            "policy": self.policy.to_dict(),
            "policy_fingerprint": self.policy_fingerprint,
            "rsi_calc_version": self.rsi_calc_version,
            "confirmation_candle_open_ms": self.confirmation_candle_open_ms,
            "confirmation_time_ms": self.confirmation_time_ms,
            "confirmation_time_us": self.confirmation_time_us,
            "h2_rsi_at_confirmation": self.h2_rsi_at_confirmation,
            "entry_candidate_time_ms": self.entry_candidate_time_ms,
            "entry_candidate_time_us": self.entry_candidate_time_us,
            "entry_candidate_price": self.entry_candidate_price,
            "entry_price_basis": self.entry_price_basis,
            "entry_reference_close_price": self.entry_reference_close_price,
            "entry_reference_label": self.entry_reference_label,
            "entry_delay_ms": self.entry_delay_ms,
            "entry_delay_us": self.entry_delay_us,
            "observed_window_candles": self.observed_window_candles,
            "expiry_reason": self.expiry_reason,
            "coverage_status": self.coverage_status,
            "coverage_detail": self.coverage_detail,
            "reason": self.reason,
            "detail": self.detail,
            "candle_source": self.candle_source,
        }

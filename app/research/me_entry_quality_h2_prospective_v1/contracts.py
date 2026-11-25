"""Issue #312 / H2 prospective runtime — frozen contracts (research only).

Research-only, pure: no I/O, no network, no database, no clock reads.

This module fixes the vocabulary shared by the source reader, the candle
adapter, the lifecycle rules, the result store and the telemetry:

* lifecycle states — EXACTLY the six FR-04 states, reused verbatim from the
  H2 engine (:mod:`app.research.me_entry_quality_h2_v1.result`);
* ingest outcomes for #305 deliveries (accepted / rejected / duplicate);
* runtime contour statuses (``SOURCE_EMPTY_WAITING`` is an expected state,
  never an RSI-algorithm error and never proof of source coverage);
* the immutable result view used for idempotent-write conflict detection;
* the candidate policy grid (windows 3 and 6 are PRELIMINARY CANDIDATES —
  never approved defaults, never post-hoc selectable).

Everything here is RESEARCH-ONLY: nothing trades, nothing scores, nothing
gates, nothing deploys.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal
from typing import Any, Mapping

from app.research.me_entry_quality_h2_v1.result import (
    H2_POLICY_STATUS,
    H2Policy,
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
    STATE_PENDING_CONFIRMATION,
    VARIANT_PEAK_REVERSAL,
    VARIANT_TURN_1,
    VARIANT_TURN_2,
    H2_VARIANT_IDS,
)
from app.research.me_entry_quality_h2_v1.rsi_confirmation import PEAK_RULE_V1

# ─── versions ────────────────────────────────────────────────────────────────
#: Identity of THIS prospective runtime protocol (storage + lifecycle rules).
H2_PROSPECTIVE_PROTOCOL_VERSION = "H2_PROSPECTIVE_1.0.0"
#: Version of the runtime orchestration code.
H2_PROSPECTIVE_RUNTIME_VERSION = "H2_PROSPECTIVE_RUNTIME_1.0.0"
#: Algorithm version = the merged H2 engine version (PR #311) — never forked.
H2_PROSPECTIVE_ALGORITHM_VERSION = "H2_RSI_ONLY_1.0.0"
H2_PROSPECTIVE_PIPELINE_VERSION = "H2_PIPELINE_1.1.0"
H2_PROSPECTIVE_RSI_CALC_VERSION = "WILDER_RSI__TECHNICAL_PARITY_V1"

#: Lifecycle state vocabulary — FR-04, verbatim H2 states.
LIFECYCLE_STATES: tuple[str, ...] = (
    STATE_PENDING_CONFIRMATION,
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
)
TERMINAL_STATES: frozenset[str] = frozenset({
    STATE_CONFIRMED_DELAYED,
    STATE_EXPIRED,
    STATE_INCOMPLETE_COVERAGE,
    STATE_INVALID_SOURCE,
    STATE_NOT_READY,
})

# ─── ingest outcomes (#305 deliveries) ───────────────────────────────────────
INGEST_ACCEPTED = "ACCEPTED"
INGEST_DUPLICATE = "DUPLICATE"
INGEST_REJECTED_PRE_FREEZE = "REJECTED_PRE_FREEZE"
INGEST_REJECTED_LINEAGE = "REJECTED_LINEAGE_INTEGRITY"
INGEST_REJECTED_INVALID = "REJECTED_INVALID_SOURCE"
INGEST_STATUSES: frozenset[str] = frozenset({
    INGEST_ACCEPTED,
    INGEST_DUPLICATE,
    INGEST_REJECTED_PRE_FREEZE,
    INGEST_REJECTED_LINEAGE,
    INGEST_REJECTED_INVALID,
})

# ─── source-integrity reason codes ───────────────────────────────────────────
REASON_PRE_FREEZE_SIGNAL_TIME = "PRE_FREEZE_SIGNAL_TIME"
REASON_PRE_FREEZE_CAPTURED_AT = "PRE_FREEZE_CAPTURED_AT"
REASON_INVALID_SOURCE_TIMING = "INVALID_SOURCE_TIMING"
REASON_INVALID_SOURCE_PRICE = "INVALID_SOURCE_PRICE"
REASON_LINEAGE_HASH_MISMATCH = "LINEAGE_HASH_MISMATCH"
REASON_EVENT_KEY_LINEAGE_CONFLICT = "EVENT_KEY_LINEAGE_CONFLICT"
REASON_SOURCE_ROW_PARSE_ERROR = "SOURCE_ROW_PARSE_ERROR"
REASON_OK = "OK"

# ─── type-safe source price handling (review P1-Decimal) ─────────────────────
#: Plain decimal text only — no signs, exponents, whitespace games or
#: arbitrary strings from untrusted sources.
_PRICE_TEXT = re.compile(r"^[0-9]+(?:\.[0-9]+)?$|^\.[0-9]+$")


def parse_source_price(value: Any) -> Decimal:
    """Canonical, EXACT ``source_reference_price`` parse (fail closed).

    Review P1-Decimal: PostgreSQL ``NUMERIC`` values arrive as
    :class:`decimal.Decimal` and must reach the H2 engine (which accepts only
    ``int`` / ``float`` / ``Decimal``) without losing precision and without
    ever round-tripping through ``float``.

    Accepts ``Decimal`` (kept exact), ``int``, finite ``float`` (converted
    via its shortest round-trip repr — never binary artifacts), and plain
    decimal strings matching :data:`_PRICE_TEXT` (strict — arbitrary strings
    are rejected).  Rejects ``bool``, ``NaN``, ``Infinity``, zero and
    negative values.
    """
    if isinstance(value, bool):
        raise ValueError("source price must not be a bool")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("source price must be finite")
        parsed = Decimal(repr(value))
    elif isinstance(value, str):
        # Strict: plain decimal digits only — no surrounding whitespace,
        # signs, exponents or thousands separators (untrusted-input guard).
        if not _PRICE_TEXT.match(value):
            raise ValueError(
                "source price string must be plain decimal digits"
            )
        parsed = Decimal(value)
    else:
        raise ValueError(
            f"unsupported source price type: {type(value).__name__}"
        )
    if not parsed.is_finite():
        raise ValueError("source price must be finite")
    if parsed <= 0:
        raise ValueError("source price must be positive")
    return parsed


def exact_price_str(value: Any) -> str:
    """Exact decimal TEXT of a source price for loss-free JSON persistence."""
    return format(parse_source_price(value), "f")

# ─── runtime contour statuses (telemetry / health) ───────────────────────────
RUNTIME_RUNNING = "RUNNING"
#: No #305 source events exist yet.  EXPECTED state: it is neither an RSI
#: algorithm error nor evidence that source coverage works (FR-08).
RUNTIME_SOURCE_EMPTY_WAITING = "SOURCE_EMPTY_WAITING"
#: Candle source unavailable / budget unconfirmed — provider stays OFF.
RUNTIME_BLOCKED_CANDLE_SOURCE = "BLOCKED_CANDLE_SOURCE"
RUNTIME_DISABLED = "DISABLED"

#: Log line emitted verbatim when the #305 cohort has no events (criterion 4).
SOURCE_EMPTY_WAITING_LOG = "SOURCE_EMPTY_WAITING"

# ─── candidate policy grid (PRELIMINARY, unfrozen) ───────────────────────────
#: Preliminary candidate observation windows (post-signal CLOSED 5m candles).
#: NOT approved defaults: freezing happens in a separate gate after source
#: coverage and the candle source are approved; they may never be selected or
#: optimized post-hoc against outcomes.
CANDIDATE_OBSERVATION_WINDOWS: tuple[int, ...] = (3, 6)


def candidate_policies(
    windows: tuple[int, ...] = CANDIDATE_OBSERVATION_WINDOWS,
) -> tuple[H2Policy, ...]:
    """The preliminary-candidate policy grid: every variant x every window.

    Every policy carries ``PRELIMINARY_CANDIDATE_NOT_FROZEN`` (enforced by
    :class:`H2Policy` itself).  ``H2_RSI_PEAK_REVERSAL`` uses the single
    prespecified :data:`PEAK_RULE_V1`; an undefined peak rule is never
    approximated.
    """
    policies: list[H2Policy] = []
    for variant_id in H2_VARIANT_IDS:
        for window in windows:
            policies.append(
                H2Policy(
                    variant_id=variant_id,
                    observation_window=int(window),
                    peak_rule=(
                        PEAK_RULE_V1
                        if variant_id == VARIANT_PEAK_REVERSAL
                        else None
                    ),
                )
            )
    return tuple(policies)


def policy_identity(policy: H2Policy) -> tuple[str, str]:
    """``(variant_id, policy_fingerprint)`` — the immutable result key part."""
    return policy.variant_id, policy.fingerprint()


def policy_manifest(
    policies: tuple[H2Policy, ...] | list[H2Policy],
) -> dict[str, Any]:
    """Deterministic manifest ``fingerprint -> parameter set`` (for pinning)."""
    return {
        policy.fingerprint(): policy.to_dict()
        for policy in sorted(
            policies, key=lambda p: (p.variant_id, p.observation_window)
        )
    }


# ─── immutable result view (idempotent-write conflict detection) ─────────────
#: The FR-05 fields that define WHAT a terminal outcome IS.  A re-attempted
#: write of the same (event, variant, policy) key must match this view
#: bit-for-bit; any difference is an explicit immutable-data conflict.
IMMUTABLE_RESULT_KEYS: tuple[str, ...] = (
    "variant_id",
    "status",
    "source_event_key",
    "source_identity",
    "symbol",
    "signal_time_us",
    "signal_time_ms",
    "activation_freeze_ts_ms",
    "lineage_hash",
    "original_price_exact",
    "source_rsi_14_snapshot",
    "source_rsi_delta_3_snapshot",
    "source_entry_confirmation_rsi_rising",
    "policy_fingerprint",
    "rsi_calc_version",
    "confirmation_candle_open_ms",
    "confirmation_time_ms",
    "confirmation_time_us",
    "h2_rsi_at_confirmation",
    "entry_candidate_time_ms",
    "entry_candidate_time_us",
    "entry_candidate_price",
    "entry_price_basis",
    "entry_reference_close_price",
    "entry_reference_label",
    "entry_delay_ms",
    "entry_delay_us",
    "observed_window_candles",
    "expiry_reason",
    "coverage_status",
    "coverage_detail",
    "reason",
)


def immutable_result_view(result: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable subset of a serialized H2 result (see keys above)."""
    return {key: result.get(key) for key in IMMUTABLE_RESULT_KEYS}


def immutable_result_hash(result: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 of :func:`immutable_result_view`."""
    payload = json.dumps(
        immutable_result_view(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── coverage-defect classes (lifecycle finalization) ────────────────────────
#: Coverage defects that cannot heal with a later refetch: the referenced
#: history is missing from the source payload itself.
PERMANENT_COVERAGE_REASONS: frozenset[str] = frozenset({
    "CANDLE_GAP",
    "CANONICAL_WINDOW_INCOMPLETE",
    "INSUFFICIENT_RSI_WARMUP",
    "MISSING_BASE_CANDLE",
})
#: Transient fetch/lag conditions: a later evaluation may still succeed.
TRANSIENT_COVERAGE_REASONS: frozenset[str] = frozenset({
    "EMPTY_SERIES",
    "NO_CLOSED_CANDLES",
    "MISSING_LATEST_CLOSED_CANDLE",
    "INSUFFICIENT_HISTORY",
    "CANDLE_FETCH_ERROR",
    "CANDLE_PROVIDER_DISABLED",
})


def is_permanent_coverage_reason(reason: str) -> bool:
    return reason in PERMANENT_COVERAGE_REASONS

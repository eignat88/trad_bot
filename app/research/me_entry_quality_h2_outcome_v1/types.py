"""Issue #298 / #312 — outcome-model data types and status vocabulary.

Research-only, pure: no I/O, no network, no database, no clock reads.
Only plain dataclasses over caller-supplied plain data (``dict`` rows or
dataclass instances) — the package is standalone and consumes the H2
engine's results only as plain mappings if at all.

Honesty labels reused VERBATIM from the merged H2 engine
(``app.research.me_entry_quality_h2_v1``):

* :data:`ENTRY_PRICE_UNDETERMINED` — ``UNDETERMINED_NO_EXECUTION_MODEL``;
* :data:`CONFIRMATION_CLOSE_REFERENCE_LABEL` —
  ``CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE``.

The confirmation-candle close is NEVER an executable price anywhere in this
package (issue #312 / FR-06 hard rule).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h2_outcome_v1.policy import (
    RESULT_STATUS_AGGREGATE_READY,
    RESULT_STATUS_EVALUATED,
    RESULT_STATUS_NOT_READY_PENDING_DECISION,
)

OUTCOME_MODEL_VERSION = "H2_OUTCOME_MODEL_1.0.0"

#: Causal path policy for the candle CONTAINING the entry instant: an
#: intra-candle entry cannot separate pre-entry from post-entry wick action,
#: so that candle is excluded from WICK-path MAE/MFE and from exit-touch
#: evaluation unless the entry happened at (or before) that candle's OPEN
#: (then the whole candle is post-entry).  Close-only sampling uses the
#: close of that candle because the close is strictly after the entry.
ENTRY_CANDLE_PATH_POLICY = (
    "ENTRY_CANDLE_INCLUDED_ONLY_WHEN_ENTRY_AT_OR_BEFORE_OPEN__"
    "INTRACANDLE_PRE_ENTRY_ACTION_UNSEPARABLE_V1"
)

#: Horizon exits occur at the last sampled CLOSE (documented assumption;
#: a stop/target exit occurs at the level itself).
EXIT_PRICE_POLICY = (
    "STOP_TARGET_EXIT_AT_LEVEL__HORIZON_EXIT_AT_LAST_CLOSE_V1"
)

# ─── honesty labels (verbatim from the H2 engine) ────────────────────────────
ENTRY_PRICE_UNDETERMINED = "UNDETERMINED_NO_EXECUTION_MODEL"
CONFIRMATION_CLOSE_REFERENCE_LABEL = (
    "CONFIRMATION_CANDLE_CLOSE_REFERENCE_ONLY_NOT_EXECUTABLE"
)
ORIGINAL_PRICE_BASIS_LABEL = "SOURCE_REFERENCE_PRICE_VERBATIM_NOT_EXECUTABLE"

# ─── entry / outcome states ──────────────────────────────────────────────────
ENTRY_ENTERED = "ENTERED"
ENTRY_EXPIRED = "ENTRY_EXPIRED"
ENTRY_MISSED = "MISSED_ENTRY"
ENTRY_PENDING = "ENTRY_PENDING_NOT_RESOLVABLE_YET"
ENTRY_NO_CONFIRMATION = "NO_CONFIRMATION"
ENTRY_INVALID = "INVALID_INPUT"

#: non-entry outcome states used for events without a usable path
OUTCOME_INCOMPLETE_COVERAGE = "INCOMPLETE_COVERAGE"
OUTCOME_NOT_READY = "NOT_READY_PENDING_DECISION"

# ─── missed-entry reasons (D-2) ──────────────────────────────────────────────
MISSED_REASON_GAP_THROUGH = "GAP_THROUGH_NO_FILL"
MISSED_REASON_LOCK_LIMIT = "LOCK_LIMIT_NO_LIQUIDITY"
MISSED_REASON_NO_LIQUIDITY = "NO_LIQUIDITY"
MISSED_REASON_NO_TRADE_PRINT = "NO_TRADE_PRINT_AT_OR_AFTER_OPEN"
EXPIRY_REASON_DEADLINE = "ENTRY_DEADLINE_REACHED_NO_ADMISSIBLE_FILL"

# ─── exit states (D-6) ───────────────────────────────────────────────────────
EXIT_STOP = "EXIT_STOP"
EXIT_TARGET = "EXIT_TARGET"
EXIT_HORIZON = "EXIT_HORIZON"
EXIT_NONE = "EXIT_NONE_NO_POSITION"

#: statuses under which the variant leg did NOT enter (D-10 membership)
NON_ENTRY_STATUSES = (
    ENTRY_MISSED,
    ENTRY_EXPIRED,
    ENTRY_NO_CONFIRMATION,
    ENTRY_PENDING,
    ENTRY_INVALID,
    OUTCOME_INCOMPLETE_COVERAGE,
)

CANDLE_5M_MS = 300_000
US_PER_MS = 1_000


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}: expected a finite number")
    return number


@dataclass(frozen=True)
class CandleBar:
    """One fully-defined 5m OHLCV bar (``open_time_ms`` = UTC epoch ms of the
    bar OPEN).  Plain-data input: :meth:`from_mapping` accepts a ``dict``."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.open_time_ms, bool)
            or not isinstance(self.open_time_ms, int)
        ):
            raise ValueError("open_time_ms must be an integer of epoch ms")
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.volume < 0.0:
            raise ValueError("volume must be >= 0")
        if self.high < max(self.open, self.close) or self.high < self.low:
            raise ValueError("invalid candle: high")
        if self.low > min(self.open, self.close) or self.low > self.high:
            raise ValueError("invalid candle: low")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "CandleBar":
        return cls(
            open_time_ms=int(row["open_time_ms"]),
            open=_finite(row["open"], "open"),
            high=_finite(row["high"], "high"),
            low=_finite(row["low"], "low"),
            close=_finite(row["close"], "close"),
            volume=_finite(row.get("volume", 0.0), "volume"),
        )

    @property
    def open_time_us(self) -> int:
        return self.open_time_ms * US_PER_MS

    @property
    def close_time_us(self) -> int:
        """UTC epoch µs of the bar CLOSE (open + 5m, exclusive bound)."""
        return (self.open_time_ms + CANDLE_5M_MS) * US_PER_MS

    @property
    def is_locked(self) -> bool:
        """A locked market: high == low (no tradable range)."""
        return self.high == self.low

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_time_ms": self.open_time_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class TradePrint:
    """One trade print for the FIRST_TRADE_AT_OR_AFTER_OPEN entry class."""

    time_us: int
    price: float

    def __post_init__(self) -> None:
        if isinstance(self.time_us, bool) or not isinstance(self.time_us, int):
            raise ValueError("time_us must be an integer")
        object.__setattr__(self, "price", _finite(self.price, "price"))
        if self.price <= 0.0:
            raise ValueError("price must be > 0")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "TradePrint":
        return cls(time_us=int(row["time_us"]), price=_finite(row["price"], "price"))


@dataclass(frozen=True)
class ConfirmationInfo:
    """The H2 confirmation facts for ONE (source event, variant).

    ``state`` is the pure H2 delayed-entry state (``CONFIRMED_DELAYED`` /
    ``EXPIRED`` / ...).  ``confirmation_close_reference`` is the NON-
    executable reference close of the confirming candle (carried only with
    its :data:`CONFIRMATION_CLOSE_REFERENCE_LABEL`).
    """

    state: str
    confirmation_time_us: int | None
    confirmation_candle_open_ms: int | None = None
    confirmation_close_reference: float | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ConfirmationInfo":
        time_us = row.get("confirmation_time_us")
        return cls(
            state=str(row["state"]),
            confirmation_time_us=None if time_us is None else int(time_us),
            confirmation_candle_open_ms=(
                None
                if row.get("confirmation_candle_open_ms") is None
                else int(row["confirmation_candle_open_ms"])
            ),
            confirmation_close_reference=(
                None
                if row.get("confirmation_close_reference") is None
                else _finite(
                    row["confirmation_close_reference"],
                    "confirmation_close_reference",
                )
            ),
        )


@dataclass(frozen=True)
class SourceEventInput:
    """One #305 source event (identity + facts carried VERBATIM).

    ``original_price`` is the source reference price basis (label
    :data:`ORIGINAL_PRICE_BASIS_LABEL`) — the BASELINE leg's entry price,
    never the delayed variant's execution price.
    """

    source_event_key: str
    variant_id: str
    symbol: str
    direction: str
    signal_time_us: int
    original_price: float | None
    source_stop_price: float | None = None
    source_target_price: float | None = None
    original_price_exact: str | None = None
    lineage_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("source_event_key", "variant_id", "symbol"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if self.direction not in ("SHORT", "LONG"):
            raise ValueError("direction must be 'SHORT' or 'LONG'")
        if (
            isinstance(self.signal_time_us, bool)
            or not isinstance(self.signal_time_us, int)
        ):
            raise ValueError("signal_time_us must be an integer of epoch µs")
        for name in (
            "original_price",
            "source_stop_price",
            "source_target_price",
        ):
            value = getattr(self, name)
            if value is not None:
                number = _finite(value, name)
                if number <= 0.0:
                    raise ValueError(f"{name} must be > 0")
                object.__setattr__(self, name, number)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "SourceEventInput":
        def opt(name: str) -> float | None:
            value = row.get(name)
            return None if value is None else _finite(value, name)

        return cls(
            source_event_key=str(row["source_event_key"]),
            variant_id=str(row["variant_id"]),
            symbol=str(row["symbol"]),
            direction=str(row.get("direction", "SHORT")),
            signal_time_us=int(row["signal_time_us"]),
            original_price=opt("original_price"),
            source_stop_price=opt("source_stop_price"),
            source_target_price=opt("source_target_price"),
            original_price_exact=row.get("original_price_exact"),
            lineage_hash=str(row.get("lineage_hash", "")),
        )

    @property
    def outcome_key_prefix(self) -> tuple[str, str]:
        return (self.source_event_key, self.variant_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_key": self.source_event_key,
            "variant_id": self.variant_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "signal_time_us": self.signal_time_us,
            "original_price": self.original_price,
            "original_price_exact": self.original_price_exact,
            "original_price_basis": ORIGINAL_PRICE_BASIS_LABEL,
            "source_stop_price": self.source_stop_price,
            "source_target_price": self.source_target_price,
            "lineage_hash": self.lineage_hash,
        }


@dataclass(frozen=True)
class LegOutcome:
    """Executable-model outcome for ONE leg (baseline or variant) of ONE
    source event.  Every price field carries its provenance label."""

    leg_name: str
    entry_status: str
    entry_reason: str
    entry_time_us: int | None
    entry_price: float | None
    entry_price_label: str
    exit_status: str
    exit_price: float | None
    exit_time_us: int | None
    held_ms: int
    gross_return_pct: float | None
    gross_return_r: float | None
    net_return_pct: float | None
    net_return_r: float | None
    mae_pct: float | None
    mfe_pct: float | None
    mae_r: float | None
    mfe_r: float | None
    mae_mfe_path_sampling: str
    mae_mfe_normalization: str
    cost_breakdown: dict[str, Any] | None

    @property
    def entered(self) -> bool:
        return self.entry_status == ENTRY_ENTERED

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_name": self.leg_name,
            "entry_status": self.entry_status,
            "entry_reason": self.entry_reason,
            "entry_time_us": self.entry_time_us,
            "entry_price": self.entry_price,
            "entry_price_label": self.entry_price_label,
            "exit_status": self.exit_status,
            "exit_price": self.exit_price,
            "exit_time_us": self.exit_time_us,
            "held_ms": self.held_ms,
            "gross_return_pct": self.gross_return_pct,
            "gross_return_r": self.gross_return_r,
            "net_return_pct": self.net_return_pct,
            "net_return_r": self.net_return_r,
            "mae_pct": self.mae_pct,
            "mfe_pct": self.mfe_pct,
            "mae_r": self.mae_r,
            "mfe_r": self.mfe_r,
            "mae_mfe_path_sampling": self.mae_mfe_path_sampling,
            "mae_mfe_normalization": self.mae_mfe_normalization,
            "cost_breakdown": self.cost_breakdown,
        }


@dataclass(frozen=True)
class EventPairOutcome:
    """Paired outcome for ONE (source event, variant, policy_fingerprint):
    baseline (original instant, original price basis) vs variant (delayed
    confirmation entry) under the SAME execution model (D-7)."""

    source_event_key: str
    variant_id: str
    policy_fingerprint: str
    policy_status: str
    evaluation_asof_us: int
    status: str
    reason: str
    baseline: LegOutcome
    variant: LegOutcome
    missed_winner: bool
    missed_winner_reason: str
    decision_snapshot: dict[str, Any]
    publication_status: str = "NOT_FOR_PUBLICATION_H2_ACTIVATION_HOLD"

    @property
    def outcome_key(self) -> tuple[str, str, str]:
        """Idempotency key (D-12 plumbing): (source_event_key, variant_id,
        policy_fingerprint)."""
        return (self.source_event_key, self.variant_id, self.policy_fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_model_version": OUTCOME_MODEL_VERSION,
            "source_event_key": self.source_event_key,
            "variant_id": self.variant_id,
            "policy_fingerprint": self.policy_fingerprint,
            "policy_status": self.policy_status,
            "evaluation_asof_us": self.evaluation_asof_us,
            "status": self.status,
            "reason": self.reason,
            "baseline": self.baseline.to_dict(),
            "variant": self.variant.to_dict(),
            "missed_winner": self.missed_winner,
            "missed_winner_reason": self.missed_winner_reason,
            "decision_snapshot": self.decision_snapshot,
            "publication_status": self.publication_status,
            "entry_candle_path_policy": ENTRY_CANDLE_PATH_POLICY,
            "exit_price_policy": EXIT_PRICE_POLICY,
            "confirmation_close_reference_label": (
                CONFIRMATION_CLOSE_REFERENCE_LABEL
            ),
        }


@dataclass(frozen=True)
class AggregateOutcome:
    """The ONLY object that may carry yield / Net E[R] / win-rate / MAE-MFE
    aggregates — and it refuses (status
    :data:`RESULT_STATUS_NOT_READY_PENDING_DECISION`, every metric ``None``)
    unless every required decision is specified AND the policy fingerprint
    is approved (``FROZEN``)."""

    status: str
    reason: str
    missing_decisions: tuple[str, ...]
    policy_fingerprint: str
    policy_status: str
    denominator_definition: str
    acceptance_rule: dict[str, Any]
    n_total_source_events: int | None
    n_in_denominator: int | None
    n_excluded_by_denominator: int | None
    counts_by_status: dict[str, int]
    net_er: float | None
    win_rate: float | None
    mean_mae: float | None
    mean_mfe: float | None
    missed_winner_count: int | None
    missed_winner_keys: tuple[str, ...]
    denominator_notes: tuple[str, ...]
    metric_labels: dict[str, str]

    @property
    def refused(self) -> bool:
        return self.status == RESULT_STATUS_NOT_READY_PENDING_DECISION

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_model_version": OUTCOME_MODEL_VERSION,
            "status": self.status,
            "reason": self.reason,
            "missing_decisions": list(self.missing_decisions),
            "policy_fingerprint": self.policy_fingerprint,
            "policy_status": self.policy_status,
            "denominator_definition": self.denominator_definition,
            "acceptance_rule": self.acceptance_rule,
            "n_total_source_events": self.n_total_source_events,
            "n_in_denominator": self.n_in_denominator,
            "n_excluded_by_denominator": self.n_excluded_by_denominator,
            "counts_by_status": dict(self.counts_by_status),
            "net_er": self.net_er,
            "win_rate": self.win_rate,
            "mean_mae": self.mean_mae,
            "mean_mfe": self.mean_mfe,
            "missed_winner_count": self.missed_winner_count,
            "missed_winner_keys": list(self.missed_winner_keys),
            "denominator_notes": list(self.denominator_notes),
            "metric_labels": dict(self.metric_labels),
            "publication_status": "NOT_FOR_PUBLICATION_H2_ACTIVATION_HOLD",
        }


def closed_candles(
    candles: Sequence[CandleBar | Mapping[str, Any]], evaluation_asof_us: int
) -> tuple[CandleBar, ...]:
    """Only candles FULLY CLOSED at ``evaluation_asof_us`` (µs precision),
    in ascending open-time order, deduplicated by open time (first wins).

    A candle is closed iff ``close_time_us <= evaluation_asof_us``
    (inclusive at the close boundary — exactly at the close it IS closed).
    """
    if (
        isinstance(evaluation_asof_us, bool)
        or not isinstance(evaluation_asof_us, int)
    ):
        raise ValueError("evaluation_asof_us must be an integer of epoch µs")
    by_open: dict[int, CandleBar] = {}
    for row in candles:
        bar = row if isinstance(row, CandleBar) else CandleBar.from_mapping(row)
        if bar.open_time_ms not in by_open:
            by_open[bar.open_time_ms] = bar
    return tuple(
        bar
        for _, bar in sorted(by_open.items())
        if bar.close_time_us <= evaluation_asof_us
    )


def not_ready_pair(
    event: SourceEventInput,
    *,
    policy_fingerprint: str,
    policy_status: str,
    evaluation_asof_us: int,
    reason: str,
    decision_snapshot: dict[str, Any],
) -> EventPairOutcome:
    """A refused per-event evaluation: NO numbers, only the refusal status
    (mirrors the H2 engine's ``UNDETERMINED_NO_EXECUTION_MODEL`` honesty)."""

    def refusal_leg(name: str) -> LegOutcome:
        return LegOutcome(
            leg_name=name,
            entry_status=OUTCOME_NOT_READY,
            entry_reason=reason,
            entry_time_us=None,
            entry_price=None,
            entry_price_label=ENTRY_PRICE_UNDETERMINED,
            exit_status=EXIT_NONE,
            exit_price=None,
            exit_time_us=None,
            held_ms=0,
            gross_return_pct=None,
            gross_return_r=None,
            net_return_pct=None,
            net_return_r=None,
            mae_pct=None,
            mfe_pct=None,
            mae_r=None,
            mfe_r=None,
            mae_mfe_path_sampling="",
            mae_mfe_normalization="",
            cost_breakdown=None,
        )

    return EventPairOutcome(
        source_event_key=event.source_event_key,
        variant_id=event.variant_id,
        policy_fingerprint=policy_fingerprint,
        policy_status=policy_status,
        evaluation_asof_us=evaluation_asof_us,
        status=RESULT_STATUS_NOT_READY_PENDING_DECISION,
        reason=reason,
        baseline=refusal_leg("BASELINE_ORIGINAL_SIGNAL"),
        variant=refusal_leg("VARIANT_DELAYED_CONFIRMATION"),
        missed_winner=False,
        missed_winner_reason="",
        decision_snapshot=decision_snapshot,
    )

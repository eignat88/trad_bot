"""Issue #298 / #312 — :class:`OutcomePolicy`: the D-1..D-12 decision object.

Research-only, pure: no I/O, no network, no database, no clock reads.

Every one of the twelve outcome-policy decisions (``docs/research/
312_h2_entry_outcome_policy_v1.md`` §3) is a NAMED parameter of
:class:`OutcomePolicy` and carries its own three-state decision status:

* :data:`DECISION_PENDING` — ``PENDING_OWNER_DECISION``, the DEFAULT for
  every undecided item.  Nothing is ever assumed on the owner's behalf;
* :data:`DECISION_PRELIMINARY` — ``PRELIMINARY_CANDIDATE_NOT_FROZEN``: a
  concrete candidate value that has been written down but NOT approved;
* :data:`DECISION_FROZEN` — ``FROZEN``: reachable ONLY through
  :meth:`OutcomePolicy.freeze` with an explicit approved fingerprint that
  matches the policy's own specification fingerprint.

Honesty contract (mirrors the H2 engine's ``UNDETERMINED_NO_EXECUTION_MODEL``
precedent): while any decision is pending — or while the fingerprint is not
approved — the outcome model REFUSES to emit any yield / Net E[R] /
win-rate / MAE-MFE aggregate.  The refusal is a RESULT STATUS
(:data:`RESULT_STATUS_NOT_READY_PENDING_DECISION`), never a number.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.research.me_entry_quality_h2_outcome_v1.costs import CostModel

OUTCOME_POLICY_VERSION = "H2_OUTCOME_POLICY_1.0.0"

# ─── D-12: the three decision states ─────────────────────────────────────────
DECISION_PENDING = "PENDING_OWNER_DECISION"
DECISION_PRELIMINARY = "PRELIMINARY_CANDIDATE_NOT_FROZEN"
DECISION_FROZEN = "FROZEN"
DECISION_STATUSES = (DECISION_PENDING, DECISION_PRELIMINARY, DECISION_FROZEN)

#: Refusal result status for every metric/aggregate the model will not emit.
RESULT_STATUS_NOT_READY_PENDING_DECISION = "NOT_READY_PENDING_DECISION"

#: Result status of an aggregate that IS computed under a frozen, approved
#: policy.  Research-only: publication is still forbidden while #298 stands
#: at ``ACTIVATION_HOLD``.
RESULT_STATUS_AGGREGATE_READY = "AGGREGATE_READY_FROZEN_POLICY_RESEARCH_ONLY"

#: Result status of a per-event evaluation that computed outcome numbers
#: under specified (possibly still unfrozen) decisions.  Numbers on a
#: per-event record are diagnostics for review — never publishable metrics.
RESULT_STATUS_EVALUATED = "EVALUATED_RESEARCH_ONLY_NOT_FOR_PUBLICATION"


class OutcomePolicyError(ValueError):
    """Invalid policy / decision state (fail closed — never guess)."""


# ─── canonical serialization for fingerprints ────────────────────────────────
def jsonable(value: Any) -> Any:
    """Deterministic JSON-safe view of a decision value (fail closed)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise OutcomePolicyError("non-finite float in decision value")
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(val) for key, val in sorted(value.items())}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return jsonable(to_dict())
    raise OutcomePolicyError(
        f"decision value of type {type(value).__name__} is not serializable"
    )


def _sha256(payload: dict[str, Any]) -> str:
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── D-1: executable entry price class ───────────────────────────────────────
ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN = "NEXT_CANDLE_OPEN"
ENTRY_PRICE_CLASS_FIRST_TRADE = "FIRST_TRADE_AT_OR_AFTER_OPEN"
ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL = "LIMIT_AT_LEVEL"
ENTRY_PRICE_CLASSES = (
    ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN,
    ENTRY_PRICE_CLASS_FIRST_TRADE,
    ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL,
)


@dataclass(frozen=True)
class EntrySpec:
    """D-1 — the admissible executable entry price class.

    The entry may occur only at an admissible price class STRICTLY AFTER the
    confirmation instant.  The confirmation-candle close is NEVER executable
    and is never a member of any price class.
    """

    price_class: str
    #: required for ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL
    limit_level_price: float | None = None
    #: data-source label for the trade prints of the first-trade class
    trade_print_source: str = ""

    def __post_init__(self) -> None:
        if self.price_class not in ENTRY_PRICE_CLASSES:
            raise OutcomePolicyError(f"unknown entry price class {self.price_class!r}")
        if self.price_class == ENTRY_PRICE_CLASS_LIMIT_AT_LEVEL:
            if isinstance(self.limit_level_price, bool) or not isinstance(
                self.limit_level_price, (int, float)
            ):
                raise OutcomePolicyError(
                    "LIMIT_AT_LEVEL requires an explicit limit_level_price"
                )
            if not (self.limit_level_price > 0):
                raise OutcomePolicyError("limit_level_price must be > 0")
        elif self.limit_level_price is not None:
            raise OutcomePolicyError(
                "limit_level_price is only defined for LIMIT_AT_LEVEL"
            )
        if self.price_class == ENTRY_PRICE_CLASS_FIRST_TRADE:
            if not isinstance(self.trade_print_source, str) or not (
                self.trade_print_source.strip()
            ):
                raise OutcomePolicyError(
                    "FIRST_TRADE_AT_OR_AFTER_OPEN requires a trade_print_source "
                    "data-source label"
                )
        elif self.trade_print_source:
            raise OutcomePolicyError(
                "trade_print_source is only defined for FIRST_TRADE_AT_OR_AFTER_OPEN"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_class": self.price_class,
            "limit_level_price": self.limit_level_price,
            "trade_print_source": self.trade_print_source,
        }


# ─── D-2: fill / missed-entry policy ─────────────────────────────────────────
GAP_THROUGH_FILL_AT_OPEN = "FILL_AT_OPEN"
GAP_THROUGH_MISSED_ENTRY = "MISSED_ENTRY_GAP_THROUGH"
GAP_THROUGH_POLICIES = (GAP_THROUGH_FILL_AT_OPEN, GAP_THROUGH_MISSED_ENTRY)

MISSED_STAT_ZERO_NET = "ZERO_NET_RETURN_IN_DENOMINATOR"
MISSED_STAT_FORGONE_BASELINE = "BASELINE_NET_AS_FORGONE_RETURN"
MISSED_STAT_POLICIES = (MISSED_STAT_ZERO_NET, MISSED_STAT_FORGONE_BASELINE)


@dataclass(frozen=True)
class FillPolicy:
    """D-2 — what happens when the market never trades at the admissible
    price (gap-through, lock-limit, no liquidity).

    ``gap_through_policy`` decides whether a favorable opening gap through a
    limit level counts as a fill at the open or as ``MISSED_ENTRY``.
    ``missed_entry_stat_policy`` decides how ``MISSED_ENTRY`` /
    ``ENTRY_EXPIRED`` events are carried into the statistics — they ALWAYS
    stay in the denominator accounting (D-8); this choice only fixes the
    return assignment, never the membership.
    """

    gap_through_policy: str
    missed_entry_stat_policy: str

    def __post_init__(self) -> None:
        if self.gap_through_policy not in GAP_THROUGH_POLICIES:
            raise OutcomePolicyError(
                f"unknown gap-through policy {self.gap_through_policy!r}"
            )
        if self.missed_entry_stat_policy not in MISSED_STAT_POLICIES:
            raise OutcomePolicyError(
                f"unknown missed-entry stat policy "
                f"{self.missed_entry_stat_policy!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_through_policy": self.gap_through_policy,
            "missed_entry_stat_policy": self.missed_entry_stat_policy,
        }


# ─── D-3: cost model (hooks live in costs.py) ────────────────────────────────
# (CostModel is imported above; its value object carries the D-3 decision.)


# ─── D-4: entry deadline / expiry ────────────────────────────────────────────
@dataclass(frozen=True)
class EntryDeadline:
    """D-4 — how long after the confirmation instant the delayed entry may
    still be executed before it expires as ``ENTRY_EXPIRED``.

    Measured from the confirmation instant at microsecond precision.
    """

    max_delay_ms: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_delay_ms, bool)
            or not isinstance(self.max_delay_ms, int)
            or self.max_delay_ms < 1
        ):
            raise OutcomePolicyError("max_delay_ms must be a positive integer")

    @property
    def max_delay_us(self) -> int:
        return self.max_delay_ms * 1000

    def to_dict(self) -> dict[str, Any]:
        return {"max_delay_ms": self.max_delay_ms}


# ─── D-5: SL/TP geometry ─────────────────────────────────────────────────────
GEOMETRY_SOURCE_VERBATIM = "SOURCE_VERBATIM"
GEOMETRY_REBASED_AT_ENTRY = "REBASED_AT_ENTRY"
GEOMETRY_RE_DERIVED = "RE_DERIVED_EXPLICIT_DISTANCES"
GEOMETRY_MODES = (
    GEOMETRY_SOURCE_VERBATIM,
    GEOMETRY_REBASED_AT_ENTRY,
    GEOMETRY_RE_DERIVED,
)


@dataclass(frozen=True)
class SlTpGeometryPolicy:
    """D-5 — stop/target geometry for the evaluated leg.

    * ``SOURCE_VERBATIM`` — the source event's stop/target prices are reused
      VERBATIM (same absolute levels for baseline and variant);
    * ``REBASED_AT_ENTRY`` — the source event's stop/target DISTANCES are
      re-applied at each leg's own entry price;
    * ``RE_DERIVED_EXPLICIT_DISTANCES`` — the distances are taken from this
      policy's explicit ``stop_distance`` / ``target_distance`` values
      (nothing is derived from outcomes or from data at runtime).
    """

    mode: str
    stop_distance: float | None = None
    target_distance: float | None = None

    def __post_init__(self) -> None:
        if self.mode not in GEOMETRY_MODES:
            raise OutcomePolicyError(f"unknown geometry mode {self.mode!r}")
        if self.mode == GEOMETRY_RE_DERIVED:
            for name in ("stop_distance", "target_distance"):
                value = getattr(self, name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise OutcomePolicyError(
                        f"RE_DERIVED_EXPLICIT_DISTANCES requires {name}"
                    )
                if not (value > 0):
                    raise OutcomePolicyError(f"{name} must be > 0")
        elif self.stop_distance is not None or self.target_distance is not None:
            raise OutcomePolicyError(
                "stop_distance/target_distance are only defined for "
                "RE_DERIVED_EXPLICIT_DISTANCES"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "stop_distance": self.stop_distance,
            "target_distance": self.target_distance,
        }


# ─── D-6: outcome horizon & exit rule ────────────────────────────────────────
EXIT_RULE_STOP_OR_TARGET = "STOP_OR_TARGET_ONLY"
EXIT_RULE_HORIZON_ONLY = "FIXED_HORIZON_ONLY"
EXIT_RULE_STOP_TARGET_OR_HORIZON = "STOP_OR_TARGET_THEN_HORIZON"
EXIT_RULES = (EXIT_RULE_STOP_OR_TARGET, EXIT_RULE_HORIZON_ONLY,
              EXIT_RULE_STOP_TARGET_OR_HORIZON)

TIE_BREAK_STOP_FIRST = "STOP_FIRST_CONSERVATIVE"
TIE_BREAK_TARGET_FIRST = "TARGET_FIRST"
TIE_BREAK_AVERAGE = "AVERAGE_STOP_TARGET_RETURN"
TIE_BREAKS = (TIE_BREAK_STOP_FIRST, TIE_BREAK_TARGET_FIRST, TIE_BREAK_AVERAGE)


@dataclass(frozen=True)
class ExitRule:
    """D-6 — outcome horizon and exit rule, with an EXPLICIT stop+target
    same-candle tie-break.

    ``horizon_minutes`` caps the outcome observation window for every exit
    rule (also the MAE/MFE observation horizon, D-9).
    ``same_candle_tie_break`` decides a candle whose range touches BOTH the
    stop and the target (only possible under wick-path sampling).
    """

    exit_rule: str
    horizon_minutes: int
    same_candle_tie_break: str

    def __post_init__(self) -> None:
        if self.exit_rule not in EXIT_RULES:
            raise OutcomePolicyError(f"unknown exit rule {self.exit_rule!r}")
        if (
            isinstance(self.horizon_minutes, bool)
            or not isinstance(self.horizon_minutes, int)
            or self.horizon_minutes < 1
        ):
            raise OutcomePolicyError("horizon_minutes must be a positive integer")
        if self.same_candle_tie_break not in TIE_BREAKS:
            raise OutcomePolicyError(
                f"unknown same-candle tie-break {self.same_candle_tie_break!r}"
            )

    @property
    def horizon_us(self) -> int:
        return self.horizon_minutes * 60 * 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_rule": self.exit_rule,
            "horizon_minutes": self.horizon_minutes,
            "same_candle_tie_break": self.same_candle_tie_break,
        }


# ─── D-7: baseline definition ────────────────────────────────────────────────
BASELINE_ORIGINAL_SIGNAL = (
    "ORIGINAL_SIGNAL_INSTANT_ORIGINAL_PRICE_BASIS_SYMMETRIC_EXIT_MODEL"
)
BASELINE_MODES = (BASELINE_ORIGINAL_SIGNAL,)


@dataclass(frozen=True)
class BaselineSpec:
    """D-7 — the exact comparator cohort.

    The implemented (and only) mode enters at the ORIGINAL signal instant
    with the ORIGINAL price basis and evaluates under the SAME exit/cost/
    geometry conventions as the variant — symmetric pairing on the SAME
    source events.
    """

    mode: str

    def __post_init__(self) -> None:
        if self.mode not in BASELINE_MODES:
            raise OutcomePolicyError(f"unknown baseline mode {self.mode!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}


# ─── D-8: Net E[R] denominator ───────────────────────────────────────────────
DENOM_ALL_SOURCE = "ALL_SOURCE_EVENTS"
DENOM_ALL_CONFIRMED = "ALL_CONFIRMED_EVENTS"
DENOM_ALL_FUNDABLE = "ALL_FUNDABLE_EVENTS"
DENOMINATORS = (DENOM_ALL_SOURCE, DENOM_ALL_CONFIRMED, DENOM_ALL_FUNDABLE)


@dataclass(frozen=True)
class DenominatorSpec:
    """D-8 — the DECLARED denominator of Net E[R].

    ``ALL_SOURCE_EVENTS`` keeps every event (including ``MISSED_ENTRY``,
    ``ENTRY_EXPIRED``, ``NO_CONFIRMATION``, ``INCOMPLETE_COVERAGE``) in the
    denominator.  The narrower denominators are explicit declarations too;
    events outside them are NEVER dropped silently — they are always
    reported as ``excluded_by_declared_denominator`` counts.
    """

    definition: str

    def __post_init__(self) -> None:
        if self.definition not in DENOMINATORS:
            raise OutcomePolicyError(f"unknown denominator {self.definition!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"definition": self.definition}


# ─── D-9: MAE/MFE conventions ────────────────────────────────────────────────
PATH_CLOSE_ONLY = "CLOSE_ONLY_CLOSED_CANDLES_V1"
PATH_WICK = "WICK_PATH_INTRA_CANDLE_V1"
PATH_SAMPLINGS = (PATH_CLOSE_ONLY, PATH_WICK)

NORM_PCT_ENTRY = "PCT_ENTRY_PRICE"
NORM_R_STOP_DISTANCE = "R_MULTIPLE_OF_STOP_DISTANCE"
NORMALIZATIONS = (NORM_PCT_ENTRY, NORM_R_STOP_DISTANCE)


@dataclass(frozen=True)
class MaeMfeSpec:
    """D-9 — MAE/MFE path sampling and normalization, both explicit.

    The observation horizon is the D-6 ``horizon_minutes``.
    """

    path_sampling: str
    normalization: str

    def __post_init__(self) -> None:
        if self.path_sampling not in PATH_SAMPLINGS:
            raise OutcomePolicyError(f"unknown path sampling {self.path_sampling!r}")
        if self.normalization not in NORMALIZATIONS:
            raise OutcomePolicyError(f"unknown normalization {self.normalization!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_sampling": self.path_sampling,
            "normalization": self.normalization,
        }


# ─── D-10: missed winners ────────────────────────────────────────────────────
MISSED_WINNER_BASELINE_NET_R = "BASELINE_NET_R_GT_THRESHOLD"
MISSED_WINNER_DEFINITIONS = (MISSED_WINNER_BASELINE_NET_R,)
MISSED_WINNER_REPORT_COUNT_AND_LIST = "COUNT_AND_LIST_OUTCOME_KEYS"
MISSED_WINNER_REPORTINGS = (MISSED_WINNER_REPORT_COUNT_AND_LIST,)


@dataclass(frozen=True)
class MissedWinnerRule:
    """D-10 — definition and reporting rule for "missed winners".

    A missed winner is a variant leg that did NOT enter
    (``MISSED_ENTRY`` / ``ENTRY_EXPIRED`` / ``NO_CONFIRMATION``) while the
    baseline leg of the SAME source event produced a net return strictly
    above ``min_net_r`` under the same exit/cost model.
    """

    definition: str
    min_net_r: float
    reporting: str

    def __post_init__(self) -> None:
        if self.definition not in MISSED_WINNER_DEFINITIONS:
            raise OutcomePolicyError(
                f"unknown missed-winner definition {self.definition!r}"
            )
        if isinstance(self.min_net_r, bool) or not isinstance(
            self.min_net_r, (int, float)
        ):
            raise OutcomePolicyError("min_net_r must be a number")
        if self.min_net_r != self.min_net_r:
            raise OutcomePolicyError("min_net_r must be finite")
        if self.reporting not in MISSED_WINNER_REPORTINGS:
            raise OutcomePolicyError(
                f"unknown missed-winner reporting {self.reporting!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition,
            "min_net_r": float(self.min_net_r),
            "reporting": self.reporting,
        }


# ─── D-11: statistical acceptance rule ───────────────────────────────────────
@dataclass(frozen=True)
class StatisticalAcceptanceRule:
    """D-11 — the acceptance rule that must be DECLARED BEFORE any outcome
    is inspected.

    ``declared_before_outcomes_inspected`` is the caller's explicit
    attestation; the model additionally enforces ordering mechanically in
    :class:`app.research.me_entry_quality_h2_outcome_v1.aggregate.OutcomeSet`
    (a rule attached after outcomes were registered makes every aggregate
    refuse).
    """

    min_paired_n: int
    evaluation_cadence: str
    decision_rule: str
    declared_before_outcomes_inspected: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_paired_n, bool)
            or not isinstance(self.min_paired_n, int)
            or self.min_paired_n < 1
        ):
            raise OutcomePolicyError("min_paired_n must be a positive integer")
        for name in ("evaluation_cadence", "decision_rule"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OutcomePolicyError(f"{name} must be a non-empty string")
        if self.declared_before_outcomes_inspected is not True:
            raise OutcomePolicyError(
                "the statistical acceptance rule must be declared BEFORE "
                "outcomes are inspected (D-11)"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_paired_n": self.min_paired_n,
            "evaluation_cadence": self.evaluation_cadence,
            "decision_rule": self.decision_rule,
            "declared_before_outcomes_inspected": True,
        }


# ─── D-12: policy freeze record ──────────────────────────────────────────────
@dataclass(frozen=True)
class FreezeRecord:
    """D-12 — the immutable approval record of ONE policy fingerprint."""

    approved_fingerprint: str
    spec_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_fingerprint": self.approved_fingerprint,
            "spec_fingerprint": self.spec_fingerprint,
        }


# ─── the decision slot ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """One D-n decision: a value plus its three-state decision status."""

    value: Any = None
    status: str = DECISION_PENDING

    def __post_init__(self) -> None:
        if self.status not in DECISION_STATUSES:
            raise OutcomePolicyError(f"unknown decision status {self.status!r}")
        if self.status == DECISION_PENDING and self.value is not None:
            raise OutcomePolicyError(
                "a PENDING_OWNER_DECISION slot must not carry a value"
            )
        if self.status != DECISION_PENDING and self.value is None:
            raise OutcomePolicyError(
                "a specified decision slot must carry a value"
            )

    @classmethod
    def pending(cls) -> "Decision":
        return cls()

    @classmethod
    def preliminary(cls, value: Any) -> "Decision":
        return cls(value=value, status=DECISION_PRELIMINARY)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "value": jsonable(self.value)}


def _pending() -> Decision:
    return Decision.pending()


# ─── the twelve decision slot names (canonical order) ────────────────────────
SLOT_D1 = "d_1_entry_price_class"
SLOT_D2 = "d_2_fill_missed_entry_policy"
SLOT_D3 = "d_3_cost_model"
SLOT_D4 = "d_4_entry_deadline"
SLOT_D5 = "d_5_sl_tp_geometry"
SLOT_D6 = "d_6_exit_rule"
SLOT_D7 = "d_7_baseline_definition"
SLOT_D8 = "d_8_net_er_denominator"
SLOT_D9 = "d_9_mae_mfe_conventions"
SLOT_D10 = "d_10_missed_winner_rule"
SLOT_D11 = "d_11_statistical_acceptance_rule"
SLOT_D12 = "d_12_policy_freeze"

DECISION_SLOTS: tuple[str, ...] = (
    SLOT_D1, SLOT_D2, SLOT_D3, SLOT_D4, SLOT_D5, SLOT_D6,
    SLOT_D7, SLOT_D8, SLOT_D9, SLOT_D10, SLOT_D11, SLOT_D12,
)
#: slots that must be specified before ANY per-event outcome number exists
#: (D-10 is included because the paired outcome carries its missed-winner
#: classification; D-8/D-11 are aggregate-level and enforced there)
EVALUATION_REQUIRED_SLOTS: tuple[str, ...] = (
    SLOT_D1, SLOT_D2, SLOT_D3, SLOT_D4, SLOT_D5, SLOT_D6, SLOT_D7,
    SLOT_D9, SLOT_D10,
)
#: slots that must be specified (and the policy FROZEN) before any aggregate
AGGREGATE_REQUIRED_SLOTS: tuple[str, ...] = DECISION_SLOTS


@dataclass(frozen=True)
class OutcomePolicy:
    """The complete D-1..D-12 outcome policy (issue #298 G5 / #312 §3).

    Defaults: EVERY slot is ``PENDING_OWNER_DECISION``.  A slot becomes
    ``PRELIMINARY_CANDIDATE_NOT_FROZEN`` only through :meth:`with_decision`,
    and the whole policy becomes ``FROZEN`` only through :meth:`freeze`
    with the exact approved fingerprint.
    """

    d_1_entry_price_class: Decision = field(default_factory=_pending)
    d_2_fill_missed_entry_policy: Decision = field(default_factory=_pending)
    d_3_cost_model: Decision = field(default_factory=_pending)
    d_4_entry_deadline: Decision = field(default_factory=_pending)
    d_5_sl_tp_geometry: Decision = field(default_factory=_pending)
    d_6_exit_rule: Decision = field(default_factory=_pending)
    d_7_baseline_definition: Decision = field(default_factory=_pending)
    d_8_net_er_denominator: Decision = field(default_factory=_pending)
    d_9_mae_mfe_conventions: Decision = field(default_factory=_pending)
    d_10_missed_winner_rule: Decision = field(default_factory=_pending)
    d_11_statistical_acceptance_rule: Decision = field(default_factory=_pending)
    d_12_policy_freeze: Decision = field(default_factory=_pending)

    # ── introspection ────────────────────────────────────────────────────────
    def slots(self) -> dict[str, Decision]:
        return {name: getattr(self, name) for name in DECISION_SLOTS}

    def missing_slots(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, decision in self.slots().items()
            if decision.status == DECISION_PENDING
        )

    def missing_spec_slots(self) -> tuple[str, ...]:
        """Pending slots among the D-1..D-11 specification decisions
        (the D-12 freeze record is what :meth:`freeze` produces and is
        therefore not a precondition of freezing)."""
        return tuple(
            name
            for name in DECISION_SLOTS
            if name != SLOT_D12
            and getattr(self, name).status == DECISION_PENDING
        )

    def decision(self, slot: str) -> Any:
        if slot not in DECISION_SLOTS:
            raise OutcomePolicyError(f"unknown decision slot {slot!r}")
        decision: Decision = getattr(self, slot)
        return decision.value

    @property
    def policy_status(self) -> str:
        spec_statuses = [
            getattr(self, name).status
            for name in DECISION_SLOTS
            if name != SLOT_D12
        ]
        if DECISION_PENDING in spec_statuses:
            return DECISION_PENDING
        freeze_status = getattr(self, SLOT_D12).status
        if freeze_status == DECISION_FROZEN and all(
            status == DECISION_FROZEN for status in spec_statuses
        ):
            return DECISION_FROZEN
        return DECISION_PRELIMINARY

    def is_ready_for_aggregates(self) -> bool:
        return self.policy_status == DECISION_FROZEN

    def missing_for_evaluation(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in EVALUATION_REQUIRED_SLOTS
            if getattr(self, name).status == DECISION_PENDING
        )

    # ── construction helpers ─────────────────────────────────────────────────
    def with_decision(self, slot: str, decision: Decision) -> "OutcomePolicy":
        """Return a copy with ONE decision slot replaced."""
        if slot not in DECISION_SLOTS:
            raise OutcomePolicyError(f"unknown decision slot {slot!r}")
        if not isinstance(decision, Decision):
            raise OutcomePolicyError("decision must be a Decision instance")
        if self.policy_status == DECISION_FROZEN:
            raise OutcomePolicyError("a FROZEN policy is immutable")
        if slot == SLOT_D12 and decision.status == DECISION_FROZEN:
            raise OutcomePolicyError(
                "FROZEN is only reachable through OutcomePolicy.freeze()"
            )
        return OutcomePolicy(**{
            **self.slots(),  # type: ignore[arg-type]
            slot: decision,
        })

    def with_preliminary(self, slot: str, value: Any) -> "OutcomePolicy":
        return self.with_decision(slot, Decision.preliminary(value))

    # ── fingerprints ─────────────────────────────────────────────────────────
    def _spec_payload(self) -> dict[str, Any]:
        return {
            name: jsonable(getattr(self, name).value)
            for name in DECISION_SLOTS
            if name != SLOT_D12
        }

    def spec_fingerprint(self) -> str:
        """SHA-256 over the D-1..D-11 decision VALUES (the freeze subject)."""
        return _sha256(self._spec_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_policy_version": OUTCOME_POLICY_VERSION,
            "policy_status": self.policy_status,
            "decisions": {name: decision.to_dict()
                          for name, decision in self.slots().items()},
            "spec_fingerprint": self.spec_fingerprint(),
        }

    def policy_fingerprint(self) -> str:
        """SHA-256 over the FULL policy: every value, every status and the
        freeze record.  This is the fingerprint stamped on every outcome."""
        return _sha256(self.to_dict())

    # ── D-12: the only path to FROZEN ────────────────────────────────────────
    def freeze(self, approved_fingerprint: str) -> "OutcomePolicy":
        """Return the FROZEN policy — only with an EXPLICIT approved
        fingerprint equal to :meth:`spec_fingerprint` and only when every
        decision is already specified.

        ``approved_fingerprint`` is the fingerprint the owner (or the
        authorized review) approved; nothing else can unlock FROZEN.
        """
        if not isinstance(approved_fingerprint, str) or not approved_fingerprint:
            raise OutcomePolicyError(
                "an explicit approved fingerprint is required to freeze"
            )
        pending = self.missing_spec_slots()
        if pending:
            raise OutcomePolicyError(
                "cannot freeze a policy with pending decisions: "
                + ", ".join(pending)
            )
        expected = self.spec_fingerprint()
        if approved_fingerprint != expected:
            raise OutcomePolicyError(
                "approved fingerprint does not match this policy's "
                "spec fingerprint"
            )
        frozen_slots: dict[str, Decision] = {}
        for name in DECISION_SLOTS:
            if name == SLOT_D12:
                continue
            current = getattr(self, name)
            frozen_slots[name] = Decision(
                value=current.value, status=DECISION_FROZEN
            )
        frozen_slots[SLOT_D12] = Decision(
            value=FreezeRecord(
                approved_fingerprint=approved_fingerprint,
                spec_fingerprint=expected,
            ),
            status=DECISION_FROZEN,
        )
        return OutcomePolicy(**frozen_slots)  # type: ignore[arg-type]

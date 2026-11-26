"""Issue #298 / #312 — paired baseline-vs-variant outcome model (D-7, D-10).

Research-only, pure: no I/O, no network, no database, no clock reads.

For the SAME source events the model evaluates, under the SAME execution
model (D-1..D-6, D-9 cost/exit/geometry conventions):

* **baseline** — entered at the ORIGINAL signal instant with the ORIGINAL
  price basis (:data:`BASELINE_ENTRY_PRICE_LABEL`);
* **variant** — the delayed confirmation entry at an admissible executable
  price class strictly AFTER the confirmation instant.

The confirmation-candle close is NEVER executable in either leg.

Refusal contract: if any decision required for per-event evaluation
(D-1..D-7, D-9, D-10) is still ``PENDING_OWNER_DECISION``,
:func:`evaluate_pair` returns a refusal record with status
``NOT_READY_PENDING_DECISION`` and NO numbers — mirroring the H2 engine's
``UNDETERMINED_NO_EXECUTION_MODEL`` honesty.

Idempotency: every result is stamped with
(``source_event_key``, ``variant_id``, ``policy_fingerprint``) — see
:attr:`EventPairOutcome.outcome_key` and the
:mod:`aggregate` registry.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h2_outcome_v1.entry_model import resolve_entry
from app.research.me_entry_quality_h2_outcome_v1.exit_model import (
    GeometryError,
    build_geometry,
    invalid_leg,
    non_entry_leg,
    simulate_leg,
)
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    MISSED_WINNER_BASELINE_NET_R,
    OutcomePolicy,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    CandleBar,
    ConfirmationInfo,
    ENTRY_EXPIRED,
    ENTRY_MISSED,
    ENTRY_NO_CONFIRMATION,
    EventPairOutcome,
    LegOutcome,
    RESULT_STATUS_EVALUATED,
    SourceEventInput,
    TradePrint,
    closed_candles,
    not_ready_pair,
)

PAIRED_MODEL_VERSION = "H2_OUTCOME_PAIRED_MODEL_1.0.0"

#: the baseline leg's executable entry label (original instant + original
#: price basis — D-7)
BASELINE_ENTRY_PRICE_LABEL = (
    "EXECUTABLE_AT_ORIGINAL_SIGNAL_INSTANT_PRICE_BASIS_V1"
)
BASELINE_LEG_NAME = "BASELINE_ORIGINAL_SIGNAL"
VARIANT_LEG_NAME = "VARIANT_DELAYED_CONFIRMATION"

#: variant-leg states that count as "did not enter" for D-10
MISSED_WINNER_ELIGIBLE_STATUSES = (
    ENTRY_MISSED,
    ENTRY_EXPIRED,
    ENTRY_NO_CONFIRMATION,
)

REASON_OK = "OK"
REASON_POLICY_NOT_READY = "POLICY_DECISIONS_PENDING"
REASON_INVALID_ASOF = "INVALID_EVALUATION_ASOF"
REASON_SIGNAL_NOT_BEFORE_ASOF = "SIGNAL_TIME_NOT_BEFORE_ASOF"
REASON_NO_ORIGINAL_PRICE = "MISSING_ORIGINAL_PRICE_BASIS"
REASON_GEOMETRY = "GEOMETRY_ERROR"
REASON_VARIANT_NO_CONFIRMATION = "VARIANT_NO_CONFIRMED_DELAYED_ENTRY"


def _baseline_leg(
    event: SourceEventInput,
    policy: OutcomePolicy,
    closed: Sequence[CandleBar],
    evaluation_asof_us: int,
) -> LegOutcome:
    """Baseline: entered at the ORIGINAL signal instant at the ORIGINAL
    price basis, evaluated under the SAME exit/cost/geometry model."""
    entry_price = event.original_price
    if entry_price is None:
        return invalid_leg(
            leg_name=BASELINE_LEG_NAME, reason=REASON_NO_ORIGINAL_PRICE
        )
    geometry = build_geometry(
        direction=event.direction,
        entry_price=float(entry_price),
        original_price=event.original_price,
        source_stop_price=event.source_stop_price,
        source_target_price=event.source_target_price,
        geometry_policy=policy.decision("d_5_sl_tp_geometry"),
    )
    synthetic_entry = _BaselineEntry(
        entry_time_us=event.signal_time_us,
        entry_price=float(entry_price),
    )
    return simulate_leg(
        event=event,
        leg_name=BASELINE_LEG_NAME,
        entry=synthetic_entry,
        geometry=geometry,
        closed=closed,
        policy=policy,
        evaluation_asof_us=evaluation_asof_us,
    )


class _BaselineEntry:
    """Minimal EntryResolution-compatible carrier for the baseline leg."""

    def __init__(self, *, entry_time_us: int, entry_price: float) -> None:
        self.status = "ENTERED"
        self.reason = REASON_OK
        self.entry_time_us = entry_time_us
        self.entry_price = entry_price
        self.entry_price_label = BASELINE_ENTRY_PRICE_LABEL
        self.price_class = "ORIGINAL_SIGNAL_INSTANT"
        self.fill_at_bar_open = False  # signal instant is intra-candle

    @property
    def entered(self) -> bool:
        return True


def evaluate_pair(
    *,
    event: SourceEventInput | Mapping[str, Any],
    confirmation: ConfirmationInfo | Mapping[str, Any] | None,
    candles: Sequence[CandleBar | Mapping[str, Any]],
    policy: OutcomePolicy,
    evaluation_asof_us: int,
    trade_prints: Sequence[TradePrint | Mapping[str, Any]] = (),
) -> EventPairOutcome:
    """Evaluate the baseline/variant pair for ONE source event.

    Deterministic, prefix-stable and causal: only candles fully closed at
    ``evaluation_asof_us`` participate and the whole result is a pure
    function of its arguments.
    """
    if not isinstance(event, SourceEventInput):
        event = SourceEventInput.from_mapping(event)
    if isinstance(confirmation, Mapping):
        confirmation = ConfirmationInfo.from_mapping(confirmation)
    prints = tuple(
        print_ if isinstance(print_, TradePrint) else TradePrint.from_mapping(print_)
        for print_ in trade_prints
    )

    fingerprint = policy.policy_fingerprint()
    snapshot = {
        name: decision.to_dict() for name, decision in policy.slots().items()
    }

    missing = policy.missing_for_evaluation()
    if missing:
        return not_ready_pair(
            event,
            policy_fingerprint=fingerprint,
            policy_status=policy.policy_status,
            evaluation_asof_us=evaluation_asof_us,
            reason=REASON_POLICY_NOT_READY + ": " + ", ".join(missing),
            decision_snapshot=snapshot,
        )
    if isinstance(evaluation_asof_us, bool) or not isinstance(
        evaluation_asof_us, int
    ):
        return not_ready_pair(
            event,
            policy_fingerprint=fingerprint,
            policy_status=policy.policy_status,
            evaluation_asof_us=0,
            reason=REASON_INVALID_ASOF,
            decision_snapshot=snapshot,
        )
    if event.signal_time_us >= evaluation_asof_us:
        return not_ready_pair(
            event,
            policy_fingerprint=fingerprint,
            policy_status=policy.policy_status,
            evaluation_asof_us=evaluation_asof_us,
            reason=REASON_SIGNAL_NOT_BEFORE_ASOF,
            decision_snapshot=snapshot,
        )

    closed = closed_candles(candles, evaluation_asof_us)

    # ── baseline leg (original instant, original price basis) ───────────────
    baseline = _baseline_leg(event, policy, closed, evaluation_asof_us)

    # ── variant leg (delayed confirmation entry) ────────────────────────────
    confirmed = (
        confirmation is not None
        and confirmation.state == "CONFIRMED_DELAYED"
        and confirmation.confirmation_time_us is not None
    )
    if not confirmed:
        variant = non_entry_leg(
            leg_name=VARIANT_LEG_NAME,
            entry_status=ENTRY_NO_CONFIRMATION,
            entry_reason=REASON_VARIANT_NO_CONFIRMATION,
            policy=policy,
        )
    else:
        try:
            resolution = resolve_entry(
                confirmation=confirmation,
                closed=closed,
                entry_spec=policy.decision("d_1_entry_price_class"),
                fill_policy=policy.decision("d_2_fill_missed_entry_policy"),
                deadline=policy.decision("d_4_entry_deadline"),
                evaluation_asof_us=evaluation_asof_us,
                direction=event.direction,
                trade_prints=prints,
            )
        except ValueError:
            return not_ready_pair(
                event,
                policy_fingerprint=fingerprint,
                policy_status=policy.policy_status,
                evaluation_asof_us=evaluation_asof_us,
                reason="ENTRY_MODEL_INPUT_INVALID",
                decision_snapshot=snapshot,
            )
        if not resolution.entered:
            variant = non_entry_leg(
                leg_name=VARIANT_LEG_NAME,
                entry_status=resolution.status,
                entry_reason=resolution.reason,
                policy=policy,
            )
        else:
            try:
                geometry = build_geometry(
                    direction=event.direction,
                    entry_price=float(resolution.entry_price),
                    original_price=event.original_price,
                    source_stop_price=event.source_stop_price,
                    source_target_price=event.source_target_price,
                    geometry_policy=policy.decision("d_5_sl_tp_geometry"),
                )
            except GeometryError as exc:
                variant = invalid_leg(
                    leg_name=VARIANT_LEG_NAME,
                    reason=REASON_GEOMETRY + ":" + exc.reason_code,
                )
            else:
                variant = simulate_leg(
                    event=event,
                    leg_name=VARIANT_LEG_NAME,
                    entry=resolution,
                    geometry=geometry,
                    closed=closed,
                    policy=policy,
                    evaluation_asof_us=evaluation_asof_us,
                )

    # ── D-10: missed-winner classification (rule is a declared decision) ────
    rule = policy.decision("d_10_missed_winner_rule")
    missed_winner = False
    missed_winner_reason = ""
    if rule.definition == MISSED_WINNER_BASELINE_NET_R:
        if (
            variant.entry_status in MISSED_WINNER_ELIGIBLE_STATUSES
            and baseline.net_return_r is not None
            and baseline.net_return_r > float(rule.min_net_r)
        ):
            missed_winner = True
            missed_winner_reason = (
                "VARIANT_DID_NOT_ENTER__BASELINE_NET_R_GT_THRESHOLD"
            )
        elif variant.entry_status in MISSED_WINNER_ELIGIBLE_STATUSES:
            missed_winner_reason = "VARIANT_DID_NOT_ENTER__BASELINE_NOT_A_WINNER"

    return EventPairOutcome(
        source_event_key=event.source_event_key,
        variant_id=event.variant_id,
        policy_fingerprint=fingerprint,
        policy_status=policy.policy_status,
        evaluation_asof_us=evaluation_asof_us,
        status=RESULT_STATUS_EVALUATED,
        reason=REASON_OK,
        baseline=baseline,
        variant=variant,
        missed_winner=missed_winner,
        missed_winner_reason=missed_winner_reason,
        decision_snapshot=snapshot,
    )

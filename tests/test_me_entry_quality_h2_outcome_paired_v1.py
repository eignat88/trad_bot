"""Issue #298 / #312 — paired baseline/variant outcome tests (D-5..D-10).

Offline, pure: synthetic closed-candle data only.  Covers symmetric
baseline-vs-variant pairing (D-7), the three explicit SL/TP geometry modes
(D-5), exit rules and the documented stop+target same-candle tie-break
(D-6), MAE/MFE close-only vs wick conventions (D-9), cost hooks (D-3),
missed-winner accounting (D-10) and the refusal of per-event numbers while
decisions are pending.
"""
from __future__ import annotations

import pytest

from app.research.me_entry_quality_h2_outcome_v1.costs import CostModel
from app.research.me_entry_quality_h2_outcome_v1.exit_model import EXIT_TIE_AVERAGE
from app.research.me_entry_quality_h2_outcome_v1.paired import (
    BASELINE_ENTRY_PRICE_LABEL,
    BASELINE_LEG_NAME,
    VARIANT_LEG_NAME,
    evaluate_pair,
)
from app.research.me_entry_quality_h2_outcome_v1.policy import (
    BASELINE_ORIGINAL_SIGNAL,
    DECISION_PENDING,
    DENOM_ALL_SOURCE,
    ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN,
    EXIT_RULE_HORIZON_ONLY,
    EXIT_RULE_STOP_OR_TARGET,
    EXIT_RULE_STOP_TARGET_OR_HORIZON,
    GAP_THROUGH_FILL_AT_OPEN,
    GEOMETRY_RE_DERIVED,
    GEOMETRY_REBASED_AT_ENTRY,
    GEOMETRY_SOURCE_VERBATIM,
    MISSED_STAT_ZERO_NET,
    MISSED_WINNER_BASELINE_NET_R,
    MISSED_WINNER_REPORT_COUNT_AND_LIST,
    NORM_R_STOP_DISTANCE,
    PATH_CLOSE_ONLY,
    PATH_WICK,
    RESULT_STATUS_EVALUATED,
    RESULT_STATUS_NOT_READY_PENDING_DECISION,
    TIE_BREAK_AVERAGE,
    TIE_BREAK_STOP_FIRST,
    TIE_BREAK_TARGET_FIRST,
    BaselineSpec,
    DenominatorSpec,
    EntryDeadline,
    EntrySpec,
    ExitRule,
    FillPolicy,
    MaeMfeSpec,
    MissedWinnerRule,
    OutcomePolicy,
    SlTpGeometryPolicy,
    StatisticalAcceptanceRule,
)
from app.research.me_entry_quality_h2_outcome_v1.types import (
    CONFIRMATION_CLOSE_REFERENCE_LABEL,
    ENTRY_ENTERED,
    ENTRY_INVALID,
    ENTRY_NO_CONFIRMATION,
    EXIT_HORIZON,
    EXIT_NONE,
    EXIT_STOP,
    EXIT_TARGET,
    CandleBar,
    ConfirmationInfo,
    SourceEventInput,
)

BASE_MS = 1_700_000_100_000
BAR_MS = 300_000
SIGNAL_US = (BASE_MS + 100_000) * 1_000
CONFIRM_US = (BASE_MS + BAR_MS) * 1_000
ASOF_US = (BASE_MS + 9 * BAR_MS) * 1_000

EVENT = SourceEventInput(
    source_event_key="src-1",
    variant_id="H2_RSI_TURN_1",
    symbol="BTCUSDT",
    direction="SHORT",
    signal_time_us=SIGNAL_US,
    original_price=100.0,
    source_stop_price=101.0,
    source_target_price=98.0,
    lineage_hash="lineage-1",
)

CONFIRMATION = ConfirmationInfo(
    state="CONFIRMED_DELAYED",
    confirmation_time_us=CONFIRM_US,
    confirmation_candle_open_ms=BASE_MS,
    confirmation_close_reference=100.2,
)


def bar(index: int, o: float, h: float, low: float, c: float, v: float = 1.0):
    return CandleBar(
        open_time_ms=BASE_MS + index * BAR_MS,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


DEFAULT_CANDLES = [
    bar(0, 100.0, 100.5, 99.8, 100.2),
    bar(1, 100.3, 100.6, 100.1, 100.4),
    bar(2, 100.4, 100.45, 99.5, 99.7),
    bar(3, 99.7, 99.9, 98.2, 98.5),
    bar(4, 98.5, 98.8, 97.9, 98.1),
    bar(5, 98.1, 98.4, 97.8, 98.0),
    bar(6, 98.0, 98.2, 97.7, 97.9),
    bar(7, 97.9, 98.1, 97.6, 97.8),
    bar(8, 97.8, 98.0, 97.5, 97.7),
]


def make_cost(
    fee: float = 5.0,
    slip: float = 2.0,
    funding_bps: float = 0.0,
    funding_model: str = "FUNDING_NONE_EXPLICIT_ZERO",
) -> CostModel:
    return CostModel(
        fee_model="FEE_FIXED_BPS_PER_SIDE",
        fee_bps_per_side=fee,
        fee_data_source="TEST_FEE_SCHEDULE",
        slippage_model="SLIPPAGE_FIXED_BPS_PER_SIDE",
        slippage_bps_per_side=slip,
        slippage_data_source="TEST_SLIPPAGE_ASSUMPTION",
        funding_model=funding_model,
        funding_bps_per_interval=funding_bps,
        funding_interval_ms=28_800_000,
        funding_data_source="TEST_FUNDING_ASSUMPTION",
    )


def make_policy(
    *,
    geometry: SlTpGeometryPolicy | None = None,
    exit_rule: str = EXIT_RULE_STOP_TARGET_OR_HORIZON,
    tie_break: str = TIE_BREAK_STOP_FIRST,
    horizon_minutes: int = 240,
    path: str = PATH_WICK,
    norm: str = NORM_R_STOP_DISTANCE,
    cost: CostModel | None = None,
    missed_stat: str = MISSED_STAT_ZERO_NET,
    denominator: str = DENOM_ALL_SOURCE,
) -> OutcomePolicy:
    policy = OutcomePolicy()
    policy = policy.with_preliminary(
        "d_1_entry_price_class",
        EntrySpec(price_class=ENTRY_PRICE_CLASS_NEXT_CANDLE_OPEN),
    )
    policy = policy.with_preliminary(
        "d_2_fill_missed_entry_policy",
        FillPolicy(
            gap_through_policy=GAP_THROUGH_FILL_AT_OPEN,
            missed_entry_stat_policy=missed_stat,
        ),
    )
    policy = policy.with_preliminary("d_3_cost_model", cost or make_cost())
    policy = policy.with_preliminary(
        "d_4_entry_deadline", EntryDeadline(max_delay_ms=30 * 60 * 1000)
    )
    policy = policy.with_preliminary(
        "d_5_sl_tp_geometry",
        geometry or SlTpGeometryPolicy(mode=GEOMETRY_REBASED_AT_ENTRY),
    )
    policy = policy.with_preliminary(
        "d_6_exit_rule",
        ExitRule(
            exit_rule=exit_rule,
            horizon_minutes=horizon_minutes,
            same_candle_tie_break=tie_break,
        ),
    )
    policy = policy.with_preliminary(
        "d_7_baseline_definition", BaselineSpec(mode=BASELINE_ORIGINAL_SIGNAL)
    )
    policy = policy.with_preliminary(
        "d_8_net_er_denominator", DenominatorSpec(definition=denominator)
    )
    policy = policy.with_preliminary(
        "d_9_mae_mfe_conventions",
        MaeMfeSpec(path_sampling=path, normalization=norm),
    )
    policy = policy.with_preliminary(
        "d_10_missed_winner_rule",
        MissedWinnerRule(
            definition=MISSED_WINNER_BASELINE_NET_R,
            min_net_r=0.0,
            reporting=MISSED_WINNER_REPORT_COUNT_AND_LIST,
        ),
    )
    policy = policy.with_preliminary(
        "d_11_statistical_acceptance_rule",
        StatisticalAcceptanceRule(
            min_paired_n=10,
            evaluation_cadence="WEEKLY_RESEARCH_REVIEW",
            decision_rule="PAIRED_MEAN_NET_R_DIFFERENCE_VS_ZERO",
            declared_before_outcomes_inspected=True,
        ),
    )
    return policy


def evaluate(candles=None, policy=None, confirmation=CONFIRMATION, **kwargs):
    return evaluate_pair(
        event=EVENT,
        confirmation=confirmation,
        candles=DEFAULT_CANDLES if candles is None else candles,
        policy=policy or make_policy(),
        evaluation_asof_us=kwargs.pop("evaluation_asof_us", ASOF_US),
        **kwargs,
    )


class TestSymmetricPairing:
    def test_both_legs_entered_on_the_same_source_event(self):
        outcome = evaluate()
        assert outcome.status == RESULT_STATUS_EVALUATED
        assert outcome.baseline.leg_name == BASELINE_LEG_NAME
        assert outcome.variant.leg_name == VARIANT_LEG_NAME
        # baseline: ORIGINAL instant + ORIGINAL price basis
        assert outcome.baseline.entry_time_us == SIGNAL_US
        assert outcome.baseline.entry_price == 100.0
        assert outcome.baseline.entry_price_label == (
            BASELINE_ENTRY_PRICE_LABEL
        )
        # variant: delayed entry at the first admissible open
        assert outcome.variant.entry_status == ENTRY_ENTERED
        assert outcome.variant.entry_price == 100.3
        assert outcome.variant.entry_time_us == CONFIRM_US

    def test_same_execution_model_for_both_legs(self):
        outcome = evaluate()
        assert (
            outcome.baseline.cost_breakdown["cost_convention"]
            == outcome.variant.cost_breakdown["cost_convention"]
        )
        assert (
            outcome.baseline.mae_mfe_path_sampling
            == outcome.variant.mae_mfe_path_sampling
        )
        assert (
            outcome.baseline.mae_mfe_normalization
            == outcome.variant.mae_mfe_normalization
        )
        # REBASED geometry: identical distances around each leg's entry
        assert (
            outcome.baseline.cost_breakdown["geometry"]["stop_distance"]
            == outcome.variant.cost_breakdown["geometry"]["stop_distance"]
        )
        assert (
            outcome.baseline.cost_breakdown["geometry"]["target_distance"]
            == outcome.variant.cost_breakdown["geometry"]["target_distance"]
        )

    def test_confirmation_close_reference_is_labelled_not_executable(self):
        outcome = evaluate()
        assert outcome.to_dict()["confirmation_close_reference_label"] == (
            CONFIRMATION_CLOSE_REFERENCE_LABEL
        )
        assert outcome.variant.entry_price != 100.2

    def test_outcome_key_is_the_idempotency_key(self):
        outcome = evaluate()
        assert outcome.outcome_key == (
            "src-1",
            "H2_RSI_TURN_1",
            outcome.policy_fingerprint,
        )


class TestGeometryModes:
    def test_source_verbatim_shares_absolute_levels(self):
        policy = make_policy(
            geometry=SlTpGeometryPolicy(mode=GEOMETRY_SOURCE_VERBATIM)
        )
        outcome = evaluate(policy=policy)
        assert (
            outcome.baseline.cost_breakdown["geometry"]["stop_price"] == 101.0
        )
        assert (
            outcome.variant.cost_breakdown["geometry"]["stop_price"] == 101.0
        )
        # variant entry 100.3 -> distances differ from the baseline's
        assert outcome.variant.cost_breakdown["geometry"]["stop_distance"] == (
            pytest.approx(0.7)
        )
        # target 98.0 is touched by the variant at bar 4 (low 97.9)
        assert outcome.variant.exit_status == EXIT_TARGET
        assert outcome.variant.exit_price == 98.0

    def test_re_derived_uses_explicit_policy_distances(self):
        policy = make_policy(
            geometry=SlTpGeometryPolicy(
                mode=GEOMETRY_RE_DERIVED,
                stop_distance=0.5,
                target_distance=1.0,
            )
        )
        outcome = evaluate(policy=policy)
        assert (
            outcome.variant.cost_breakdown["geometry"]["stop_price"] == 100.8
        )
        assert (
            outcome.variant.cost_breakdown["geometry"]["target_price"] == 99.3
        )

    def test_verbatim_geometry_fails_closed_when_stop_not_beyond_entry(self):
        event = SourceEventInput(
            source_event_key="src-1",
            variant_id="H2_RSI_TURN_1",
            symbol="BTCUSDT",
            direction="SHORT",
            signal_time_us=SIGNAL_US,
            original_price=100.0,
            source_stop_price=100.1,
            source_target_price=98.0,
        )
        policy = make_policy(
            geometry=SlTpGeometryPolicy(mode=GEOMETRY_SOURCE_VERBATIM)
        )
        outcome = evaluate_pair(
            event=event,
            confirmation=CONFIRMATION,
            candles=DEFAULT_CANDLES,
            policy=policy,
            evaluation_asof_us=ASOF_US,
        )
        # the baseline entry 100.0 is still valid (stop 100.1 beyond it) ...
        assert outcome.baseline.entry_status == ENTRY_ENTERED
        # ... but the variant entry at 100.3 is beyond the verbatim stop:
        # fail closed, never a fabricated number
        assert outcome.variant.entry_status == ENTRY_INVALID
        assert outcome.variant.net_return_r is None


class TestExitsAndTieBreak:
    def test_variant_exits_at_rebased_target(self):
        outcome = evaluate()
        assert outcome.variant.exit_status == EXIT_TARGET
        assert outcome.variant.exit_price == 98.3  # 100.3 - 2.0
        assert outcome.variant.gross_return_r == pytest.approx(2.0)

    def test_baseline_exits_at_its_own_rebased_target(self):
        outcome = evaluate()
        assert outcome.baseline.exit_status == EXIT_TARGET
        assert outcome.baseline.exit_price == 98.0  # 100.0 - 2.0

    def test_exit_at_stop_when_price_rallies(self):
        candles = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(2, 100.4, 101.5, 100.3, 101.2),
        ]
        outcome = evaluate(candles=candles)
        assert outcome.variant.exit_status == EXIT_STOP
        assert outcome.variant.exit_price == 101.3  # 100.3 + 1.0
        assert outcome.variant.gross_return_r == pytest.approx(-1.0)

    def _tie_candles(self):
        return [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.6, 100.1, 100.4),
            bar(2, 100.4, 101.5, 98.2, 99.0),  # touches stop AND target
        ]

    def test_same_candle_tie_break_stop_first(self):
        outcome = evaluate(
            candles=self._tie_candles(), policy=make_policy(
                tie_break=TIE_BREAK_STOP_FIRST
            )
        )
        assert outcome.variant.exit_status == EXIT_STOP
        assert outcome.variant.exit_price == 101.3

    def test_same_candle_tie_break_target_first(self):
        outcome = evaluate(
            candles=self._tie_candles(), policy=make_policy(
                tie_break=TIE_BREAK_TARGET_FIRST
            )
        )
        assert outcome.variant.exit_status == EXIT_TARGET
        assert outcome.variant.exit_price == 98.3

    def test_same_candle_tie_break_average(self):
        outcome = evaluate(
            candles=self._tie_candles(), policy=make_policy(
                tie_break=TIE_BREAK_AVERAGE
            )
        )
        assert outcome.variant.exit_status == EXIT_TIE_AVERAGE
        assert outcome.variant.exit_price == pytest.approx(99.8)

    def test_fixed_horizon_only_ignores_touches_and_exits_at_horizon_close(self):
        outcome = evaluate(
            policy=make_policy(
                exit_rule=EXIT_RULE_HORIZON_ONLY, horizon_minutes=20
            )
        )
        assert outcome.variant.exit_status == EXIT_HORIZON
        assert outcome.variant.exit_price == 98.1  # close of the last bar

    def test_stop_or_target_only_without_touch_is_unresolved_not_invented(self):
        flat = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 100.3, 100.45, 100.15, 100.4),
            bar(2, 100.4, 100.45, 100.2, 100.35),
            bar(3, 100.35, 100.4, 100.2, 100.3),
            bar(4, 100.3, 100.35, 100.2, 100.25),
        ]
        outcome = evaluate(
            candles=flat,
            policy=make_policy(
                exit_rule=EXIT_RULE_STOP_OR_TARGET, horizon_minutes=20
            ),
        )
        assert outcome.variant.exit_status == EXIT_NONE
        assert outcome.variant.gross_return_r is None
        assert outcome.variant.net_return_r is None


class TestMaeMfeAndCosts:
    def test_wick_and_close_only_conventions_differ_and_are_labelled(self):
        wick = evaluate(policy=make_policy(path=PATH_WICK))
        close = evaluate(policy=make_policy(path=PATH_CLOSE_ONLY))
        assert wick.variant.mae_mfe_path_sampling == PATH_WICK
        assert close.variant.mae_mfe_path_sampling == PATH_CLOSE_ONLY
        # wick MAE sees 100.6; close-only MAE sees only 100.4
        assert wick.variant.mae_r == pytest.approx(0.3)
        assert close.variant.mae_r == pytest.approx(0.1)
        # wick MFE sees the 98.2 wick; close-only MFE follows the closes
        # (its target cross happens one bar later, at the 98.1 close)
        assert wick.variant.mfe_r == pytest.approx(2.1)
        assert close.variant.mfe_r == pytest.approx(2.2)
        assert close.variant.exit_time_us > wick.variant.exit_time_us

    def test_normalization_is_labelled(self):
        outcome = evaluate()
        assert outcome.variant.mae_mfe_normalization == NORM_R_STOP_DISTANCE
        assert outcome.variant.mae_r == pytest.approx(
            outcome.variant.mae_pct * 100.3
        )

    def test_costs_are_applied_with_data_source_labels(self):
        outcome = evaluate()
        breakdown = outcome.variant.cost_breakdown
        assert breakdown["fee_data_source"] == "TEST_FEE_SCHEDULE"
        assert breakdown["slippage_data_source"] == "TEST_SLIPPAGE_ASSUMPTION"
        assert breakdown["funding_data_source"] == "TEST_FUNDING_ASSUMPTION"
        assert breakdown["fee_cost_pct"] == pytest.approx(0.001)
        assert breakdown["slippage_cost_pct"] == pytest.approx(0.0004)
        assert breakdown["funding_cost_pct"] == 0.0
        expected_net_r = 2.0 - 0.0014 * 100.3 / 1.0
        assert outcome.variant.net_return_r == pytest.approx(expected_net_r)
        assert outcome.variant.net_return_pct == pytest.approx(
            2.0 / 100.3 - 0.0014
        )

    def test_funding_accrues_over_the_held_interval(self):
        cost = make_cost(
            funding_bps=8.0,
            funding_model="FUNDING_FIXED_BPS_PER_INTERVAL_ACCRUAL",
        )
        outcome = evaluate(policy=make_policy(cost=cost))
        breakdown = outcome.variant.cost_breakdown
        # held 10 minutes of one 8h interval
        assert breakdown["funding_cost_pct"] == pytest.approx(
            8.0 / 10_000.0 * (600_000 / 28_800_000)
        )
        assert breakdown["held_ms"] == 600_000


class TestMissedWinnersAndNoConfirmation:
    def test_no_confirmation_variant_is_paired_and_flagged(self):
        confirmation = ConfirmationInfo(
            state="EXPIRED", confirmation_time_us=None
        )
        outcome = evaluate(confirmation=confirmation)
        assert outcome.variant.entry_status == ENTRY_NO_CONFIRMATION
        assert outcome.variant.net_return_r is None
        assert outcome.baseline.entered

    def test_missed_winner_when_baseline_would_have_won(self):
        confirmation = ConfirmationInfo(
            state="EXPIRED", confirmation_time_us=None
        )
        outcome = evaluate(confirmation=confirmation)
        assert outcome.baseline.net_return_r > 0
        assert outcome.missed_winner is True
        assert outcome.missed_winner_reason != ""

    def test_no_missed_winner_when_baseline_loses(self):
        candles = [
            bar(0, 100.0, 100.5, 99.8, 100.2),
            bar(1, 101.0, 101.6, 100.9, 101.4),
            bar(2, 101.4, 101.8, 101.2, 101.5),
            bar(3, 101.5, 101.9, 101.3, 101.6),
        ]
        confirmation = ConfirmationInfo(
            state="EXPIRED", confirmation_time_us=None
        )
        outcome = evaluate(candles=candles, confirmation=confirmation)
        # baseline stops out at 101.0 -> not a winner
        assert outcome.baseline.exit_status == EXIT_STOP
        assert outcome.missed_winner is False

    def test_missed_winner_threshold_is_respected(self):
        confirmation = ConfirmationInfo(
            state="EXPIRED", confirmation_time_us=None
        )
        policy = make_policy()
        policy = policy.with_preliminary(
            "d_10_missed_winner_rule",
            MissedWinnerRule(
                definition=MISSED_WINNER_BASELINE_NET_R,
                min_net_r=999.0,
                reporting=MISSED_WINNER_REPORT_COUNT_AND_LIST,
            ),
        )
        outcome = evaluate(policy=policy, confirmation=confirmation)
        assert outcome.missed_winner is False


class TestRefusalAndCausality:
    def test_pending_decisions_produce_a_refusal_without_numbers(self):
        outcome = evaluate(policy=OutcomePolicy())
        assert outcome.status == RESULT_STATUS_NOT_READY_PENDING_DECISION
        assert outcome.baseline.net_return_r is None
        assert outcome.variant.net_return_r is None
        assert outcome.baseline.entry_price is None
        assert outcome.variant.entry_price is None
        assert "POLICY_DECISIONS_PENDING" in outcome.reason

    def test_deterministic_repeatability(self):
        first = evaluate().to_dict()
        second = evaluate().to_dict()
        assert first == second

    def test_prefix_stability_appending_future_candles_never_changes_result(self):
        early_asof = (BASE_MS + 5 * BAR_MS) * 1_000
        before = evaluate(evaluation_asof_us=early_asof).to_dict()
        extended = DEFAULT_CANDLES + [
            bar(9, 97.7, 103.0, 95.0, 102.0),
            bar(10, 102.0, 104.0, 101.0, 103.0),
        ]
        after = evaluate(
            candles=extended, evaluation_asof_us=early_asof
        ).to_dict()
        assert before == after

    def test_signal_time_must_precede_the_asof(self):
        outcome = evaluate(evaluation_asof_us=SIGNAL_US - 1)
        assert outcome.status == RESULT_STATUS_NOT_READY_PENDING_DECISION

    def test_plain_dict_inputs_are_accepted(self):
        outcome = evaluate_pair(
            event=EVENT.to_dict(),
            confirmation={
                "state": "CONFIRMED_DELAYED",
                "confirmation_time_us": CONFIRM_US,
                "confirmation_candle_open_ms": BASE_MS,
                "confirmation_close_reference": 100.2,
            },
            candles=[c.to_dict() for c in DEFAULT_CANDLES],
            policy=make_policy(),
            evaluation_asof_us=ASOF_US,
        )
        assert outcome.status == RESULT_STATUS_EVALUATED
        assert outcome.variant.entered

"""Issue #206 — tests for the read-only reference-to-execution alignment audit.

Covers: lineage matching and every INCONCLUSIVE branch (missing/unmatured/
mismatched — never substituted), the no-double-counting cost invariant,
STOP_FIRST path semantics, risk recomputation on unchanged absolute stop/
target prices, entry availability and limit-touch classification, sample
coverage gating, symbol clustering, lookahead/timestamp checks, verdict
transitions and the read-only contract.
"""
from __future__ import annotations
from pathlib import Path

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_short_a_ref_exec_alignment_audit_v1 import (
    ALIGNMENT_CONFIRMED,
    ALIGNMENT_INVALID,
    ALIGNMENT_INCONCLUSIVE,
    FEAS_FAIL,
    FEAS_INCONCLUSIVE,
    FEAS_PASS,
    MISMATCHED_REFERENCE_PRICE,
    MISMATCHED_SIGNAL_TIME,
    MISMATCHED_SYMBOL,
    MISSING_EXIT_DATA,
    MISSING_LINEAGE_OBSERVATION,
    MISSING_OUTCOME,
    OK,
    OUTCOME_UNMATURED,
    aggregate,
    alignment_verdict,
    begin_read_only,
    bootstrap_mean,
    build_row_lineage,
    counterfactual,
    cost_terms,
    derive_path_class,
    fill_feasibility_verdict,
    gross_r,
    load_evidence,
    run_audit,
    symbol_cluster,
    write_reports,
)

UTC = timezone.utc
SIGNAL = datetime(2026, 10, 26, 13, 0, 0, tzinfo=UTC)


# ── fakes ────────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []
        self.closed = False

    def execute(self, sql, params=None):
        self._conn.statements.append((sql, params))
        for matcher, columns, rows in self._conn.routes:
            if matcher in sql:
                self.description = [(c,) for c in columns]
                self._rows = list(rows)
                return
        self.description = []
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, routes=None):
        self.routes = routes or []
        self.statements = []

    def cursor(self):
        return FakeCursor(self)


def fake_conn(evidence_rows=(), observations=(), outcomes=(), activation=(),
              forward_sources=(), forward_lineage=(), forward_outcomes=()):
    def dicts(rows):
        if not rows:
            return ([], [])
        columns = list(rows[0].keys())
        return columns, [tuple(r[c] for c in columns) for r in rows]

    routes = []
    for matcher, rows in (
        ("FROM research.me_short_a_fill_activation_v1", activation),
        ("FROM research.me_short_a_fill_feasibility_v1", evidence_rows),
        ("FROM research.prospective_observation", observations),
        ("FROM research.prospective_outcome", outcomes),
        ("FROM research.me_short_a_source_signal_v1", forward_sources),
        ("FROM research.me_short_a_event_lineage_v1", forward_lineage),
        ("FROM research.me_short_a_outcome_observation_v1", forward_outcomes),
    ):
        columns, tuples = dicts(rows)
        routes.append((matcher, columns, tuples))
    return FakeConnection(routes)


def evidence_row(**over):
    row = {
        "event_key": "evt-1",
        "symbol": "ARIAUSDT",
        "signal_time": SIGNAL,
        "captured_at": SIGNAL + timedelta(seconds=2),
        "freeze_ts": datetime(2026, 10, 26, 12, 53, 57, tzinfo=UTC),
        "reference_price": 100.0,
        "stop_price": 100.2,
        "target_price": 95.0,
        "structural_r": 0.2,
        "structural_r_pct": 0.2,
        "bid": 99.0,
        "ask": 99.2,
        "book_time_ms": int((SIGNAL + timedelta(seconds=2)).timestamp() * 1000),
        "book_age_ms": 500,
        "spread_bps": 20.18,
        "reference_to_bid_bps": 100.0,
        "depth_bid_10bps_usdt": 5000.0,
        "requested_notional_usdt": 100.0,
        "requested_qty": 1.0,
        "executable_qty": 1.0,
        "min_order_qty": 0.001,
        "qty_step": 0.001,
        "min_notional": 5.0,
        "estimated_fill": 98.9,
        "entry_slippage_bps": None,
        "estimated_roundtrip_cost_pct": None,
        "estimated_roundtrip_cost_r": None,
        "cost_class": "ABOVE_ELEVATED",
        "feasibility_status": "OK",
        "reason": None,
        "scenario_results": {
            "scenarios": [
                {"notional_usdt": "100", "estimated_fill": "98.9", "status": "OK",
                 "constraints_known": True, "below_min_qty": False,
                 "below_min_notional": False},
                {"notional_usdt": "500", "estimated_fill": "98.8", "status": "OK",
                 "constraints_known": True, "below_min_qty": False,
                 "below_min_notional": False},
                {"notional_usdt": "1000", "estimated_fill": "98.7", "status": "OK",
                 "constraints_known": True, "below_min_qty": False,
                 "below_min_notional": False},
            ]
        },
        "source_observation_ids": [555],
        "raw_snapshot": {},
    }
    row.update(over)
    return row


def observation_row(**over):
    row = {
        "observation_id": 555,
        "experiment_id": "ME_SHORT_GEOM_A_EXECUTION_V1",
        "symbol": "ARIAUSDT",
        "direction": "SHORT",
        "signal_time": SIGNAL,
        "reference_price": 100.0,
        "invalidation_price": 100.2,
        "target_1": 95.0,
        "target_2": 93.0,
        "features": {"trigger_5m_close": 99.5},
        "parameters": {},
        "created_at": SIGNAL,
    }
    row.update(over)
    return row


def outcome_row(**over):
    row = {
        "observation_id": 555,
        "experiment_id": "ME_SHORT_GEOM_A_EXECUTION_V1",
        "mfe_240m": 3.1,
        "mae_240m": 0.0,
        "mfe_r_240m": 15.5,
        "mae_r_240m": 0.0,
        "return_at_240m": -2.5,
        "tp_hit": True,
        "sl_hit": False,
        "tp_before_sl": True,
        "sl_before_tp": False,
        "ambiguous_intrabar": False,
        "time_to_tp": 20.0,
        "time_to_sl": None,
        "evaluated_240m_at": SIGNAL + timedelta(minutes=241),
        "is_final": True,
        "created_at": SIGNAL + timedelta(minutes=241),
    }
    row.update(over)
    return row


# ── read-only contract ───────────────────────────────────────────────────


def test_run_audit_only_issues_selects():
    conn = fake_conn([evidence_row()], [observation_row()], [outcome_row()],
                     [{"experiment_id": "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1",
                       "status": "RUNNING", "freeze_ts": SIGNAL - timedelta(minutes=6)}])
    begin_read_only(conn)
    run_audit(conn)
    for sql, _ in conn.statements:
        statement = sql.strip().upper()
        assert statement.startswith(("SELECT", "WITH", "SET TRANSACTION READ ONLY")), sql


def test_select_rejects_writes():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import _select
    conn = FakeConnection()
    with pytest.raises(ValueError):
        _select(conn, "DELETE FROM research.me_short_a_fill_feasibility_v1")


# ── lineage and INCONCLUSIVE branches (no substitution) ──────────────────


def test_lineage_matched_row_is_ok():
    lineage = build_row_lineage(evidence_row(), {555: observation_row()},
                                {555: outcome_row()}, SIGNAL + timedelta(hours=5))
    assert lineage["lineage_status"] == OK
    assert lineage["observation_id"] == 555
    assert lineage["lookahead_flags"] == []
    assert lineage["features_present"]["trigger_5m_close"] is True


def test_missing_observation_is_inconclusive():
    lineage = build_row_lineage(evidence_row(), {}, {}, SIGNAL)
    assert lineage["lineage_status"] == MISSING_LINEAGE_OBSERVATION
    assert any("prospective_observation" in f for f in lineage["missing_fields"])


def test_mismatched_lineage_never_borrows_values():
    bad_symbol = build_row_lineage(evidence_row(), {555: observation_row(symbol="XAUUSDT")},
                                   {555: outcome_row()}, SIGNAL)
    assert bad_symbol["lineage_status"] == MISMATCHED_SYMBOL

    bad_time = build_row_lineage(
        evidence_row(), {555: observation_row(signal_time=SIGNAL + timedelta(minutes=5))},
        {555: outcome_row()}, SIGNAL)
    assert bad_time["lineage_status"] == MISMATCHED_SIGNAL_TIME

    bad_price = build_row_lineage(
        evidence_row(), {555: observation_row(reference_price=101.0)},
        {555: outcome_row()}, SIGNAL)
    assert bad_price["lineage_status"] == MISMATCHED_REFERENCE_PRICE
    assert "outcome" not in bad_price  # nothing borrowed


def test_missing_outcome_and_unmatured_are_inconclusive():
    missing = build_row_lineage(evidence_row(), {555: observation_row()}, {}, SIGNAL)
    assert missing["lineage_status"] == MISSING_OUTCOME

    immature = build_row_lineage(
        evidence_row(), {555: observation_row()},
        {555: outcome_row(is_final=False, evaluated_240m_at=None)},
        SIGNAL + timedelta(hours=5))
    assert immature["lineage_status"] == OUTCOME_UNMATURED

    too_early = build_row_lineage(
        evidence_row(), {555: observation_row()}, {555: outcome_row()},
        SIGNAL + timedelta(minutes=100))  # before 240m hold
    assert too_early["lineage_status"] == OUTCOME_UNMATURED


def test_missing_features_are_reported_not_invented():
    lineage = build_row_lineage(
        evidence_row(),
        {555: observation_row(features={}, parameters={})},
        {555: outcome_row()}, SIGNAL + timedelta(hours=5))
    assert "trigger_5m_close" in lineage["missing_fields"]
    assert "entry_zone_low" in lineage["missing_fields"]
    assert lineage["features_present"]["trigger_5m_close"] is False


# ── cost decomposition / double counting ─────────────────────────────────


def test_cost_terms_decomposition_and_overlap():
    terms = cost_terms(evidence_row(), 98.9)
    assert terms["A_reference_to_bid_pct"] == pytest.approx(0.01)          # (100-99)/100
    assert terms["B_bid_to_fill_pct"] == pytest.approx(0.001)              # (99-98.9)/100
    assert terms["C_spread_pct"] == pytest.approx(0.2 / 99.1)
    assert terms["D_fees_pct"] == pytest.approx(0.0011)
    assert terms["entry_deviation_total_pct"] == pytest.approx(0.011)
    assert terms["diagnostic_phase1_cost_pct"] == pytest.approx(
        0.011 + 0.2 / 99.1 + 0.0011)
    # A+B+C is exactly what would be double counted if subtracted again.
    assert terms["overlap_if_subtracted_again_pct"] == pytest.approx(
        0.011 + 0.2 / 99.1)


def test_price_improvement_is_negative_b_not_clamped():
    terms = cost_terms(evidence_row(), 99.5)  # fill above bid
    assert terms["B_bid_to_fill_pct"] == pytest.approx(-0.005)
    assert terms["entry_deviation_total_pct"] == pytest.approx(0.01 - 0.005)


def test_executable_net_never_subtracts_entry_side_again():
    outcome = outcome_row()  # TP_FIRST
    cf = counterfactual(evidence_row(), outcome, 98.9, 1000)
    execu = cf["executable"]
    assert execu["gross_r"] == pytest.approx((98.9 - 95.0) / (100.2 - 98.9))
    assert execu["cost_r_exit_only_frozen"] == pytest.approx(
        0.00105 * 98.9 / (100.2 - 98.9))
    assert execu["cost_r_entry_fee"] == pytest.approx(
        0.00055 * 98.9 / (100.2 - 98.9))
    assert execu["net_r_frozen_exit"] == pytest.approx(
        execu["gross_r"]
        - execu["cost_r_entry_fee"]
        - execu["cost_r_exit_only_frozen"])
    # The misapplied diagnostic cost (A+B+C+D at reference anchor) would be
    # far larger than the exit-only cost actually used.
    diagnostic = 0.011 + 0.2 / 99.1 + 0.0011
    double_counted = diagnostic * 100.0 / (100.2 - 98.9)
    assert execu["cost_r_exit_only_frozen"] < double_counted
    # And control keeps the frozen round-trip accounting instead.
    assert cf["control"]["cost_r"] == pytest.approx(0.0021 * 100.0 / 0.2)


# ── STOP_FIRST paths, risk recomputation, unknown exits ──────────────────


def test_stop_first_ambiguous_is_full_stop_loss():
    outcome = outcome_row(ambiguous_intrabar=True, tp_before_sl=False,
                          sl_before_tp=True, tp_hit=True, sl_hit=True)
    assert derive_path_class(outcome) == "AMBIGUOUS_STOP_FIRST"
    assert gross_r(100.0, 100.2, 95.0, "AMBIGUOUS_STOP_FIRST", None) == -1.0
    cf = counterfactual(evidence_row(), outcome, 98.9, 100)
    assert cf["control"]["gross_r"] == -1.0
    assert cf["executable"]["gross_r"] == -1.0


def test_timeout_uses_derived_absolute_close_unchanged_by_entry():
    outcome = outcome_row(tp_before_sl=False, sl_before_tp=False,
                          tp_hit=False, sl_hit=False, return_at_240m=-2.5)
    assert derive_path_class(outcome) == "TIMEOUT"
    cf = counterfactual(evidence_row(), outcome, 98.9, 100)
    # close = 100 * (1 - 0.025) = 97.5 — same absolute price in both views.
    assert cf["timeout_close_abs"] == pytest.approx(97.5)
    assert cf["control"]["gross_r"] == pytest.approx((100 - 97.5) / 0.2)
    assert cf["executable"]["gross_r"] == pytest.approx((98.9 - 97.5) / 1.3)


def test_missing_exit_data_is_unknown_not_zero():
    outcome = outcome_row(tp_before_sl=False, sl_before_tp=False,
                          tp_hit=False, sl_hit=False, return_at_240m=None)
    cf = counterfactual(evidence_row(), outcome, 98.9, 100)
    assert cf["control"]["gross_r"] is None
    assert cf["control"]["net_r"] is None
    assert cf["executable"]["status"] == MISSING_EXIT_DATA
    assert cf["executable"]["net_r_frozen_exit"] is None


def test_risk_recomputed_on_same_absolute_stop_target():
    cf = counterfactual(evidence_row(), outcome_row(), 98.9, 500)
    execu = cf["executable"]
    assert execu["risk_abs"] == pytest.approx(100.2 - 98.9)
    assert execu["risk_inflation_pct"] == pytest.approx(
        (1.3 - 0.2) / 0.2 * 100)
    assert cf["control"]["risk_abs"] == pytest.approx(0.2)
    # stop/target prices identical in both views.
    assert cf["stop_price"] == 100.2
    assert cf["target_price"] == 95.0


def test_entry_infeasible_scenario_has_no_counterfactual_substitution():
    cf = counterfactual(evidence_row(), outcome_row(), None, 1000)
    assert cf["executable"]["gross_r"] is None
    assert cf["executable"]["status"] == "ENTRY_INFEASIBLE_OR_UNKNOWN"
    assert cf["control"]["gross_r"] is not None  # control still computed


# ── limit touch, lookahead, clusters ─────────────────────────────────────


def test_limit_touch_derived_from_mae():
    def run_with_mae(mae):
        conn = fake_conn([evidence_row()], [observation_row()],
                         [outcome_row(mae_240m=mae)],
                         [{"experiment_id": "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1",
                           "status": "RUNNING", "freeze_ts": SIGNAL}])
        report = run_audit(conn, now=SIGNAL + timedelta(hours=5),
                           bootstrap_n=10, seed=3)
        return report["rows"][0]["legacy_limit_at_reference_touch"]

    # MAE for SHORT = adverse up-move above entry: >0 means the reference
    # price traded again (limit at reference would fill); 0 is a NO_TOUCH.
    assert run_with_mae(0.3) is True
    assert run_with_mae(0.0) is False
    assert run_with_mae(None) is None


def test_lookahead_and_timestamp_flags():
    past_capture = evidence_row(captured_at=SIGNAL - timedelta(seconds=5))
    future_book = evidence_row(
        book_time_ms=int((SIGNAL + timedelta(seconds=30)).timestamp() * 1000))
    early_outcome = outcome_row(evaluated_240m_at=SIGNAL + timedelta(minutes=10),
                                created_at=SIGNAL + timedelta(minutes=10))
    l1 = build_row_lineage(past_capture, {555: observation_row()},
                           {555: outcome_row()}, SIGNAL + timedelta(hours=5))
    assert "capture_before_signal" in l1["lookahead_flags"]
    l2 = build_row_lineage(future_book, {555: observation_row()},
                           {555: outcome_row()}, SIGNAL + timedelta(hours=5))
    assert "book_timestamp_in_future" in l2["lookahead_flags"]
    l3 = build_row_lineage(evidence_row(), {555: observation_row()},
                           {555: early_outcome}, SIGNAL + timedelta(hours=5))
    assert "outcome_finalized_before_maturity" in l3["lookahead_flags"]

    frozen = evidence_row(signal_time=SIGNAL, freeze_ts=SIGNAL)  # not after
    l4 = build_row_lineage(frozen, {555: observation_row(signal_time=SIGNAL)},
                           {555: outcome_row()}, SIGNAL + timedelta(hours=5))
    assert "signal_time_not_after_phase1_freeze" in l4["lookahead_flags"]


def test_symbol_clusters_group_correlated_symbols():
    assert symbol_cluster("XAUUSDT") == symbol_cluster("XAUTUSDT") == "XAU"
    assert symbol_cluster("BTCUSDT") == "BTC"
    assert symbol_cluster("ARIAUSDT") == "ARI"


# ── sample gating and verdicts ───────────────────────────────────────────


def _base_view(symbol, control_net, exec_net):
    return {
        "event_key": f"evt-{symbol}-{control_net}",
        "symbol": symbol,
        "lineage_status": OK,
        "lookahead_flags": [],
        "control": {"net_r": control_net, "gross_r": control_net, "cost_r": 0.0},
        "executable": {"net_r_frozen_exit": exec_net, "net_r_worst_exit": exec_net - 0.1,
                       "gross_r": exec_net, "cost_r_exit_only_frozen": 0.0},
    }


def test_three_rows_never_publish_profitability():
    rows = [_base_view(f"S{i}USDT", 2.0, 1.5) for i in range(3)]
    agg = aggregate(rows, bootstrap_n=50, seed=1)
    assert agg["matched_conclusive_rows"] == 3
    assert agg["control_net_r"]["insufficient_sample"] is True
    assert agg["control_net_r"]["point_estimate"] is None
    assert agg["control_net_r"]["bootstrap"] is None

    verdict = alignment_verdict(rows, agg)
    assert verdict["verdict"] == ALIGNMENT_INCONCLUSIVE
    assert any("minimum_n=200" in reason for reason in verdict["reasons"])


def test_alignment_confirmed_at_frozen_cohort_size():
    rows = [_base_view(f"S{i:03d}USDT", 2.0, 1.5) for i in range(200)]
    agg = aggregate(rows, bootstrap_n=200, seed=7)
    assert agg["matched_conclusive_rows"] >= 200
    assert agg["symbols"] >= 20
    verdict = alignment_verdict(rows, agg)
    assert verdict["verdict"] == ALIGNMENT_CONFIRMED


def test_alignment_invalid_when_executable_economics_fail():
    rows = [_base_view(f"S{i:03d}USDT", 2.0, -3.0) for i in range(200)]
    agg = aggregate(rows, bootstrap_n=200, seed=7)
    verdict = alignment_verdict(rows, agg)
    assert verdict["verdict"] == ALIGNMENT_INVALID


def test_lookahead_flags_block_confirmation():
    rows = [_base_view(f"S{i:03d}USDT", 2.0, 1.5) for i in range(200)]
    rows[0]["lookahead_flags"] = ["capture_before_signal"]
    agg = aggregate(rows, bootstrap_n=200, seed=7)
    verdict = alignment_verdict(rows, agg)
    assert verdict["verdict"] == ALIGNMENT_INCONCLUSIVE


def test_fill_feasibility_verdicts():
    def ev(status, known=True):
        return {"feasibility_status": status,
                "scenario_results": {"scenarios": [
                    {"constraints_known": known, "status": status}]}}

    small = fill_feasibility_verdict([ev("OK")] * 3)
    assert small["verdict"] == FEAS_INCONCLUSIVE

    good = fill_feasibility_verdict([ev("OK")] * 30)
    assert good["verdict"] == FEAS_PASS

    bad = fill_feasibility_verdict([ev("OK")] * 25 + [ev("INSUFFICIENT_DEPTH")] * 5)
    assert bad["verdict"] == FEAS_FAIL

    # Rows with UNKNOWN constraints never enter the min-feasibility
    # denominator (they do count for the quote/depth ok_rate).
    excluded = fill_feasibility_verdict(
        [ev("OK", known=True)] * 30 + [ev("INSUFFICIENT_DEPTH", known=False)] * 10)
    assert excluded["min_constraint_feasible_rate"] == pytest.approx(1.0)
    assert excluded["verdict"] == FEAS_FAIL  # ok_rate = 30/40 < 0.95


def test_bootstrap_is_deterministic():
    values = [1.0, -2.0, 3.5, 0.5, -0.25] * 8
    first = bootstrap_mean(values, n_resamples=500, seed=42)
    second = bootstrap_mean(values, n_resamples=500, seed=42)
    assert first == second
    assert first["ci_95_low"] <= first["point_estimate"] <= first["ci_95_high"]


# ── end-to-end run over a fake database ──────────────────────────────────


def test_run_audit_end_to_end_matched_row():
    source = {
        "event_key": "evt-1",
        "symbol": "ARIAUSDT",
        "direction": "SHORT",
        "signal_time": SIGNAL,
        "reference_price": 100.0,
        "stop_price": 100.2,
        "target_price": 95.0,
        "no_lookahead_flags": [],
    }
    lineage = {
        **source,
        "link_status": "LINKED",
        "event_key_integrity": True,
        "phase1_linked": True,
        "source_signal_present": True,
        "phase1_event_key": "evt-1",
    }
    forward_outcome = {
        **source,
        "outcome_status": "MATCHED_FINALIZED",
        "is_final": True,
        "coverage_complete": True,
        "asof_ok": True,
        "horizon_end_time": SIGNAL + timedelta(minutes=240),
        "finalization_candle_close_time": SIGNAL + timedelta(minutes=240),
        "finalized_at": SIGNAL + timedelta(minutes=241),
        "path_class": "TARGET_HIT",
        "exit_price": 95.0,
        "source_quality_flags": [],
    }
    conn = fake_conn(
        evidence_rows=[evidence_row()],
        observations=[observation_row()],
        outcomes=[outcome_row()],
        activation=[{
            "experiment_id": "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1",
            "status": "RUNNING",
            "freeze_ts": datetime(2026, 10, 26, 12, 53, 57, tzinfo=UTC),
        }],
        forward_sources=[source],
        forward_lineage=[lineage],
        forward_outcomes=[forward_outcome],
    )
    report = run_audit(conn, now=SIGNAL + timedelta(hours=5), bootstrap_n=50, seed=3)
    assert report["alignment_verdict"] == ALIGNMENT_INCONCLUSIVE  # n < minimum
    assert report["fill_feasibility_verdict"] == FEAS_INCONCLUSIVE
    assert report["sources"]["evidence_rows"] == 1
    row = report["rows"][0]
    assert row["lineage_status"] == OK
    assert len(row["scenarios"]) == 3
    base = row["scenarios"][0]["counterfactual"]
    assert base["control"]["gross_r"] == pytest.approx(25.0)  # (100-95)/0.2
    assert base["executable"]["gross_r"] == pytest.approx(3.0)  # (98.9-95)/1.3
    assert row["legacy_limit_at_reference_touch"] is False  # mae_240m == 0 -> NO_TOUCH
    assert report["prospective_only_plan"]


def test_run_audit_with_unavailable_source_reports_missing_fields():
    conn = FakeConnection()  # no routes: everything empty
    report = run_audit(conn, now=SIGNAL, bootstrap_n=10, seed=3)
    assert report["alignment_verdict"] == ALIGNMENT_INCONCLUSIVE
    assert report["rows"] == []
    assert any("unavailable or empty" in r for r in report["alignment"]["reasons"])
    assert report["missing_fields"]["registry"]
    assert report["prospective_only_plan"]


def test_run_audit_survives_broken_evidence_table():
    class BrokenConn(FakeConnection):
        def cursor(self):
            raise RuntimeError("relation does not exist")

    report = run_audit(BrokenConn(), now=SIGNAL, bootstrap_n=10, seed=3)
    assert report["alignment_verdict"] == ALIGNMENT_INCONCLUSIVE
    assert report["sources"]["evidence_rows"] is None
    assert "evidence_error" in report["sources"]


def test_write_reports_emits_all_deliverables(tmp_path):
    conn = fake_conn([evidence_row()], [observation_row()], [outcome_row()],
                     [{"experiment_id": "ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1",
                       "status": "RUNNING", "freeze_ts": SIGNAL}])
    report = run_audit(conn, now=SIGNAL + timedelta(hours=5), bootstrap_n=20, seed=3)
    written = write_reports(report, str(tmp_path))
    names = sorted(Path(p).name for p in written)
    assert names == [
        "EXECUTABLE_ENTRY_COUNTERFACTUAL.md",
        "REFERENCE_PRICE_LINEAGE.md",
        "REFERENCE_TO_BID_COST_DECOMPOSITION.csv",
        "alignment_audit_report.json",
    ]
    csv_text = open(tmp_path / "REFERENCE_TO_BID_COST_DECOMPOSITION.csv",
                    encoding="utf-8").read()
    header = csv_text.splitlines()[0]
    assert "A_reference_to_bid_pct" in header
    assert "overlap_if_subtracted_again_pct" in header
    data = json.loads((tmp_path / "alignment_audit_report.json").read_text("utf-8"))
    assert data["alignment_verdict"] == ALIGNMENT_INCONCLUSIVE
    md = (tmp_path / "EXECUTABLE_ENTRY_COUNTERFACTUAL.md").read_text("utf-8")
    assert "No double counting" in md
    assert "prospective-only plan" in md.lower()

def test_executable_net_includes_entry_fee_exactly_once():
    cf = counterfactual(evidence_row(), outcome_row(), 98.9, 1000)
    execu = cf["executable"]

    entry_fee_r = 0.00055 * 98.9 / (100.2 - 98.9)

    assert execu["cost_r_entry_fee"] == pytest.approx(entry_fee_r)
    assert execu["net_r_frozen_exit"] == pytest.approx(
        execu["gross_r"] - entry_fee_r - execu["cost_r_exit_only_frozen"]
    )
    assert execu["net_r_worst_exit"] == pytest.approx(
        execu["gross_r"] - entry_fee_r - execu["cost_r_exit_only_worst"]
    )

def test_forward_outcome_timeout_uses_absolute_exit_price():
    outcome = {
        "outcome_status": "MATCHED_FINALIZED",
        "path_class": "TIMEOUT_240M",
        "exit_price": 97.5,
        "return_at_240m": 999.0,
        "tp_before_sl": False,
        "sl_before_tp": False,
        "ambiguous_intrabar": False,
    }

    cf = counterfactual(evidence_row(), outcome, 98.9, 1000)

    assert cf["path_class"] == "TIMEOUT"
    assert cf["timeout_close_abs"] == pytest.approx(97.5)
    assert cf["control"]["gross_r"] == pytest.approx(
        (100.0 - 97.5) / (100.2 - 100.0)
    )
    assert cf["executable"]["gross_r"] == pytest.approx(
        (98.9 - 97.5) / (100.2 - 98.9)
    )

@pytest.mark.parametrize("path,expected", [
    ("TARGET_HIT", "TP_FIRST"),
    ("STOP_HIT", "SL_FIRST"),
    ("TIMEOUT_240M", "TIMEOUT"),
])
def test_forward_outcome_path_mapping(path, expected):
    outcome = {
        "outcome_status": "MATCHED_FINALIZED",
        "path_class": path,
        "exit_price": 97.5,
        "ambiguous_intrabar": False,
    }
    assert derive_path_class(outcome) == expected


def test_forward_outcome_ambiguous_stop_first():
    outcome = {
        "outcome_status": "MATCHED_FINALIZED",
        "path_class": "STOP_HIT",
        "ambiguous_intrabar": True,
    }
    assert derive_path_class(outcome) == "AMBIGUOUS_STOP_FIRST"


@pytest.mark.parametrize("status,path", [
    ("OUTCOME_UNMATURED", "TARGET_HIT"),
    ("SOURCE_INCOMPLETE", "TARGET_HIT"),
    ("MATCHED_FINALIZED", "UNRESOLVED"),
])
def test_forward_outcome_rejects_invalid_or_unfinalized(status, path):
    outcome = {
        "outcome_status": status,
        "path_class": path,
        "ambiguous_intrabar": False,
    }
    with pytest.raises(ValueError):
        derive_path_class(outcome)

def test_forward_aggregate_rejects_unverified_lineage():
    row = {
        "event_key": "evt-1",
        "symbol": "ARIAUSDT",
        "lineage_status": OK,
        "forward_link_status": "QUARANTINED",
        "forward_event_key_integrity": False,
        "forward_phase1_linked": False,
        "forward_outcome_status": "SOURCE_INCOMPLETE",
        "forward_is_final": False,
        "lookahead_flags": [],
        "control": {"net_r": 2.0},
        "executable": {"net_r_frozen_exit": 1.5,
                       "net_r_worst_exit": 1.4},
    }

    result = aggregate([row], bootstrap_n=100, seed=7)

    assert result["matched_conclusive_rows"] == 0
    assert result["control_net_r"]["n"] == 0
    assert result["executable_net_r_frozen_exit"]["n"] == 0

def test_forward_aggregate_missing_lineage_is_fail_closed():
    row = {
        "event_key": "evt-no-forward",
        "symbol": "ARIAUSDT",
        "lineage_status": OK,
        "lookahead_flags": [],
        "control": {"net_r": 2.0},
        "executable": {
            "net_r_frozen_exit": 1.5,
            "net_r_worst_exit": 1.4,
        },
    }

    result = aggregate(
        [row], bootstrap_n=100, seed=7, require_forward=True
    )

    assert result["matched_conclusive_rows"] == 0
    assert result["control_net_r"]["n"] == 0
    assert result["executable_net_r_frozen_exit"]["n"] == 0

def test_run_audit_excludes_legacy_outcomes_without_forward_lineage():
    conn = fake_conn(
        evidence_rows=[evidence_row()],
        observations=[observation_row()],
        outcomes=[outcome_row()],
    )

    report = run_audit(
        conn,
        now=SIGNAL + timedelta(hours=5),
        bootstrap_n=10,
        seed=7,
    )

    assert report["aggregates"]["matched_conclusive_rows"] == 0
    assert report["aggregates"]["control_net_r"]["n"] == 0
    assert report["aggregates"]["executable_net_r_frozen_exit"]["n"] == 0

def test_validate_forward_event_accepts_verified_finalized():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import validate_forward_event

    evidence = evidence_row()
    source = {
        "event_key": "evt-1", "symbol": "ARIAUSDT",
        "direction": "SHORT", "signal_time": SIGNAL,
        "reference_price": 100.0, "stop_price": 100.2,
        "target_price": 95.0, "no_lookahead_flags": [],
    }
    lineage = {
        **source,
        "link_status": "LINKED",
        "event_key_integrity": True,
        "phase1_linked": True,
        "source_signal_present": True,
        "phase1_event_key": "evt-1",
    }
    outcome = {
        **source,
        "outcome_status": "MATCHED_FINALIZED",
        "is_final": True,
        "coverage_complete": True,
        "asof_ok": True,
        "horizon_end_time": SIGNAL + timedelta(minutes=240),
        "finalization_candle_close_time": SIGNAL + timedelta(minutes=240),
        "finalized_at": SIGNAL + timedelta(minutes=241),
        "path_class": "TARGET_HIT",
        "exit_price": 95.0,
        "source_quality_flags": [],
    }

    result = validate_forward_event(
        evidence, source, lineage, outcome, SIGNAL + timedelta(hours=5)
    )
    assert result["eligible"] is True
    assert result["reasons"] == []


def test_validate_forward_event_rejects_incomplete_outcome():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import validate_forward_event

    result = validate_forward_event(
        evidence_row(), None, None,
        {"event_key": "evt-1", "outcome_status": "SOURCE_INCOMPLETE",
         "is_final": False, "coverage_complete": False, "asof_ok": False},
        SIGNAL + timedelta(hours=5),
    )

    assert result["eligible"] is False
    assert "SOURCE_SIGNAL_MISSING" in result["reasons"]
    assert "FORWARD_LINEAGE_MISSING" in result["reasons"]
    assert "OUTCOME_NOT_FINALIZED" in result["reasons"]

def test_run_audit_uses_forward_outcome_without_legacy_observation():
    evidence = evidence_row(source_observation_ids=None)
    source = {
        "event_key": "evt-1", "symbol": "ARIAUSDT",
        "direction": "SHORT", "signal_time": SIGNAL,
        "reference_price": 100.0, "stop_price": 100.2,
        "target_price": 95.0, "no_lookahead_flags": [],
    }
    lineage = {
        **source,
        "link_status": "LINKED",
        "event_key_integrity": True,
        "phase1_linked": True,
        "source_signal_present": True,
        "phase1_event_key": "evt-1",
    }
    outcome = {
        **source,
        "outcome_status": "MATCHED_FINALIZED",
        "is_final": True,
        "coverage_complete": True,
        "asof_ok": True,
        "horizon_end_time": SIGNAL + timedelta(minutes=240),
        "finalization_candle_close_time": SIGNAL + timedelta(minutes=240),
        "finalized_at": SIGNAL + timedelta(minutes=241),
        "path_class": "TARGET_HIT",
        "exit_price": 95.0,
        "source_quality_flags": [],
        "mae_240m": 0.0,
    }

    conn = fake_conn(
        evidence_rows=[evidence],
        forward_sources=[source],
        forward_lineage=[lineage],
        forward_outcomes=[outcome],
    )
    report = run_audit(
        conn, now=SIGNAL + timedelta(hours=5),
        bootstrap_n=10, seed=7,
    )

    assert report["sources"]["forward_eligible"] == 1
    assert report["rows"][0]["forward_eligibility"]["eligible"] is True
    assert report["rows"][0]["lineage_status"] == OK
    assert report["rows"][0]["legacy_lineage_status"] == MISSING_LINEAGE_OBSERVATION
    assert "source_observation_ids[0] -> prospective_observation" in (
        report["rows"][0]["legacy_missing_fields"]
    )
    assert report["aggregates"]["matched_conclusive_rows"] == 1
    assert report["rows"][0]["scenarios"][0]["counterfactual"]["executable"]["gross_r"] == pytest.approx(3.0)

def test_forward_success_preserves_legacy_missing_observation_diagnostic():
    evidence = evidence_row(source_observation_ids=None)
    legacy = build_row_lineage(
        evidence, {}, {}, SIGNAL + timedelta(hours=5)
    )

    assert legacy["lineage_status"] == MISSING_LINEAGE_OBSERVATION
    assert "source_observation_ids[0] -> prospective_observation" in legacy["missing_fields"]
def test_shared_forward_eligibility_is_fail_closed():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import eligible_for_decision

    valid = {
        "lineage_status": OK,
        "lookahead_flags": [],
        "forward_validated": True,
        "forward_link_status": "LINKED",
        "forward_event_key_integrity": True,
        "forward_phase1_linked": True,
        "forward_outcome_status": "MATCHED_FINALIZED",
        "forward_is_final": True,
    }

    assert eligible_for_decision(valid, require_forward=True)

    for field in (
        "forward_validated",
        "forward_link_status",
        "forward_event_key_integrity",
        "forward_phase1_linked",
        "forward_outcome_status",
        "forward_is_final",
    ):
        invalid = dict(valid)
        invalid.pop(field)
        assert not eligible_for_decision(invalid, require_forward=True), field

    invalid = dict(valid, lookahead_flags=["capture_before_signal"])
    assert not eligible_for_decision(invalid, require_forward=True)

    invalid = dict(valid, lineage_status=MISSING_LINEAGE_OBSERVATION)
    assert not eligible_for_decision(invalid, require_forward=True)
def test_shared_forward_eligibility_is_fail_closed():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import eligible_for_decision

    valid = {
        "lineage_status": OK,
        "lookahead_flags": [],
        "forward_validated": True,
        "forward_link_status": "LINKED",
        "forward_event_key_integrity": True,
        "forward_phase1_linked": True,
        "forward_outcome_status": "MATCHED_FINALIZED",
        "forward_is_final": True,
    }

    assert eligible_for_decision(valid, require_forward=True)

    for field in (
        "forward_validated",
        "forward_link_status",
        "forward_event_key_integrity",
        "forward_phase1_linked",
        "forward_outcome_status",
        "forward_is_final",
    ):
        invalid = dict(valid)
        invalid.pop(field)
        assert not eligible_for_decision(invalid, require_forward=True), field

    invalid = dict(valid, lookahead_flags=["capture_before_signal"])
    assert not eligible_for_decision(invalid, require_forward=True)

    invalid = dict(valid, lineage_status=MISSING_LINEAGE_OBSERVATION)
    assert not eligible_for_decision(invalid, require_forward=True)

def test_forward_validator_rejects_source_quality_and_missing_fields():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import validate_forward_event

    evidence = evidence_row()
    source = {
        "event_key": evidence["event_key"],
        "symbol": evidence["symbol"],
        "direction": "SHORT",
        "signal_time": evidence["signal_time"],
        "reference_price": evidence["reference_price"],
        "stop_price": evidence["stop_price"],
        "target_price": evidence["target_price"],
        "source_quality_flags": ["INVALID_SOURCE"],
        "missing_fields": ["trigger_5m_close"],
        "no_lookahead_flags": [],
    }
    lineage = {
        **source,
        "link_status": "LINKED",
        "event_key_integrity": True,
        "phase1_linked": True,
        "source_signal_present": True,
        "phase1_event_key": evidence["event_key"],
    }
    outcome = {
        **source,
        "outcome_status": "MATCHED_FINALIZED",
        "is_final": True,
        "coverage_complete": True,
        "asof_ok": True,
        "horizon_end_time": SIGNAL + timedelta(minutes=240),
        "finalization_candle_close_time": SIGNAL + timedelta(minutes=240),
        "finalized_at": SIGNAL + timedelta(minutes=241),
        "path_class": "TARGET_HIT",
        "exit_price": 95.0,
        "source_quality_flags": [],
        "missing_fields": [],
    }

    result = validate_forward_event(
        evidence, source, lineage, outcome, SIGNAL + timedelta(hours=5)
    )

    assert result["eligible"] is False
    assert "SOURCE_QUALITY_FLAGS" in result["reasons"]
    assert "SOURCE_MISSING_FIELDS" in result["reasons"]

def test_forward_validator_rejects_invalid_finalization_timing():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import validate_forward_event

    evidence = evidence_row()
    source = {
        "event_key": evidence["event_key"],
        "symbol": evidence["symbol"],
        "direction": "SHORT",
        "signal_time": evidence["signal_time"],
        "reference_price": evidence["reference_price"],
        "stop_price": evidence["stop_price"],
        "target_price": evidence["target_price"],
        "no_lookahead_flags": [],
        "source_quality_flags": [],
        "missing_fields": [],
    }
    lineage = {
        **source,
        "link_status": "LINKED",
        "event_key_integrity": True,
        "phase1_linked": True,
        "source_signal_present": True,
        "phase1_event_key": evidence["event_key"],
    }
    outcome = {
        **source,
        "outcome_status": "MATCHED_FINALIZED",
        "is_final": True,
        "coverage_complete": True,
        "asof_ok": True,
        "horizon_end_time": SIGNAL + timedelta(minutes=240),
        "finalization_candle_close_time": SIGNAL + timedelta(minutes=240),
        "finalized_at": SIGNAL + timedelta(minutes=241),
        "path_class": "TARGET_HIT",
        "exit_price": 95.0,
        "source_quality_flags": [],
        "missing_fields": [],
    }

    assert validate_forward_event(
        evidence, source, lineage, outcome, SIGNAL + timedelta(hours=5)
    )["eligible"] is True

    premature = dict(outcome, finalized_at=SIGNAL + timedelta(minutes=239))
    assert validate_forward_event(
        evidence, source, lineage, premature, SIGNAL + timedelta(hours=5)
    )["eligible"] is False

    wrong_horizon = dict(outcome, horizon_end_time=SIGNAL + timedelta(minutes=230))
    assert validate_forward_event(
        evidence, source, lineage, wrong_horizon, SIGNAL + timedelta(hours=5)
    )["eligible"] is False

def test_alignment_requires_usable_paired_executable_sample():
    rows = [
        _base_view(f"S{i:03d}USDT", 2.0, 1.5)
        for i in range(200)
    ]
    for row in rows[1:]:
        row["executable"]["net_r_frozen_exit"] = None
        row["executable"]["net_r_worst_exit"] = None

    agg = aggregate(rows, bootstrap_n=50, seed=7)
    verdict = alignment_verdict(rows, agg)

    assert agg["matched_conclusive_rows"] == 200
    assert agg["executable_net_r_frozen_exit"]["n"] == 1
    assert agg["executable_net_r_frozen_exit"]["insufficient_sample"] is True
    assert agg["executable_net_r_frozen_exit"]["point_estimate"] is None
    assert agg["executable_net_r_frozen_exit"]["bootstrap"] is None
    assert verdict["verdict"] == ALIGNMENT_INCONCLUSIVE


def test_alignment_invalid_requires_frozen_cohort_thresholds():
    rows = [
        _base_view("BTCUSDT", 2.0, -3.0)
        for _ in range(30)
    ]

    agg = aggregate(rows, bootstrap_n=50, seed=7)
    verdict = alignment_verdict(rows, agg)

    assert verdict["verdict"] == ALIGNMENT_INCONCLUSIVE
    assert any(
        "minimum_n=200" in reason
        for reason in verdict["reasons"]
    )
    assert any(
        "minimum_symbols=20" in reason
        for reason in verdict["reasons"]
    )

def test_executable_fill_requires_verified_exchange_feasibility():
    from decimal import Decimal
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import scenario_executable_fill

    cases = [
        (
            {
                "status": "BELOW_EXCHANGE_MIN",
                "estimated_fill": "98.9",
                "constraints_known": True,
                "below_min_qty": True,
                "below_min_notional": False,
            },
            None,
        ),
        (
            {
                "status": "OK",
                "estimated_fill": "98.9",
                "constraints_known": False,
                "below_min_qty": False,
                "below_min_notional": False,
            },
            None,
        ),
        (
            {
                "status": "OK",
                "estimated_fill": "98.9",
                "constraints_known": True,
                "below_min_qty": False,
                "below_min_notional": False,
            },
            Decimal("98.9"),
        ),
    ]

    for scenario, expected in cases:
        assert scenario_executable_fill(scenario) == expected

def test_forward_validator_distinguishes_positive_no_lookahead_marker():
    from app.research.me_short_a_ref_exec_alignment_audit_v1 import validate_forward_event

    evidence = evidence_row()
    source = {
        "event_key": evidence["event_key"],
        "symbol": evidence["symbol"],
        "direction": "SHORT",
        "signal_time": evidence["signal_time"],
        "reference_price": evidence["reference_price"],
        "stop_price": evidence["stop_price"],
        "target_price": evidence["target_price"],
        "no_lookahead_flags": ["recent_high_window_closed_before_signal"],
        "source_quality_flags": [],
        "missing_fields": [],
    }
    lineage = {
        **source,
        "link_status": "LINKED",
        "event_key_integrity": True,
        "phase1_linked": True,
        "source_signal_present": True,
        "phase1_event_key": evidence["event_key"],
    }
    outcome = {
        **source,
        "outcome_status": "MATCHED_FINALIZED",
        "is_final": True,
        "coverage_complete": True,
        "asof_ok": True,
        "horizon_end_time": SIGNAL + timedelta(minutes=240),
        "finalization_candle_close_time": SIGNAL + timedelta(minutes=240),
        "finalized_at": SIGNAL + timedelta(minutes=241),
        "path_class": "TARGET_HIT",
        "exit_price": 95.0,
        "source_quality_flags": [],
        "missing_fields": [],
    }
    now = SIGNAL + timedelta(hours=5)

    valid = validate_forward_event(evidence, source, lineage, outcome, now)
    assert valid["eligible"] is True

    invalid_source = {
        **source,
        "no_lookahead_flags": [
            "recent_high_window_closed_before_signal",
            "UNEXPECTED_LOOKAHEAD_FLAG",
        ],
    }
    invalid = validate_forward_event(
        evidence, invalid_source, lineage, outcome, now
    )
    assert invalid["eligible"] is False
    assert "SOURCE_LOOKAHEAD_FLAGS" in invalid["reasons"]

def test_feasibility_requires_sufficient_verified_exchange_constraints():
    def make_evidence(known):
        return {
            "feasibility_status": "OK",
            "scenario_results": {
                "scenarios": [{
                    "status": "OK",
                    "constraints_known": known,
                    "below_min_qty": False,
                    "below_min_notional": False,
                }]
            },
        }

    for verified_n in (0, 1):
        rows = (
            [make_evidence(True) for _ in range(verified_n)]
            + [make_evidence(False) for _ in range(30 - verified_n)]
        )

        result = fill_feasibility_verdict(rows)

        assert result["verdict"] == FEAS_INCONCLUSIVE
        assert result["n_quote_valid"] == 30
        assert any(
            "constraints" in reason.lower()
            for reason in result["reasons"]
        )

def test_alignment_requires_positive_control_confidence_interval():
    rows = [
        _base_view(
            f"S{i:03d}USDT",
            1.0 if i % 2 == 0 else -0.95,
            1.5,
        )
        for i in range(200)
    ]

    agg = aggregate(rows, bootstrap_n=200, seed=7)
    control_ci = agg["control_net_r"]["bootstrap"]
    executable_ci = agg["executable_net_r_frozen_exit"]["bootstrap"]

    assert agg["usable_paired_rows"] == 200
    assert agg["symbols"] >= 20
    assert agg["control_net_r"]["point_estimate"] > 0
    assert control_ci["ci_95_low"] <= 0
    assert executable_ci["ci_95_low"] > 0

    verdict = alignment_verdict(rows, agg)
    assert verdict["verdict"] == ALIGNMENT_INCONCLUSIVE

@pytest.mark.parametrize("fill", [94.0, 95.0])
def test_short_entry_at_or_below_target_is_invalid(fill):
    cf = counterfactual(evidence_row(), outcome_row(), fill, 1000)

    assert cf["control"]["gross_r"] is not None
    assert cf["executable"]["status"] == "INVALID_GEOMETRY"
    assert cf["executable"]["gross_r"] is None
    assert cf["executable"].get("net_r_frozen_exit") is None


def test_short_entry_above_target_remains_valid():
    cf = counterfactual(evidence_row(), outcome_row(), 95.01, 1000)

    assert cf["executable"]["status"] == "OK"
    assert cf["executable"]["gross_r"] is not None

"""Adversarial and integrity tests for the Issue #169 final paired verdict tooling.

Offline/pure: no database required. Covers the mandatory adversarial cases:
shared event ID, duplicate source IDs, cross-symbol identity collision, different
directions, baseline-after-Point-B, one-to-many mapping, single-cohort finalized,
Point-B-only, baseline-only, pre-freeze, freeze-equal, immature outcome,
negative/zero structural risk, SHORT timeout sign, 2R target consistency,
STOP_FIRST ambiguity, 240m cutoff, cluster-bootstrap reproducibility,
leave-one-day-out, missing/incomplete day, source count conservation and
deterministic report re-run.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

try:  # repository layout (tools/research/...)
    from tools.research import htf_point_b_final_paired_snapshot_and_report_v1 as tool
except ImportError:  # flat isolated-output layout used on the VPS
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import snapshot_and_report_v1 as tool  # noqa: E402

FREEZE = tool.FREEZE
POINT_B = tool.POINT_B
BASELINE = tool.BASELINE
T0 = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)


def make_row(exp, *, event="touch-1", direction="SHORT", symbol="BTCUSDT",
             time=T0, obs=1, final=True, r=1.0, is_short=True,
             entry=100.0, stop=110.0, target=80.0, policy="STOP_FIRST",
             max_hold=240, tp=False, sl=False, ambiguous=False,
             evaluated=None):
    return {
        "observation_id": obs, "experiment_id": exp, "symbol": symbol,
        "direction": direction, "signal_time": time, "setup_event_id": event,
        "frozen_intrabar_policy": policy, "frozen_max_hold": max_hold,
        "source_signal_id": obs, "source_observation_id": None,
        "reference_price": entry, "invalidation_price": stop, "target_1": target,
        "variant_entry": None, "variant_stop": None, "variant_target": None,
        "has_outcome": True, "is_final": final, "return_at_240m": r,
        "tp_hit": tp, "sl_hit": sl, "tp_before_sl": tp, "sl_before_tp": sl,
        "ambiguous_intrabar": ambiguous, "time_to_tp": None, "time_to_sl": None,
        "evaluated_240m_at": evaluated if evaluated is not None
        else (time + timedelta(minutes=240)),
        "outcome_updated_at": time + timedelta(minutes=240),
        "observation_created_at": time,
    }


def meta_asof(asof_iso):
    return {
        "SNAPSHOT_ASOF_UTC": asof_iso,
        "db_now": asof_iso,
        "query_identity": {"observations": "x"},
        "session": {"current_database": "trad_bot", "current_user": "u",
                    "transaction_isolation": "repeatable read",
                    "transaction_read_only": "on"},
        "source_row_counts": {"raw_joined_rows": 0},
        "snapshot_rows_sha256": "y",
        "frozen_geometry": {"freeze_ts": FREEZE.isoformat()},
    }


ASOF = meta_asof("2026-10-21T13:20:00+00:00")

SHORT_FEATURES = dict(direction="SHORT", entry=100.0, stop=110.0, target=80.0)


# ── 1. shared event id matching ─────────────────────────────────────

def test_shared_event_id_matching_and_delta():
    rows = [
        make_row(BASELINE, obs=1, time=T0, r=2.0),
        make_row(POINT_B, obs=2, time=T0 + timedelta(minutes=25), r=1.0),
    ]
    pairs, unmatched, funnel, audit, _ = tool.match_and_classify(rows, _asof_dt())
    assert len(pairs) == 1
    # SHORT timeout: R = -ret/10 with entry=100, stop=110 -> baseline -0.2, point-b -0.1
    assert pairs[0]["baseline_r"] == pytest.approx(-0.2)
    assert pairs[0]["point_b_r"] == pytest.approx(-0.1)
    assert pairs[0]["delta_r"] == pytest.approx(0.1)
    assert funnel["matched_finalized"] == 1
    assert funnel["conservation_ok"] is True


def _asof_dt():
    return datetime.fromisoformat(ASOF["SNAPSHOT_ASOF_UTC"])


# ── 2/6. duplicate source ids / one-to-many mapping ─────────────────

def test_duplicate_and_one_to_many_fail_closed():
    rows = [
        make_row(BASELINE, obs=1),
        make_row(BASELINE, obs=2),
        make_row(POINT_B, obs=3),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["duplicate_identity"] == 1
    assert unmatched[0]["reason"] == "duplicate_identity"
    assert funnel["conservation_ok"] is True


# ── 3. cross-symbol identity collision ──────────────────────────────

def test_cross_symbol_identity_collision_detected():
    rows = [
        make_row(BASELINE, obs=1, symbol="BTCUSDT", event="touch-x"),
        make_row(POINT_B, obs=2, symbol="ETHUSDT", event="touch-x",
                 time=T0 + timedelta(minutes=30)),
    ]
    pairs, unmatched, funnel, _, diag = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["symbol_or_direction_mismatch"] == 1
    assert funnel["cross_symbol_event_ids"] == 1
    assert diag["cross_symbol_event_ids"] == ["touch-x"]


# ── 4. different directions ─────────────────────────────────────────

def test_cross_direction_does_not_pair():
    rows = [
        make_row(BASELINE, obs=1, direction="LONG", entry=100.0, stop=90.0, target=120.0),
        make_row(POINT_B, obs=2, direction="SHORT"),
    ]
    pairs, unmatched, funnel, _, diag = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["baseline_only"] == 1 and funnel["point_b_only"] == 1
    assert funnel["cross_direction_event_ids"] == 1
    assert diag["cross_direction_event_ids"] == ["touch-1"]


# ── 5. baseline after point-b ───────────────────────────────────────

def test_causality_violation_fails_closed():
    rows = [
        make_row(BASELINE, obs=1, time=T0 + timedelta(minutes=30)),
        make_row(POINT_B, obs=2, time=T0),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["baseline_after_point_b"] == 1
    assert funnel["invalid_lineage"] == 1


# ── 7. only one cohort finalized ────────────────────────────────────

def test_single_cohort_finalized_excluded():
    rows = [
        make_row(BASELINE, obs=1, final=False, r=None),
        make_row(POINT_B, obs=2, time=T0 + timedelta(minutes=10)),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["matched_excluded"] == 1
    assert "baseline:not_finalized" in unmatched[0]["detail"]


# ── 8/9. one-sided events ───────────────────────────────────────────

def test_point_b_only_and_baseline_only_events():
    rows = [
        make_row(BASELINE, event="only-bl", obs=1),
        make_row(POINT_B, event="only-pb", obs=2),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["baseline_only"] == 1 and funnel["point_b_only"] == 1
    assert {u["reason"] for u in unmatched} == {"baseline_only_event", "point_b_only_event"}
    assert funnel["conservation_ok"] is True


# ── 10/11. pre-freeze and freeze-equal signals ──────────────────────

def test_prefreeze_and_freeze_equal_rejected():
    rows = [
        make_row(POINT_B, event="early", obs=1, time=FREEZE - timedelta(minutes=1)),
        make_row(POINT_B, event="exact", obs=2, time=FREEZE),
        make_row(POINT_B, event="ok", obs=3, time=FREEZE + timedelta(minutes=1)),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert funnel["prefreeze_rows"] == 2
    assert funnel["point_b_only"] == 1
    assert not pairs


# ── 12. immature outcome ────────────────────────────────────────────

def test_immature_outcome_not_final_pair():
    late = _asof_dt() - timedelta(minutes=60)  # 240m horizon not yet elapsed
    rows = [
        make_row(BASELINE, obs=1, time=late, final=False, r=None),
        make_row(POINT_B, obs=2, time=late + timedelta(minutes=5), final=False, r=None),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert not pairs
    assert funnel["immature"] == 1
    assert "immature" in unmatched[0]["detail"]


# ── 13. negative/zero structural risk ───────────────────────────────

def test_negative_and_zero_risk_rejected():
    assert tool.frozen_gross_r("SHORT", 100.0, 100.0, 80.0, 1.0,
                               False, False, False, "STOP_FIRST", 240)[1] == "bad_directional_geometry"
    # stop on the wrong side for the direction
    assert tool.frozen_gross_r("SHORT", 100.0, 90.0, 80.0, 1.0,
                               False, False, False, "STOP_FIRST", 240)[1] == "bad_directional_geometry"
    assert tool.frozen_gross_r("LONG", 100.0, 110.0, 120.0, 1.0,
                               False, False, False, "STOP_FIRST", 240)[1] == "bad_directional_geometry"


# ── 14. SHORT timeout sign ──────────────────────────────────────────

def test_short_and_long_timeout_sign_normalization():
    value, path = tool.frozen_gross_r("SHORT", 100.0, 110.0, 80.0, 1.0,
                                      False, False, False, "STOP_FIRST", 240)
    assert path == "TIMEOUT"
    assert value == pytest.approx(-0.1)  # price +1% is adverse for SHORT
    value, path = tool.frozen_gross_r("LONG", 100.0, 90.0, 120.0, 2.0,
                                      False, False, False, "STOP_FIRST", 240)
    assert path == "TIMEOUT"
    assert value == pytest.approx(0.2)


# ── 15. 2R target consistency ───────────────────────────────────────

def test_target_must_be_exactly_2r():
    assert tool.frozen_gross_r("SHORT", 100.0, 110.0, 85.0, 1.0,
                               False, False, False, "STOP_FIRST", 240)[1] == "target_not_2R"
    value, path = tool.frozen_gross_r("SHORT", 100.0, 110.0, 80.0, 1.0,
                                      True, False, False, "STOP_FIRST", 240)
    assert (value, path) == (2.0, "TP_FIRST")


# ── 16. STOP_FIRST ambiguity ────────────────────────────────────────

def test_stop_first_ambiguity_resolves_to_stop():
    assert tool.frozen_gross_r("SHORT", 100.0, 110.0, 80.0, 1.0,
                               False, False, True, "STOP_FIRST", 240) == (-1.0, "AMBIGUOUS_STOP_FIRST")
    assert tool.frozen_gross_r("SHORT", 100.0, 110.0, 80.0, 1.0,
                               True, True, True, "STOP_FIRST", 240) == (-1.0, "AMBIGUOUS_STOP_FIRST")
    assert tool.frozen_gross_r("SHORT", 100.0, 110.0, 80.0, 1.0,
                               False, False, False, "TP_FIRST", 240)[1] == "non_frozen_policy"


# ── 17. 240m cutoff behaviour ───────────────────────────────────────

def test_240m_cutoff_and_premature_finalization_audit():
    # final row evaluated before signal+240m is an integrity violation
    rows = [
        make_row(POINT_B, obs=1, time=T0,
                 evaluated=T0 + timedelta(minutes=100)),
    ]
    _, _, _, audit, _ = tool.match_and_classify(rows, _asof_dt())
    assert audit.get("premature_finalization") == 1
    # non-frozen max_hold fails closed
    assert tool.frozen_gross_r("SHORT", 100.0, 110.0, 80.0, 1.0,
                               False, False, False, "STOP_FIRST", 120)[1] == "non_frozen_policy"


# ── 18. cluster-bootstrap reproducibility ───────────────────────────

def test_cluster_bootstrap_reproducible_and_seed_stable():
    items = [{"day": f"2026-10-{d:02d}", "symbol": "S", "delta_r": float(d - 7)}
             for d in range(1, 15) for _ in range(3)]
    a = tool.cluster_bootstrap_ci(items, "day", 500, 169)
    b = tool.cluster_bootstrap_ci(items, "day", 500, 169)
    assert a == b
    assert a["clusters"] == 14
    assert a["ci95"] is not None
    assert a["kish_effective_clusters"] == pytest.approx(14.0)


# ── 19/20. LODO and missing/incomplete day ──────────────────────────

def test_leave_one_day_out_and_single_day_edge():
    items = []
    for d in ("2026-10-07", "2026-10-08", "2026-10-09"):
        for i in range(5):
            items.append(_pair(d, "S1", delta=1.0 if d == "2026-10-09" else -1.0))
    stats = tool.directional_stats(items, reps=200, seed=169)
    assert stats["leave_one_day_out_mean_delta_r"]["2026-10-09"] < stats["paired_delta_mean_r"]
    assert stats["leave_one_day_out_mean_delta_r"]["2026-10-07"] > stats["paired_delta_mean_r"]
    single = [_pair("2026-10-07", "S1", delta=0.5) for _ in range(4)]
    stats1 = tool.directional_stats(single, reps=200, seed=169)
    assert stats1["leave_one_day_out_mean_delta_r"] == {"2026-10-07": None}
    assert stats1["day_cluster"]["clusters"] == 1
    assert stats1["day_cluster"]["ci95"] is None
    assert stats1["boundary_sensitivity"]["drop_first_day_mean_delta_r"] is None


def _pair(day, symbol, delta, direction="SHORT"):
    return {"event_id": f"{day}-{symbol}-{delta}", "direction": direction,
            "symbol": symbol, "day": day, "delta_r": delta,
            "point_b_r": delta, "baseline_r": 0.0, "delay_minutes": 30.0,
            "point_b_exit": "TIMEOUT", "baseline_exit": "TIMEOUT",
            "point_b_entry_price": 100.0, "baseline_entry_price": 100.0,
            "entry_price_diff": 0.0, "point_b_risk": 10.0, "baseline_risk": 10.0,
            "risk_diff": 0.0}


# ── 21. source count conservation ───────────────────────────────────

def test_source_count_conservation():
    rows = [
        make_row(BASELINE, event="m", obs=1),
        make_row(POINT_B, event="m", obs=2, time=T0 + timedelta(minutes=10)),
        make_row(BASELINE, event="bl-only", obs=3),
        make_row(POINT_B, event="pb-only", obs=4),
        make_row(BASELINE, event="dup", obs=5),
        make_row(BASELINE, event="dup", obs=6),
        make_row(POINT_B, event="dup", obs=7),
        make_row(BASELINE, event="late", obs=8, time=T0 + timedelta(minutes=50)),
        make_row(POINT_B, event="late", obs=9, time=T0 + timedelta(minutes=10)),
    ]
    pairs, unmatched, funnel, _, _ = tool.match_and_classify(rows, _asof_dt())
    assert len(pairs) == 1
    accounted = (funnel["matched_finalized"] + funnel["matched_excluded"]
                 + funnel["baseline_only"] + funnel["point_b_only"]
                 + funnel["duplicate_identity"] + funnel["symbol_or_direction_mismatch"]
                 + funnel["baseline_after_point_b"])
    assert accounted == funnel["source_event_identities"] == 5
    assert funnel["conservation_ok"] is True


# ── 22. deterministic report re-run ─────────────────────────────────

def _tiny_dataset():
    rows = []
    obs = 0
    for d in range(7, 12):
        day = datetime(2026, 10, d, 9, 0, tzinfo=timezone.utc)
        for direction, entry, stop, target in (("SHORT", 100.0, 110.0, 80.0),
                                               ("LONG", 100.0, 90.0, 120.0)):
            for i in range(4):
                obs += 1
                ev = f"e{d}-{direction}-{i}"
                rows.append(make_row(BASELINE, event=ev, direction=direction, obs=obs,
                                     time=day, r=float(i), entry=entry, stop=stop,
                                     target=target))
                rows.append(make_row(POINT_B, event=ev, direction=direction, obs=obs + 1000,
                                     time=day + timedelta(minutes=20), r=float(i) - 1.0,
                                     entry=entry, stop=stop, target=target))
    return rows


def test_deterministic_report_rerun(tmp_path):
    rows = _tiny_dataset()
    outs = []
    for tag in ("a", "b"):
        outdir = tmp_path / tag
        outdir.mkdir()
        summary, details = tool.analyze(rows, ASOF, reps=500, seed=169)
        tool.write_artifacts(outdir, summary, details, ASOF,
                             {"research_head": "deadbeef", "production_head": "cafebabe"})
        outs.append({p.name: tool._sha256_file(p) for p in sorted(outdir.iterdir())})
    assert outs[0] == outs[1]
    # byte-level portability: artifacts must be LF-only
    for p in sorted((tmp_path / "a").iterdir()):
        assert b"\r\n" not in p.read_bytes(), f"CRLF in {p.name}"
    # both directions must carry verdicts and funnel conservation must hold
    summary, _ = tool.analyze(rows, ASOF, reps=500, seed=169)
    assert summary["funnel"]["conservation_ok"] is True
    assert summary["verdicts"]["LONG"]["verdict"] in {"PROMISING", "INCONCLUSIVE", "NO_EDGE", "BLOCKED"}
    assert summary["verdicts"]["SHORT"]["verdict"] in {"PROMISING", "INCONCLUSIVE", "NO_EDGE", "BLOCKED"}
    # deterministic JSON bytes
    j1 = json.dumps(summary, sort_keys=True)
    j2 = json.dumps(tool.analyze(rows, ASOF, reps=500, seed=169)[0], sort_keys=True)
    assert j1 == j2


# ── negative tests: malformed snapshot and missing required fields ──

def _write_snapshot_csv(path, rows, fields=None):
    import csv
    fields = fields or tool.ROW_FIELDS
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def test_malformed_snapshot_missing_columns_fails_closed(tmp_path):
    rows = [make_row(POINT_B, obs=1)]
    _write_snapshot_csv(tmp_path / tool.SNAPSHOT_ROWS_FILE, rows,
                        fields=[f for f in tool.ROW_FIELDS if f != "setup_event_id"])
    with pytest.raises(ValueError, match="missing required"):
        tool.load_rows(tmp_path)


def test_malformed_snapshot_meta_missing_asof_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="SNAPSHOT_ASOF_UTC"):
        tool.analyze([], {}, reps=100, seed=169)
    with pytest.raises(ValueError, match="SNAPSHOT_ASOF_UTC"):
        tool.analyze([], {"SNAPSHOT_ASOF_UTC": ""}, reps=100, seed=169)


def test_naive_timestamp_in_snapshot_fails_closed(tmp_path):
    row = make_row(POINT_B, obs=1)
    row["signal_time"] = "2026-10-07T09:00:00"  # naive: no timezone
    _write_snapshot_csv(tmp_path / tool.SNAPSHOT_ROWS_FILE, [row])
    with pytest.raises(ValueError, match="naive timestamp"):
        tool.load_rows(tmp_path)


def test_report_refuses_artifact_overwrite_without_approval(tmp_path):
    rows = _tiny_dataset()
    summary, details = tool.analyze(rows, ASOF, reps=200, seed=169)
    tool.write_artifacts(tmp_path, summary, details, ASOF,
                         {"research_head": "x", "production_head": "y"})
    with pytest.raises(RuntimeError, match="refusing to overwrite canonical"):
        tool.write_artifacts(tmp_path, summary, details, ASOF,
                             {"research_head": "x", "production_head": "y"})
    # explicit approval flag permits an intentional rerun
    tool.write_artifacts(tmp_path, summary, details, ASOF,
                         {"research_head": "x", "production_head": "y"},
                         allow_overwrite=True)


def test_capture_refuses_nonempty_output_dir(tmp_path):
    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not empty"):
        tool.run_capture(Path("/nonexistent-worktree"), Path("/nonexistent-root"), tmp_path)
    # snapshot files are never overwritten even with explicit approval
    _write_snapshot_csv(tmp_path / tool.SNAPSHOT_ROWS_FILE, [make_row(POINT_B, obs=1)])
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        tool.run_capture(Path("/nonexistent-worktree"), Path("/nonexistent-root"), tmp_path,
                         allow_nonempty=True)


def test_snapshot_checksum_mismatch_fails_closed(tmp_path):
    _write_snapshot_csv(tmp_path / tool.SNAPSHOT_ROWS_FILE, [make_row(POINT_B, obs=1)])
    (tmp_path / tool.SNAPSHOT_META_FILE).write_text(json.dumps({
        "SNAPSHOT_ASOF_UTC": "2026-10-21T13:40:50+00:00",
        "snapshot_rows_sha256": tool._sha256_file(tmp_path / tool.SNAPSHOT_ROWS_FILE),
    }), encoding="utf-8")
    rows, meta = tool.load_snapshot(tmp_path)  # consistent snapshot loads fine
    assert len(rows) == 1
    # rows edited/truncated after capture: report must fail closed, not re-attribute
    _write_snapshot_csv(tmp_path / tool.SNAPSHOT_ROWS_FILE, [])
    with pytest.raises(ValueError, match="checksum mismatch"):
        tool.load_snapshot(tmp_path)


def test_snapshot_missing_recorded_checksum_fails_closed(tmp_path):
    _write_snapshot_csv(tmp_path / tool.SNAPSHOT_ROWS_FILE, [make_row(POINT_B, obs=1)])
    (tmp_path / tool.SNAPSHOT_META_FILE).write_text(
        json.dumps({"SNAPSHOT_ASOF_UTC": "2026-10-21T13:40:50+00:00"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing snapshot_rows_sha256"):
        tool.load_snapshot(tmp_path)


def test_empty_eligible_cohort_extrema_are_null():
    # capture with zero post-freeze observations must serialize null, not crash
    assert tool.eligible_extrema([]) == (None, None)
    earliest, latest = tool.eligible_extrema([
        {"signal_time": "2026-10-07T09:00:00+00:00"},
        {"signal_time": "2026-10-08T09:00:00+00:00"},
    ])
    assert earliest == "2026-10-07T09:00:00+00:00"
    assert latest == "2026-10-08T09:00:00+00:00"


# ── parity with the frozen branch implementation (when available) ───

def test_parity_with_branch_frozen_gross_r():
    try:
        from tools.research.htf_point_b_paired_report_v1 import frozen_gross_r as branch_calc
    except Exception:
        pytest.skip("branch tooling not importable in this environment")
    feat = {"_frozen_intrabar_policy": "STOP_FIRST", "_frozen_max_hold": 240}
    cases = [
        ("SHORT", 100.0, 110.0, 80.0, 1.0, False, False, False),
        ("SHORT", 100.0, 110.0, 80.0, 1.0, True, False, False),
        ("SHORT", 100.0, 110.0, 80.0, 1.0, False, True, False),
        ("SHORT", 100.0, 110.0, 80.0, 1.0, False, False, True),
        ("LONG", 100.0, 90.0, 120.0, 2.0, False, False, False),
        ("LONG", 100.0, 90.0, 115.0, 2.0, True, False, False),
    ]
    for direction, entry, stop, target, ret, tp, sl, amb in cases:
        mine = tool.frozen_gross_r(direction, entry, stop, target, ret, tp, sl, amb,
                                   "STOP_FIRST", 240)
        theirs = branch_calc(direction, entry, stop, target, ret, tp, sl, amb, feat)
        assert mine == theirs, (direction, entry, stop, target, ret, tp, sl, amb)

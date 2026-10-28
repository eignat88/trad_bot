"""Regression suite for the ME SHORT A executable-entry counterfactual (PR #211).

Covers the corrective-review matrix: TP_FIRST / SL_FIRST / STOP_FIRST
ambiguity, missing intermediate candles, duplicate and conflicting OHLC,
unclosed cutoff candles, 5m-boundary and microsecond signals, missing/stale
orderbook, impossible position sizes, normal vs elevated execution costs and
the immutable QUSDT fixture (2 TP / 1 SL, frozen semantics unchanged).
"""
import csv
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.research.me_short_a_execution_alignment_counterfactual_v1 import (
    CONFLICTING_CANDLE, ELEVATED_COST, EXECUTION_INFEASIBLE, EXECUTION_MODELED,
    EXECUTION_UNKNOWN, FINAL, INVALID_GEOMETRY, MISSING_CANDLE,
    MISSING_ENTRY, NORMAL_COST, PATH_SL, PATH_STOP_FIRST, PATH_TIMEOUT, PATH_TP,
    SOURCE_INCOMPLETE_COVERAGE, TIMEOUT_CLOSE_UNAVAILABLE, assess_execution_feasibility,
    audit, build_integrity, cluster_bootstrap, cost_decomposition, event_key,
    evaluate, paired_comparison, report, ts,
)

D = Decimal
FIX = Path(__file__).parent / "fixtures"


def data(filename):
    with (FIX / filename).open(newline="") as file:
        return list(csv.DictReader(file))


# ─── synthetic builders ──────────────────────────────────────────────────────

SYMBOL = "TSTUSDT"
SIGNAL_TIME = "2026-10-27T02:37:05.590912+00:00"  # mid-candle, microseconds
GRID_SIGNAL_TIME = "2026-10-27T02:45:00+00:00"    # exactly on the 5m grid
MICRO_SIGNAL_TIME = "2026-10-27T02:45:00.500000+00:00"


def signal(**over):
    row = {"symbol": SYMBOL, "signal_time": SIGNAL_TIME,
           "reference_price": "0.205", "estimated_fill": "0.20",
           "stop_price": "0.21", "target_price": "0.10"}
    row.update(over)
    return row


def candle(open_time, *, high="0.16", low="0.14", open_="0.15", close="0.15",
           closed="true", symbol=SYMBOL):
    if isinstance(open_time, datetime):
        open_time = open_time.isoformat()
    return {"symbol": symbol, "open_time": open_time, "open": open_,
            "high": high, "low": low, "close": close, "is_closed": closed}


def series(start_iso, count, *, symbol=SYMBOL, closed="true"):
    start = datetime.fromisoformat(start_iso)
    return [candle(start + timedelta(minutes=5 * i), closed=closed, symbol=symbol)
            for i in range(count)]


def series_between(first_iso, last_iso, **kw):
    first, last = datetime.fromisoformat(first_iso), datetime.fromisoformat(last_iso)
    count = int((last - first).total_seconds() // 300) + 1
    return series(first_iso, count, **kw)


# Mid-candle signal: window 02:40..06:35, hit bars up to 06:30.
MID_FIRST = "2026-10-27T02:40:00+00:00"
# Grid signal 02:45: window 02:50..06:40 (timeout close = 06:40 close).
GRID_FIRST = "2026-10-27T02:50:00+00:00"
GRID_LAST = "2026-10-27T06:40:00+00:00"
# Microsecond signal 02:45:00.5: window 02:50..06:45 (straddle bar 06:45).
MICRO_LAST = "2026-10-27T06:45:00+00:00"


# ─── immutable QUSDT fixture: 2 TP / 1 SL, frozen semantics unchanged ────────

def test_qusdt_2_tp_1_sl_with_fixed_absolute_stop_and_target():
    result = audit(data("me_short_a_issue206_qusdt_signals.csv"),
                   data("me_short_a_issue206_qusdt_candles.csv"))
    actual = [x["executable"] for x in result]
    assert [x["status"] for x in actual] == [FINAL] * 3
    assert [x["path"] for x in actual] == [PATH_TP, PATH_TP, PATH_SL]
    assert [x["exit_candle"][11:16] for x in actual] == ["06:30", "06:30", "03:25"]
    assert round(sum(float(x["net_r"]) for x in actual), 2) == 3.26
    # Control must stay untouched (separate frozen price normalization).
    assert all(x["frozen"]["status"] == FINAL for x in result)
    assert all(x["frozen"]["path"] == p for x, p in zip(result, [PATH_TP, PATH_TP, PATH_SL]))
    # Elevated costs can only be worse than normal costs, never better.
    for x in actual:
        assert D(x["net_r_elevated"]) < D(x["net_r_normal"]) == D(x["net_r"])
    # The fixture carries no market-microstructure evidence: the modeled
    # entries are NOT automatically executable trades.
    assert all(x["execution_feasibility"]["status"] == EXECUTION_UNKNOWN for x in result)
    assert all("MISSING_EXECUTION_EVIDENCE" in x["execution_feasibility"]["reasons"]
               for x in result)


def test_qusdt_event_key_matches_frozen_lineage_identity():
    from app.research.me_short_a_source_intake_v1 import (
        compute_event_key as phase1_event_key)
    sig = data("me_short_a_issue206_qusdt_signals.csv")[0]
    mine = event_key(sig["symbol"], sig["signal_time"], sig["reference_price"],
                     sig["stop_price"], sig["target_price"])
    assert mine == phase1_event_key(sig["symbol"], sig["signal_time"],
                                    sig["reference_price"], sig["stop_price"],
                                    sig["target_price"])
    # The #212 spelling contract: every representation of one number hashes
    # the same, so writer and reader always join on one key.
    assert mine == event_key(sig["symbol"], sig["signal_time"],
                             float(sig["reference_price"]),
                             float(sig["stop_price"]), float(sig["target_price"]))


# ─── horizon / timeout status separation (P1.2) ──────────────────────────────

def test_missing_history_cannot_be_counted_as_profit():
    s = signal()
    result = evaluate(s, [], entry_key="estimated_fill")
    assert result["status"] == SOURCE_INCOMPLETE_COVERAGE
    assert result["reason"] == "NO_CANDLES"


def test_missing_gap_cannot_be_skipped():
    s = data("me_short_a_issue206_qusdt_signals.csv")[0]
    candles = data("me_short_a_issue206_qusdt_candles.csv")
    candles = [c for c in candles if "T03:20:" not in c["open_time"]]
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == MISSING_CANDLE
    assert result["reason"] == "GAP_IN_HISTORY"


def test_truncated_tail_is_incomplete_coverage_not_gap():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == SOURCE_INCOMPLETE_COVERAGE
    assert result["reason"] == "SOURCE_ENDS_BEFORE_HORIZON"


def test_microsecond_signal_timeout_close_unavailable_with_full_history():
    s = signal(signal_time=MICRO_SIGNAL_TIME)
    candles = series_between(GRID_FIRST, MICRO_LAST)
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == TIMEOUT_CLOSE_UNAVAILABLE
    assert result["reason"] == "TIMEOUT_MARK_MID_CANDLE"


def test_grid_signal_timeout_is_final_at_cutoff_close():
    s = signal(signal_time=GRID_SIGNAL_TIME)
    candles = series_between(GRID_FIRST, GRID_LAST)
    last_close = candles[-1]["close"]
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == FINAL
    assert result["path"] == PATH_TIMEOUT
    assert result["exit_candle"] == GRID_LAST
    assert result["exit"] == last_close


def test_grid_signal_with_microseconds_is_not_final():
    s = signal(signal_time="2026-10-27T02:45:00.000001+00:00")
    candles = series_between(GRID_FIRST, MICRO_LAST)
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == TIMEOUT_CLOSE_UNAVAILABLE


def test_unclosed_cutoff_candle_is_never_finalized():
    s = signal(signal_time=GRID_SIGNAL_TIME)
    candles = series_between(GRID_FIRST, GRID_LAST, closed="true")
    candles[-1]["is_closed"] = "false"  # the bar ending exactly at the cutoff
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == SOURCE_INCOMPLETE_COVERAGE
    assert result["reason"] == "CUTOFF_CANDLE_UNCLOSED"
    assert result["excluded_rows_at_slot"] == {"UNCLOSED_CANDLE": 1}


def test_future_candles_at_export_time_are_excluded():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T04:00:00+00:00")
    result = evaluate(s, candles, entry_key="estimated_fill",
                      as_of=ts("2026-10-27T03:10:00+00:00"))
    assert result["status"] == SOURCE_INCOMPLETE_COVERAGE
    assert result["excluded_rows_at_slot"] == {"FUTURE_OR_UNCLOSED_CANDLE": 1}


# ─── TP_FIRST / SL_FIRST / STOP_FIRST ambiguity (frozen semantics) ───────────

def test_tp_first():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    candles.append(candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.09"))
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == FINAL
    assert result["path"] == PATH_TP
    assert result["exit_candle"] == "2026-10-27T03:05:00+00:00"
    assert result["exit"] == "0.10"


def test_sl_first():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:20:00+00:00")
    candles.append(candle("2026-10-27T03:25:00+00:00", high="0.22", low="0.14"))
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == FINAL
    assert result["path"] == PATH_SL
    assert result["exit_candle"] == "2026-10-27T03:25:00+00:00"
    assert result["exit"] == "0.21"


def test_stop_first_ambiguous():
    s = data("me_short_a_issue206_qusdt_signals.csv")[0]
    c = dict(data("me_short_a_issue206_qusdt_candles.csv")[1])
    c.update(high="0.24", low="0.15")
    result = evaluate(s, [c], entry_key="estimated_fill")
    assert result["path"] == PATH_STOP_FIRST
    assert result["exit"] == s["stop_price"]  # conservative STOP_FIRST pricing


def test_stop_first_ambiguity_on_synthetic_bar():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    candles.append(candle("2026-10-27T03:05:00+00:00", high="0.22", low="0.09"))
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == FINAL
    assert result["path"] == PATH_STOP_FIRST
    assert result["exit"] == "0.21"


# ─── candle integrity (P1.1) ─────────────────────────────────────────────────

def test_identical_duplicates_are_collapsed_and_counted():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    hit = candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.09")
    candles.extend([dict(hit), dict(hit)])
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == FINAL
    assert result["path"] == PATH_TP
    integrity = build_integrity(candles, [SYMBOL])
    assert integrity["totals"]["duplicate_identical_rows"] == 1
    assert integrity["totals"]["conflicting_slots"] == 0


def test_conflicting_ohlc_returns_dedicated_status_not_first_record():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T04:00:00+00:00")
    contested = candle("2026-10-27T03:05:00+00:00", high="0.22", low="0.09")
    contested_other = candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.14")
    candles.append(contested)
    # The later row overwrites nothing: the slot must be reported, never
    # resolved to whichever record happens to come first.
    idx = [i for i, c in enumerate(candles) if c["open_time"] == contested["open_time"]][0]
    candles[idx] = contested_other
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == CONFLICTING_CANDLE
    assert result["conflict_open"] == "2026-10-27T03:05:00+00:00"
    integrity = build_integrity(candles, [SYMBOL])
    assert integrity["totals"]["conflicting_slots"] == 1


def test_conflict_beyond_resolved_exit_does_not_change_outcome():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    candles.append(candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.09"))
    # A contested slot strictly AFTER the resolving exit bar cannot change it.
    candles.append(candle("2026-10-27T03:10:00+00:00", high="0.155", low="0.14"))
    candles.append(candle("2026-10-27T03:10:00+00:00", high="0.13", low="0.12"))
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == FINAL  # exit resolved before the contested slot
    assert result["path"] == PATH_TP


def test_corrupt_candle_at_needed_slot_is_excluded_never_imputed():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T04:00:00+00:00")
    idx = [i for i, c in enumerate(candles) if c["open_time"] == "2026-10-27T03:05:00+00:00"][0]
    candles[idx] = candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.17")  # low > high
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == MISSING_CANDLE
    assert result["excluded_rows_at_slot"] == {"OHLC_INVALID": 1}


def test_off_grid_candle_is_excluded():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T04:00:00+00:00")
    idx = [i for i, c in enumerate(candles) if c["open_time"] == "2026-10-27T03:05:00+00:00"][0]
    candles[idx] = candle("2026-10-27T03:05:30+00:00")
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == MISSING_CANDLE
    integrity = build_integrity(candles, [SYMBOL])
    assert integrity["by_symbol"][SYMBOL]["exclusion_reasons"] == {"OFF_GRID_OPEN_TIME": 1}


def test_ohlc_bounds_include_open():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T04:00:00+00:00")
    idx = [i for i, c in enumerate(candles) if c["open_time"] == "2026-10-27T03:05:00+00:00"][0]
    candles[idx] = candle("2026-10-27T03:05:00+00:00",
                          high="0.16", low="0.14", open_="0.19", close="0.15")  # open > high
    result = evaluate(s, candles, entry_key="estimated_fill")
    assert result["status"] == MISSING_CANDLE
    assert result["excluded_rows_at_slot"] == {"OHLC_INVALID": 1}


def test_invalid_geometry_is_rejected():
    s = signal(target_price="0.25")  # target above entry for a SHORT
    result = evaluate(s, [], entry_key="estimated_fill")
    assert result["status"] == INVALID_GEOMETRY


# ─── execution feasibility (P1.3) ────────────────────────────────────────────

def book_signal(**over):
    signal_ms = int(datetime.fromisoformat(SIGNAL_TIME).timestamp() * 1000)
    row = signal(**{"reference_price": "0.20",  # exact qty math: 100 / 0.20 = 500
                    "estimated_fill": "0.199", "bid": "0.1995", "ask": "0.2",
                    "book_time_ms": str(signal_ms - 1000),
                    "depth_bid_10bps_usdt": "5000",
                    "requested_notional_usdt": "100",
                    "requested_qty": "500", "executable_qty": "500",
                    "min_order_qty": "1", "qty_step": "0.1",
                    "min_notional": "5"})
    row.update(over)
    return row


def test_execution_modeled_pass_is_not_confirmed_execution():
    feasibility = assess_execution_feasibility(book_signal())
    assert feasibility["status"] == EXECUTION_MODELED
    assert feasibility["confirmed_execution"] is False
    assert "MODELED_CHECKS_PASS_NOT_A_REAL_FILL" in feasibility["reasons"]


def test_missing_orderbook_is_execution_feasibility_unknown():
    feasibility = assess_execution_feasibility(signal())  # no book fields at all
    assert feasibility["status"] == EXECUTION_UNKNOWN
    assert "MISSING_EXECUTION_EVIDENCE" in feasibility["reasons"]


def test_stale_orderbook_is_execution_feasibility_unknown():
    signal_ms = int(datetime.fromisoformat(SIGNAL_TIME).timestamp() * 1000)
    feasibility = assess_execution_feasibility(
        book_signal(book_time_ms=str(signal_ms - 20000)))
    assert feasibility["status"] == EXECUTION_UNKNOWN
    assert "STALE_BOOK_SNAPSHOT" in feasibility["reasons"]


def test_assumed_latency_counts_into_quote_freshness():
    signal_ms = int(datetime.fromisoformat(SIGNAL_TIME).timestamp() * 1000)
    feasibility = assess_execution_feasibility(
        book_signal(book_time_ms=str(signal_ms - 500), assumed_latency_ms="20000"))
    assert feasibility["status"] == EXECUTION_UNKNOWN
    assert "STALE_BOOK_SNAPSHOT" in feasibility["reasons"]


def test_future_book_snapshot_is_rejected():
    signal_ms = int(datetime.fromisoformat(SIGNAL_TIME).timestamp() * 1000)
    feasibility = assess_execution_feasibility(
        book_signal(book_time_ms=str(signal_ms + 5000)))
    assert feasibility["status"] == EXECUTION_UNKNOWN
    assert "FUTURE_BOOK_SNAPSHOT" in feasibility["reasons"]
    at_export = assess_execution_feasibility(
        book_signal(), as_of=ts("2026-10-26T00:00:00+00:00"))
    assert at_export["status"] == EXECUTION_UNKNOWN
    assert "FUTURE_BOOK_SNAPSHOT" in at_export["reasons"]


def test_impossible_position_size_is_infeasible_not_executable():
    below_min = assess_execution_feasibility(book_signal(min_notional="200"))
    assert below_min["status"] == EXECUTION_INFEASIBLE
    assert "BELOW_EXCHANGE_MIN:min_notional" in below_min["reasons"]
    zero_qty = assess_execution_feasibility(
        book_signal(requested_notional_usdt="0.01", requested_qty="0.05",
                    executable_qty="0"))
    assert zero_qty["status"] == EXECUTION_INFEASIBLE
    assert "BELOW_EXCHANGE_MIN:executable_qty_rounds_to_zero" in zero_qty["reasons"]


def test_insufficient_depth_is_infeasible():
    feasibility = assess_execution_feasibility(book_signal(depth_bid_10bps_usdt="10"))
    assert feasibility["status"] == EXECUTION_INFEASIBLE
    assert "INSUFFICIENT_DEPTH" in feasibility["reasons"]


def test_fill_above_best_bid_marks_evidence_inconsistent():
    feasibility = assess_execution_feasibility(book_signal(bid="0.198"))
    assert feasibility["status"] == EXECUTION_UNKNOWN
    assert "FILL_INCONSISTENT_WITH_BOOK" in feasibility["reasons"]


# ─── costs: normal/elevated scenarios and no double-counting (P2.1/P2.2) ─────

def test_normal_and_elevated_cost_scenarios():
    s = signal()
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    candles.append(candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.09"))
    result = evaluate(s, candles, entry_key="estimated_fill")
    entry, exit_price, stop = D("0.2"), D("0.10"), D("0.21")
    risk = stop - entry
    gross = (entry - exit_price) / risk
    assert D(result["net_r_normal"]) == gross - NORMAL_COST * entry / risk
    assert D(result["net_r_elevated"]) == gross - ELEVATED_COST * entry / risk
    assert D(result["net_r_elevated"]) - D(result["net_r_normal"]) == \
        -(ELEVATED_COST - NORMAL_COST) * entry / risk
    assert D(result["net_r"]) == D(result["net_r_normal"])


def test_cost_decomposition_embeds_drift_and_impact_without_double_count():
    feasibility = assess_execution_feasibility(book_signal())
    decomposition = cost_decomposition(book_signal(), feasibility)
    assert D(decomposition["reference_to_bid_drift_bps"]) == D("25")
    assert D(decomposition["market_impact_bps"]) == \
        (D("0.1995") - D("0.199")) / D("0.1995") * 10000  # pure bid->fill walk
    assert decomposition["reference_to_bid_drift_accounting"] == \
        "EMBEDDED_IN_EXECUTABLE_ENTRY_NOT_ADDITIVE"
    assert decomposition["market_impact_accounting"] == \
        "EMBEDDED_IN_EXECUTABLE_ENTRY_NOT_ADDITIVE"
    assert decomposition["spread_accounting"] == "PART_OF_COST_RATE_ONCE"
    assert decomposition["fees_accounting"] == "PART_OF_COST_RATE_ONCE"
    assert decomposition["exit_slippage"] == "NOT_MODELED_UNKNOWN_NOT_ZERO"
    assert decomposition["cost_rate_normal_pct"] == str(NORMAL_COST)
    assert decomposition["cost_rate_elevated_pct"] == str(ELEVATED_COST)


def test_cost_decomposition_reports_unknown_not_zero_without_book():
    decomposition = cost_decomposition(signal(), assess_execution_feasibility(signal()))
    assert decomposition["reference_to_bid_drift_bps"] is None
    assert decomposition["market_impact_bps"] is None
    assert decomposition["spread_bps"] is None
    assert decomposition["exit_slippage"] == "NOT_MODELED_UNKNOWN_NOT_ZERO"


# ─── paired comparison with one event_key (P2.3/P2.6) ────────────────────────

def test_pairs_unified_event_key_and_missing_side_excluded():
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")
    candles.append(candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.09"))
    comparable_sig = signal()
    incomplete_sig = signal(reference_price="")  # frozen side lacks its entry
    rows = audit([comparable_sig, incomplete_sig], candles)
    assert len({row["event_key"] for row in rows}) == 2
    paired = paired_comparison(rows)
    assert paired["summary"]["n_pairs_total"] == 2
    assert paired["summary"]["n_comparable"] == 1
    assert paired["summary"]["n_excluded"] == 1
    assert paired["summary"]["excluded_reasons"] == {"FROZEN_DATA_MISSING:MISSING_ENTRY": 1}
    good = [p for p in paired["pairs"] if p["comparable"]][0]
    frozen_net = D(good["frozen_net_r_normal"])
    executable_net = D(good["executable_net_r_normal"])
    assert D(good["delta_net_r_normal"]) == executable_net - frozen_net
    bad = [p for p in paired["pairs"] if not p["comparable"]][0]
    assert bad["delta_net_r_normal"] is None  # never merged across a missing side


def test_pair_with_unresolved_outcome_is_not_combined():
    candles = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00")  # no hit yet
    rows = audit([signal()], candles)
    paired = paired_comparison(rows)
    assert paired["summary"]["n_comparable"] == 0
    assert paired["pairs"][0]["excluded_reason"] == \
        "FROZEN_DATA_MISSING:SOURCE_INCOMPLETE_COVERAGE+EXECUTABLE_DATA_MISSING:SOURCE_INCOMPLETE_COVERAGE"


# ─── coverage report (P2.4) ──────────────────────────────────────────────────

def test_coverage_report_by_symbol_and_day():
    day1 = series_between(MID_FIRST, "2026-10-27T03:00:00+00:00", symbol=SYMBOL)
    day1.append(candle("2026-10-27T03:05:00+00:00", high="0.15", low="0.09"))
    day2 = series_between("2026-10-28T02:40:00+00:00", "2026-10-28T03:00:00+00:00", symbol=SYMBOL)
    day2.append(candle("2026-10-28T03:05:00+00:00", high="0.15", low="0.09"))
    other = [dict(c, symbol="OTHERUSDT") for c in day1]
    candles = day1 + day2 + other
    signals = [signal(),
               signal(signal_time="2026-10-28T02:37:05+00:00"),
               signal(symbol="OTHERUSDT")]
    full = report(signals, candles, bootstrap_replicates=50, bootstrap_seed=1)
    coverage = full["coverage"]
    assert coverage["totals"]["events"] == 3
    assert set(coverage["by_symbol"]) == {SYMBOL, "OTHERUSDT"}
    assert coverage["by_symbol"][SYMBOL]["events"] == 2
    assert set(coverage["by_day"]) == {"2026-10-27", "2026-10-28"}
    assert coverage["by_day"]["2026-10-27"]["events"] == 2
    assert coverage["totals"]["frozen_status"] == {FINAL: 3}


# ─── cluster bootstrap over correlated repeats (P2.5) ────────────────────────

def _bootstrap_rows():
    rows = []
    for symbol, day, count in ((SYMBOL, "2026-10-27", 3), (SYMBOL, "2026-10-28", 1),
                               ("OTHERUSDT", "2026-10-27", 2)):
        for i in range(count):
            rows.append({"symbol": symbol,
                         "signal_time": f"{day}T0{i}:37:05+00:00"})
    return rows


def test_cluster_bootstrap_keeps_repeated_signals_together():
    rows = _bootstrap_rows()
    values = [D(str(i)) for i in range(len(rows))]
    result = cluster_bootstrap(values, rows, unit="test", replicates=200, seed=7)
    assert result["method"] == "CLUSTER_BOOTSTRAP_SYMBOL_UTC_DAY"
    assert result["n_events"] == 6
    assert result["n_clusters"] == 3  # symbol-day clusters, not individual signals
    assert result["mean"] == str(sum(values) / 6)
    assert D(result["ci95_low"]) <= D(result["ci95_high"])
    assert D(result["ci95_low"]) >= min(values)
    assert D(result["ci95_high"]) <= max(values)


def test_cluster_bootstrap_is_deterministic_for_a_seed():
    rows = _bootstrap_rows()
    values = [D(str(i)) for i in range(len(rows))]
    first = cluster_bootstrap(values, rows, unit="t", replicates=100, seed=7)
    second = cluster_bootstrap(values, rows, unit="t", replicates=100, seed=7)
    assert first == second


def test_cluster_bootstrap_single_cluster_degenerates_to_mean():
    rows = [{"symbol": SYMBOL, "signal_time": f"2026-10-27T0{i}:37:05+00:00"}
            for i in range(3)]
    values = [D("1"), D("2"), D("3")]
    result = cluster_bootstrap(values, rows, unit="t", replicates=50, seed=3)
    assert result["n_clusters"] == 1
    assert D(result["ci95_low"]) == D(result["mean"]) == D(result["ci95_high"])


def test_cluster_bootstrap_empty_population_is_explicit():
    result = cluster_bootstrap([], [], unit="t", replicates=50, seed=3)
    assert result["n_events"] == 0
    assert result["mean"] is None and result["ci95_low"] is None and result["ci95_high"] is None

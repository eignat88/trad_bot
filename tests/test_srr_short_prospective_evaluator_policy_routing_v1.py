from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.research.policies.srr_short_timeout_candle_policy_v1 import (
    CANDLE_MS,
    FROZEN_FREEZE_TS,
    SRR_EXPERIMENT_ID,
    evaluate_srr_short_timeout_candle_path,
)
from app.research.prospective_evaluator import ProspectiveOOSEvaluator
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SOURCE_CONFLICTING_CANDLES,
    SOURCE_FETCH_ERROR,
    SOURCE_INCOMPLETE_COVERAGE,
    SOURCE_TIMESTAMP_INVALID,
    SOURCE_VALIDATED,
    BybitHistoricalCandleSource,
    HistoricalArchiveCandleSource,
    HistoricalCandleSourceResult,
    SrrShortExecutionRExpansionProspectiveEvaluator,
    extract_frozen_geometry,
    validate_source_result,
)

FREEZE = datetime.fromisoformat(FROZEN_FREEZE_TS.replace("Z", "+00:00"))
SIGNAL = FREEZE.timestamp() * 1000 + 3 * 60_000
SIGNAL_MS = int(SIGNAL)
SIGNAL_OPEN = SIGNAL_MS // CANDLE_MS * CANDLE_MS
CUTOFF = SIGNAL_MS + 120 * 60_000
ASOF = CUTOFF + 120 * 60_000


def _dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _candle(offset_minutes: int, *, high=100.2, low=99.8, close=100.0, open_=100.0):
    return SimpleNamespace(
        timestamp=SIGNAL_OPEN + offset_minutes * 60_000,
        open=open_, high=high, low=low, close=close, volume=1.0,
    )


def _clean_candles(n=24):
    return [_candle(index * 5) for index in range(n)]


def _features(**overrides):
    features = {
        "_structural_r": 0.5,
        "_frozen_sl_r": 2.0,
        "_frozen_tp_r": 1.5,
        "_frozen_max_hold": 120,
        "_frozen_intrabar_policy": "STOP_FIRST",
        "_execution_r_abs": 1.0,
        "_execution_r_definition": "2.00 * structural_R",
        "_frozen_freeze_ts": FROZEN_FREEZE_TS,
    }
    features.update(overrides)
    return features


def _obs(**overrides):
    obs = {
        "observation_id": 1,
        "symbol": "AAAUSDT",
        "signal_time": _dt(SIGNAL_MS),
        "experiment_id": SRR_EXPERIMENT_ID,
        "direction": "SHORT",
        "reference_price": 100.0,
        "invalidation_price": 100.5,
        "target_1": 90.0,
        "target_2": 85.0,
        "variant_entry": 100.0,
        "variant_stop": 101.0,
        "variant_target": 99.25,
        "features": json.dumps(_features()),
    }
    obs.update(overrides)
    return obs


class _ArchiveSource(HistoricalArchiveCandleSource):
    def __init__(self, candles, status=SOURCE_VALIDATED):
        super().__init__({"AAAUSDT": candles}, archive_id="synthetic-archive")
        self.status = status
        self.requests = []

    def fetch_5m_candles(self, symbol, start_ms, end_ms, evaluation_asof_ms=None):
        self.requests.append((symbol, start_ms, end_ms, evaluation_asof_ms))
        result = super().fetch_5m_candles(symbol, start_ms, end_ms, evaluation_asof_ms)
        if self.status == SOURCE_VALIDATED:
            return result
        return HistoricalCandleSourceResult(
            status=self.status,
            reason_code=self.status,
            diagnostics={"symbol": symbol},
        )


class _RecordingWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, obs, result):
        self.calls.append((obs, result))


class _FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows=None):
        self.cursor_obj = _FakeCursor(rows)
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _standalone(obs, candles, asof=ASOF):
    entry = float(obs["reference_price"])
    structural_r = float(abs(obs["invalidation_price"] - entry))
    return evaluate_srr_short_timeout_candle_path(
        signal_time_ms=int(datetime.fromisoformat(obs["signal_time"].replace("Z", "+00:00")).timestamp() * 1000),
        entry=entry,
        stop=entry + 2.0 * structural_r,
        target=entry - 1.5 * structural_r,
        structural_r=structural_r,
        execution_r=2.0 * structural_r,
        max_hold_minutes=120,
        candles=candles,
        evaluation_asof_ms=asof,
        source_provenance={"source_kind": "SYNTHETIC", "source_id": "standalone"},
    )


def test_srr_route_reuses_verified_policy_exactly():
    candles = _clean_candles()
    source = _ArchiveSource(candles)
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(_FakeConn(), source, dry_run=True)
    result = adapter.route(_obs(), ASOF)
    standalone = _standalone(_obs(), candles)
    ignored = {"source_provenance"}
    route_policy = {key: value for key, value in result.policy_result.items() if key not in ignored}
    standalone_policy = {key: value for key, value in standalone.as_dict().items() if key not in ignored}
    assert result.status == standalone.status
    assert result.path_class == standalone.path_class
    assert result.gross_r == standalone.gross_r
    assert result.net_r_normal == standalone.net_r_normal
    assert result.net_r_elevated == standalone.net_r_elevated
    assert route_policy == standalone_policy
    assert result.finalization_eligible is True
    assert result.reason_code == "SRR_SHORT_POLICY_OK"
    assert result.source_status == SOURCE_VALIDATED


def test_history_request_covers_first_eligible_through_cutoff():
    candles = _clean_candles()
    source = _ArchiveSource(candles)
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(_FakeConn(), source)
    adapter.route(_obs(), ASOF)
    symbol, start_ms, end_ms, asof_ms = source.requests[0]
    assert symbol == "AAAUSDT"
    assert start_ms == (SIGNAL_MS // CANDLE_MS) * CANDLE_MS
    assert end_ms == ((CUTOFF - CANDLE_MS) // CANDLE_MS) * CANDLE_MS
    assert asof_ms == ASOF


def test_missing_first_middle_or_final_candle_is_incomplete():
    for label, remove_index in (("first", 0), ("middle", 10), ("final", 23)):
        candles = _clean_candles(24)
        candles.pop(remove_index)
        source = _ArchiveSource(candles)
        adapter = SrrShortExecutionRExpansionProspectiveEvaluator(_FakeConn(), source)
        result = adapter.route(_obs(), ASOF)
        assert result.status == "INCOMPLETE_COVERAGE", label
        assert result.finalization_eligible is False
        assert result.path_class is None


def test_duplicate_identical_candles_are_filtered_but_conflicting_candles_reject():
    base = _clean_candles(2)
    duplicated = base + [base[0]]
    result = validate_source_result(
        HistoricalCandleSourceResult(status=SOURCE_VALIDATED, candles=tuple(duplicated)),
        symbol="AAAUSDT",
        requested_start_ms=SIGNAL_OPEN,
        requested_end_ms=SIGNAL_OPEN + CANDLE_MS,
        required_close_ms=SIGNAL_OPEN + CANDLE_MS,
        evaluation_asof_ms=ASOF,
    )
    assert result.status == SOURCE_VALIDATED
    conflicting = base + [SimpleNamespace(**{**base[0].__dict__, "high": 100.3})]
    result = validate_source_result(
        HistoricalCandleSourceResult(status=SOURCE_VALIDATED, candles=tuple(conflicting)),
        symbol="AAAUSDT",
        requested_start_ms=SIGNAL_OPEN,
        requested_end_ms=SIGNAL_OPEN + CANDLE_MS,
        required_close_ms=SIGNAL_OPEN + CANDLE_MS,
        evaluation_asof_ms=ASOF,
    )
    assert result.status == SOURCE_CONFLICTING_CANDLES


def test_source_status_mapping_is_explicit():
    statuses = [
        (SOURCE_FETCH_ERROR, "SOURCE_UNVERIFIABLE", SOURCE_FETCH_ERROR),
        (SOURCE_INCOMPLETE_COVERAGE, "INCOMPLETE_COVERAGE", SOURCE_INCOMPLETE_COVERAGE),
        (SOURCE_TIMESTAMP_INVALID, "INVALID_INPUT", SOURCE_TIMESTAMP_INVALID),
        (SOURCE_CONFLICTING_CANDLES, "INVALID_INPUT", SOURCE_CONFLICTING_CANDLES),
    ]
    for source_status, expected_status, expected_source in statuses:
        adapter = SrrShortExecutionRExpansionProspectiveEvaluator(
            _FakeConn(), _ArchiveSource(_clean_candles(), status=source_status),
        )
        result = adapter.route(_obs(), ASOF)
        assert result.status == expected_status
        assert result.source_status == expected_source
        assert result.finalization_eligible is False
        assert result.gross_r is None


def test_missing_frozen_tuple_cannot_fall_back_to_mutable_geometry():
    for overrides in (
        {"variant_entry": None},
        {"variant_stop": None},
        {"variant_target": None},
        {"variant_entry": 99.0, "variant_stop": 101.1},
        {"features": "{}"},
        {"features": json.dumps(_features(_frozen_intrabar_policy="ORDER_FIRST"))},
        {"features": json.dumps(_features(_execution_r_abs=0.9))},
    ):
        adapter = SrrShortExecutionRExpansionProspectiveEvaluator(
            _FakeConn(), _ArchiveSource(_clean_candles()),
        )
        result = adapter.route(_obs(**overrides), ASOF)
        assert result.geometry_valid is False
        assert result.finalization_eligible is False
        assert result.gross_r is None
        assert result.path_class is None


def test_invalid_direction_is_blocked():
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(_FakeConn(), _ArchiveSource(_clean_candles()))
    result = adapter.route(_obs(direction="LONG"), ASOF)
    assert result.reason_code == "DIRECTION_NOT_SHORT"
    assert result.geometry_valid is False
    assert result.finalization_eligible is False


def test_same_candle_tp_sl_uses_stop_first_and_economics_once():
    candles = _clean_candles()
    candles[1] = _candle(5, low=99.0, high=101.5)
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(_FakeConn(), _ArchiveSource(candles))
    result = adapter.route(_obs(), ASOF)
    assert result.status == "RESOLVED"
    assert result.path_class == "SL_FIRST"
    assert result.reason_code == "TP_SL_SAME_CANDLE_STOP_FIRST"
    assert result.policy_result["reason_code"] == "TP_SL_SAME_CANDLE_STOP_FIRST"
    assert result.gross_r == pytest.approx(-1.0)
    assert result.cost_r_normal == pytest.approx(0.21)
    assert result.cost_r_elevated == pytest.approx(0.31)
    assert result.net_r_normal == pytest.approx(-1.21)
    assert result.net_r_elevated == pytest.approx(-1.31)
    assert result.finalization_eligible is True


def test_boundary_uncertainty_is_not_finalizable():
    candles = _clean_candles()
    candles[0] = _candle(0, low=99.0, high=101.5)
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(_FakeConn(), _ArchiveSource(candles))
    result = adapter.route(_obs(), ASOF)
    assert result.status == "FIRST_CANDLE_BOUNDARY_UNCERTAIN"
    assert result.finalization_eligible is False
    assert result.gross_r is None


def test_asof_at_4m_vs_5m_prevents_premature_finalization():
    early_asof = SIGNAL_OPEN + 4 * 60_000
    later_asof = SIGNAL_OPEN + 5 * 60_000
    candles = [
        _candle(0, low=99.0),  # Boundary TP: candle begins before the signal.
        _candle(5, low=99.8),
    ]
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(
        _FakeConn(), _ArchiveSource(candles)
    )
    early = adapter.route(_obs(), early_asof)
    later = adapter.route(_obs(), later_asof)
    assert early.source_status == SOURCE_VALIDATED
    assert early.status == "INCOMPLETE_COVERAGE"
    assert early.finalization_eligible is False
    assert early.eligible_candle_count == 0
    assert later.source_status == SOURCE_VALIDATED
    assert later.finalization_eligible is False
    assert later.status in {"FIRST_CANDLE_BOUNDARY_UNCERTAIN", "INCOMPLETE_COVERAGE"}
    assert later.path_class != "TP_FIRST"
    assert later.eligible_candle_count == 0


def test_dry_run_never_writes_outcomes_but_can_retry_after_source_error():
    writer = _RecordingWriter()
    rows = [(1, "AAAUSDT", _dt(SIGNAL_MS), SRR_EXPERIMENT_ID, "SHORT", 100.0, 100.5,
             100.0, 101.0, 99.25, json.dumps(_features()), None, None)]
    conn = _FakeConn(rows)
    source = _ArchiveSource(_clean_candles(), status=SOURCE_FETCH_ERROR)
    adapter = SrrShortExecutionRExpansionProspectiveEvaluator(conn, source, dry_run=True, write_outcomes=writer)
    stats = adapter.run_evaluation_cycle(SRR_EXPERIMENT_ID)
    assert stats["signals_checked"] == 1
    assert stats["finalized"] == 0
    assert stats["source_errors"] == 1
    assert writer.calls == []

    source.status = SOURCE_VALIDATED
    source._candles_by_symbol = {"AAAUSDT": _clean_candles()}
    retry_stats = adapter.run_evaluation_cycle(SRR_EXPERIMENT_ID)
    assert retry_stats["finalized"] == 1
    assert writer.calls == []


def test_prospective_evaluator_routes_only_srr_experiment():
    class Router:
        def __init__(self):
            self.calls = []

        def run_evaluation_cycle(self, experiment_id):
            self.calls.append(experiment_id)
            return {"finalized": 1, "errors": 0}

    started = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    srr_rows = [(1, "AAAUSDT", _dt(SIGNAL_MS), SRR_EXPERIMENT_ID, "SHORT", 100.0, 100.5,
                 100.0, 101.0, 99.25, json.dumps(_features()), None, None)]
    generic_rows = [(2, "AAAUSDT", _dt(SIGNAL_MS), "OTHER_OOS", "SHORT", 100.0, 100.5,
                     None, None, 100.0, 101.0, 99.25, json.dumps(_features()),
                     None, None, None, None, None, False)]

    class CycleConn:
        def __init__(self, rows):
            self.rows = rows
            self.first = True

        def cursor(self):
            return self

        def fetchone(self):
            if self.first:
                self.first = False
                return (started,)
            return None

        def fetchall(self):
            return getattr(self, "current_rows", self.rows)

        def execute(self, sql, params=None):
            self.last_sql = sql
            if "prospective_experiment_direction_state" in sql:
                self.current_rows = []
            elif "prospective_observation" in sql:
                self.current_rows = self.rows
            else:
                self.current_rows = []

        def close(self):
            pass

        def commit(self):
            pass

    router = Router()
    evaluator = ProspectiveOOSEvaluator(CycleConn(srr_rows), None, None, srr_policy_router=router)
    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)
    assert router.calls == [SRR_EXPERIMENT_ID]
    assert stats["finalized"] == 1

    evaluator = ProspectiveOOSEvaluator(CycleConn(generic_rows), None, None, srr_policy_router=router)
    evaluator._get_candles = lambda symbol, from_time, to_time: _clean_candles()
    evaluator.evaluate_observation = lambda obs, candles, now, stats: stats.setdefault("generic", True)
    generic_stats = evaluator.run_evaluation_cycle("OTHER_OOS")
    assert router.calls == [SRR_EXPERIMENT_ID]
    assert generic_stats["generic"] is True


def test_bybit_source_paginates_until_start_is_covered():
    client = MagicMock()
    opens = [SIGNAL_OPEN + i * CANDLE_MS for i in range(24)]

    def fake_public_get(endpoint, **params):
        assert endpoint == "/v5/market/kline"
        assert params["category"] == "linear"
        assert params["interval"] == "5"
        assert params["symbol"] == "AAAUSDT"
        assert params["limit"] == 3
        eligible = [ts for ts in opens if ts <= params["end"]]
        page = eligible[-3:]
        return {
            "result": {
                "list": [
                    [str(ts), "100", "100.2", "99.8", "100", "1"]
                    for ts in reversed(page)
                ]
            }
        }

    client._public_get.side_effect = fake_public_get
    source = BybitHistoricalCandleSource(client, page_limit=3)
    result = source.fetch_5m_candles(
        "AAAUSDT", SIGNAL_OPEN, SIGNAL_OPEN + 23 * CANDLE_MS, ASOF
    )

    assert result.status == SOURCE_VALIDATED, result.diagnostics
    assert len(result.candles) == 24
    assert result.diagnostics["pages"] == 8

    calls = client._public_get.call_args_list
    ends = [call.kwargs["end"] for call in calls]
    assert len(ends) == 8
    assert ends == sorted(ends, reverse=True)
    assert len(set(ends)) == len(ends)
    assert result.candles[0].timestamp == SIGNAL_OPEN
    assert result.candles[-1].timestamp == SIGNAL_OPEN + 23 * CANDLE_MS


def test_asof_never_exposes_future_archive_candles():
    source = _ArchiveSource(_clean_candles())
    end_ms = SIGNAL_OPEN + 23 * CANDLE_MS
    asof_ms = SIGNAL_OPEN + CANDLE_MS
    result = source.fetch_5m_candles("AAAUSDT", SIGNAL_OPEN, end_ms, asof_ms)
    assert result.status == SOURCE_VALIDATED
    assert [c.timestamp for c in result.candles] == [SIGNAL_OPEN]


def test_asof_missing_closed_candle_still_blocks_source():
    candles = _clean_candles()
    candles.pop(1)
    source = _ArchiveSource(candles)
    asof_ms = SIGNAL_OPEN + 2 * CANDLE_MS
    result = source.fetch_5m_candles("AAAUSDT", SIGNAL_OPEN, SIGNAL_OPEN + 23 * CANDLE_MS, asof_ms)
    assert result.status == SOURCE_INCOMPLETE_COVERAGE
    assert result.reason_code == "SOURCE_INTERVAL_INCOMPLETE"

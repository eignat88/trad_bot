from datetime import datetime, timezone
from pathlib import Path

from app.research.prospective_evaluator import _check_tp_sl
from app.research.prospective_observer import ProspectiveOOSObserver


class FakeCursor:
    def __init__(self, row=(42,)):
        self.row = row
        self.sql = None
        self.params = None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return self.row


class Cursor(FakeCursor):
    def cursor(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass


def observe(conn, **overrides):
    kwargs = dict(
        scanner_name="VOLATILITY_COMPRESSION", direction="SHORT",
        symbol="BTCUSDT", signal_time="2026-10-03T00:00:00Z",
        reference_price=100.0, invalidation_price=101.0,
        target_1=98.0, target_2=None, score=0.5,
        features={"bb_width_percentile": 0.4}, parameters={},
        market_regime="LOW_VOL",
    )
    kwargs.update(overrides)
    return ProspectiveOOSObserver(conn, {
        "VC_SHORT_EXECUTION_V1": {
            "scanner_name": "VOLATILITY_COMPRESSION", "direction": "SHORT",
            "threshold": 0.569723, "max_hold_minutes": 120,
            "freeze_ts": "2026-10-02T07:30:21Z",
        },
    }).observe(**kwargs)


def test_freeze_boundary():
    freeze_ts = "2026-10-02T07:30:21Z"

    conn = Cursor()
    assert observe(conn, signal_time="2026-10-02T07:30:20Z") == []
    assert conn.sql is None

    conn = Cursor()
    assert observe(conn, signal_time=freeze_ts) == []
    assert conn.sql is None

    conn = Cursor()
    assert observe(conn, signal_time="2026-10-02T07:30:22Z") == [42]


def test_short_and_filter_only():
    conn = Cursor()
    assert observe(conn)[0] == 42
    assert conn.params[0] == "VC_SHORT_EXECUTION_V1"
    assert conn.params[6] == 100.0
    assert conn.params[7] == 101.0

    conn = Cursor()
    assert observe(conn, direction="LONG") == []
    conn = Cursor()
    assert observe(conn, features={"bb_width_percentile": 0.6}) == []
    conn = Cursor()
    assert observe(conn, features={"bb_width_percentile": None}) == []


def test_invalid_geometry_rejected_and_sl_tp_exact():
    conn = Cursor()
    assert observe(conn, invalidation_price=99.0) == []
    conn = Cursor()
    assert observe(conn, target_1=102.0) == []

    conn = Cursor()
    observe(conn)
    assert conn.params[13] == 100.0
    assert conn.params[14] == 101.0
    assert conn.params[15] == 98.0
    features = __import__("json").loads(conn.params[16])
    assert features["_frozen_structural_r"] == 1.0


def test_dedup_key_and_stop_first_timeout_cost_semantics():
    observer = ProspectiveOOSObserver(None, {})
    key = observer._make_source_key(
        "VOLATILITY_COMPRESSION", "BTCUSDT", "SHORT", "2026-10-03T00:00:00Z",
    )
    assert key == observer._make_source_key(
        "VOLATILITY_COMPRESSION", "BTCUSDT", "SHORT", "2026-10-03T00:00:00Z",
    )

    class C:
        def __init__(self, timestamp=60_000):
            self.timestamp = timestamp
            self.high = 102.0
            self.low = 94.0
            self.close = 100.0

    result = _check_tp_sl(
        [C()], 100.0, 101.0, 98.0, 120,
        datetime.fromtimestamp(0, timezone.utc), True, "STOP_FIRST",
    )
    assert result["ambiguous_intrabar"] is True
    assert result["sl_before_tp"] is True
    assert result["tp_before_sl"] is False


def test_no_paper_live_and_existing_experiment_unchanged():
    source = Path("app/research/prospective_observer.py").read_text()
    assert "VC_SHORT_EXECUTION_V1" in source
    assert "VC_SHORT_BB_WIDTH_V1" in source
    assert "place_order" not in source
    assert "paper_trade" not in source

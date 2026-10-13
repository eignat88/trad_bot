from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v2.candles import ClosedCandle
from app.research.me_r_long_early_mae_v2.outcomes import baseline_outcome
from tools.research.me_rl_v2_exhaustion_filter_execution_reassessment_v1 import (
    classify,
    net_r,
    next_boundary,
)

UTC = timezone.utc


def candle(high, low, close):
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    return ClosedCandle(
        open_at=start,
        close_at=start + timedelta(minutes=5),
        open=100.0,
        high=high,
        low=low,
        close=close,
    )


def test_next_boundary():
    t = datetime(2026, 10, 1, 10, 2, 30, tzinfo=UTC)
    assert next_boundary(t) == datetime(2026, 10, 1, 10, 5, tzinfo=UTC)


def test_next_boundary_exact():
    t = datetime(2026, 10, 1, 10, 5, tzinfo=UTC)
    assert next_boundary(t) == t


def test_filter_boundary():
    assert classify("0.4") == "PASS"
    assert classify("0.40001") == "REJECT"
    assert classify(None) == "MISSING"


def test_stop_first_same_candle():
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    result = baseline_outcome(
        [candle(104, 96, 100)],
        start,
        start + timedelta(minutes=5),
        100,
        97.5,
        103,
    )
    assert result is not None
    assert result.exit_reason == "AMBIGUOUS_STOP_FIRST"
    assert result.ambiguous is True
    assert result.gross_r == -1.0


def test_normal_cost():
    actual = net_r(1.2, 100, 103, 2.5, 0.0005)
    expected = 1.2 - 0.00105 * (100 + 103) / 2.5
    assert abs(actual - expected) < 1e-10


def test_invalid_candle_quality_rejected():
    from tools.research.me_rl_v2_exhaustion_filter_execution_reassessment_v1 import has_invalid_candles

    rows = [
        {"quality_status": "valid"},
        {"quality_status": "invalid"},
    ]
    assert has_invalid_candles(rows) is True


def test_valid_candle_quality_allowed():
    from tools.research.me_rl_v2_exhaustion_filter_execution_reassessment_v1 import has_invalid_candles

    rows = [{"quality_status": "valid"}]
    assert has_invalid_candles(rows) is False


def test_research_snapshot_exists():
    from pathlib import Path
    from tools.research.me_rl_v2_exhaustion_filter_execution_reassessment_v1 import PREFIX

    base = Path("docs/research")
    for suffix in ("_TRADES.csv", "_EXCLUSIONS.csv", "_SUMMARY.json"):
        assert (base / (PREFIX + suffix)).exists()


def test_snapshot_protection_before_database_connection(monkeypatch, tmp_path):
    from tools.research import me_rl_v2_exhaustion_filter_execution_reassessment_v1 as mod
    import sys

    artifact = tmp_path / (mod.PREFIX + "_SUMMARY.json")
    artifact.write_text("DO_NOT_OVERWRITE", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["replay", "--outdir", str(tmp_path)])

    import pytest
    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 2
    assert artifact.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE"


def test_replay_rejects_invalid_candle_before_outcome(monkeypatch, tmp_path):
    import sys
    from tools.research import (
        me_rl_v2_exhaustion_filter_execution_reassessment_v1 as mod,
    )

    class FakeCursor:
        def __init__(self):
            self.call_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            self.call_count += 1

        def fetchall(self):
            if self.call_count == 1:
                return [{
                    "observation_id": 1,
                    "symbol": "BTCUSDT",
                    "signal_time": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
                    "entry_zone_low": 99.8,
                    "invalidation_price": 97.5,
                    "target_1": 103.0,
                    "exhaustion": "0.3",
                    "instrument_id": 1,
                    "is_final": True,
                }]
            return [{
                "open_time": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
                "close_time": datetime(2026, 10, 1, 10, 5, tzinfo=UTC),
                "open": 100,
                "high": 104,
                "low": 96,
                "close": 100,
                "quality_status": "invalid",
            }]

    class FakeConnection:
        def set_session(self, **kwargs):
            assert kwargs["readonly"] is True

        def cursor(self, **kwargs):
            return FakeCursor()

        def close(self):
            pass

    import types

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = lambda **kwargs: FakeConnection()

    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = object

    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay", "--outdir", str(tmp_path)],
    )

    def forbidden_outcome(*args, **kwargs):
        raise AssertionError("INVALID_CANDLE_REACHED_OUTCOME")

    monkeypatch.setattr(mod, "baseline_outcome", forbidden_outcome)

    mod.main()

    import json
    summary = json.loads(
        (tmp_path / (mod.PREFIX + "_SUMMARY.json")).read_text()
    )

    assert summary["source_n"] == 1
    assert summary["full_windows_after_finalization_filter"] == 0
    assert summary["exclusion_counts"]["COVERAGE_INVALID_CANDLE_QUALITY"] == 1


def test_empty_replay_replaces_stale_trades(monkeypatch, tmp_path):
    import csv
    import json
    import sys
    import types

    from tools.research import (
        me_rl_v2_exhaustion_filter_execution_reassessment_v1 as mod,
    )

    class EmptyCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            pass

        def fetchall(self):
            return []

    class FakeConnection:
        def set_session(self, **kwargs):
            assert kwargs["readonly"] is True

        def cursor(self, **kwargs):
            return EmptyCursor()

        def close(self):
            pass

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = lambda **kwargs: FakeConnection()

    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = object

    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)

    trades = tmp_path / (mod.PREFIX + "_TRADES.csv")
    trades.write_text("STALE_TRADE_DATA", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        ["replay", "--outdir", str(tmp_path), "--overwrite"],
    )

    mod.main()

    with trades.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert rows == []
    assert "STALE_TRADE_DATA" not in trades.read_text(encoding="utf-8")
    with trades.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames is not None
        assert reader.fieldnames[0] == "observation_id"

    summary = json.loads(
        (tmp_path / (mod.PREFIX + "_SUMMARY.json")).read_text()
    )

    assert summary["source_n"] == 0
    assert summary["models"]["B_NEXT_OPEN"]["CONTROL"]["n"] == 0


def test_trade_csv_newline_configuration():
    from pathlib import Path

    p = Path(
        "tools/research/me_rl_v2_exhaustion_filter_execution_reassessment_v1.py"
    )
    source = p.read_text(encoding="utf-8")

    bad = 'lineterminator=' + '"' + chr(92) * 2 + 'n"'
    good = 'lineterminator=' + '"' + chr(92) + 'n"'

    assert bad not in source
    assert good in source

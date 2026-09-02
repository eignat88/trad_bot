from pathlib import Path


MART = Path(__file__).parents[1] / "sql" / "mart" / "010_paper_safety_gate.sql"


def test_safety_mart_uses_persisted_safety_events_for_24h_metrics():
    sql = MART.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW mart.paper_safety_metrics AS" in sql
    assert "FROM dds.paper_safety_event" in sql
    for metric in ("severe_stop_gap_24h", "would_block_24h", "actual_blocks_24h"):
        assert metric in sql


def test_safety_mart_exposes_scanner_and_recent_event_analytics():
    sql = MART.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW mart.paper_safety_by_scanner AS" in sql
    assert "CREATE OR REPLACE VIEW mart.paper_safety_recent_events AS" in sql
    assert "scanner_name, symbol, direction" in sql

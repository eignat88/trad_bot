from pathlib import Path


MART = Path(__file__).parents[1] / "sql" / "mart" / "010_paper_safety_gate.sql"


def test_safety_mart_uses_persisted_safety_events_for_24h_metrics():
    sql = MART.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW mart.paper_safety_metrics AS" in sql
    assert "FROM dds.paper_safety_event" in sql
    for metric in ("severe_stop_gap_24h", "would_block_24h", "actual_blocks_24h"):
        assert metric in sql


def test_safety_mart_reads_runtime_mode_from_durable_gate_state_not_events():
    sql = MART.read_text(encoding="utf-8")
    metrics_view = sql.split("CREATE OR REPLACE VIEW mart.paper_safety_metrics AS", 1)[1]

    assert "g.safety_gate_mode" in metrics_view
    assert "gate.safety_gate_mode" in metrics_view
    assert "last_event AS" not in metrics_view
    assert "COALESCE(le.safety_gate_mode" not in metrics_view
    assert "FROM dds.paper_safety_event\n        ORDER BY event_at DESC" not in metrics_view


def test_runtime_mode_and_gate_status_are_independent_mart_fields():
    sql = MART.read_text(encoding="utf-8")

    assert "gate.safety_gate_mode," in sql
    assert "CASE WHEN gate.is_blocked THEN 'BLOCKED' ELSE 'OPEN' END AS gate_status" in sql


def test_safety_mart_exposes_scanner_and_recent_event_analytics():
    sql = MART.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW mart.paper_safety_by_scanner AS" in sql
    assert "CREATE OR REPLACE VIEW mart.paper_safety_recent_events AS" in sql
    assert "scanner_name, symbol, direction" in sql

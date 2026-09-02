from pathlib import Path


SCHEMA = Path(__file__).parents[1] / "app" / "db" / "schema.sql"


def test_paper_trade_setup_is_globally_unique():
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_trade_setup" in sql
    assert "ON dds.paper_trade (setup_id);" in sql
    assert "DROP INDEX IF EXISTS dds.uq_paper_trade_active_per_setup;" in sql


def test_paper_stats_filter_performance_metrics_to_closed_trades():
    sql = SCHEMA.read_text(encoding="utf-8")
    view = sql.split("CREATE OR REPLACE VIEW dds.paper_trade_stats AS", 1)[1]
    for metric in (
        "AVG(pnl_r) FILTER (WHERE status = 'CLOSED')",
        "AVG(pnl_usdt) FILTER (WHERE status = 'CLOSED')",
        "SUM(pnl_usdt) FILTER (WHERE status = 'CLOSED')",
        "AVG(pnl_percent) FILTER (WHERE status = 'CLOSED')",
        "AVG(duration_sec) FILTER (WHERE status = 'CLOSED')",
        "status = 'CLOSED' AND pnl_usdt > 0",
        "status = 'CLOSED' AND pnl_usdt < 0",
    ):
        assert metric in view


def test_paper_safety_gate_state_is_durable_singleton():
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS dds.paper_safety_gate_state" in sql
    assert "gate_id       SMALLINT PRIMARY KEY DEFAULT 1 CHECK (gate_id = 1)" in sql
    assert "is_blocked    BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "blocked_since TIMESTAMPTZ" in sql


def test_paper_safety_events_are_append_only_and_record_enforcement():
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS dds.paper_safety_event" in sql
    assert "would_block              BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "gate_blocked             BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "idx_paper_safety_event_type_at" in sql

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

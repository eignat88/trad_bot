from pathlib import Path


DASHBOARD = (
    Path(__file__).parents[1]
    / "monitoring"
    / "grafana"
    / "dashboards"
    / "system-health-v2.json"
)


def test_paper_safety_dashboard_uses_actual_stop_gap_metric_name():
    dashboard = DASHBOARD.read_text(encoding="utf-8")

    assert "stop_gap_24h" in dashboard
    assert "stop_gap_count_24h" not in dashboard


def test_paper_safety_dashboard_exposes_runtime_mode_and_gate_status():
    dashboard = DASHBOARD.read_text(encoding="utf-8")

    for label in (
        "Safety Mode",
        "Gate Status",
        "STOP_GAP 24h",
        "Severe 24h",
        "Would Block 24h",
        "Actual Blocks 24h",
        "Worst Gap R",
        "Last Severe Event",
        "Balance",
        "Equity",
        "Open Positions",
    ):
        assert label in dashboard

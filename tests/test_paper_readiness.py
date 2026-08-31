from app.config import Settings
from app.paper.readiness import assess_readiness


def test_readiness_requires_positive_two_week_forward_test():
    settings = Settings(
        paper_min_forward_days=14,
        paper_min_closed_trades=30,
        paper_max_drawdown=0.10,
    )

    not_ready = assess_readiness(
        {
            "forward_days": 13.9,
            "closed_trades": 29,
            "net_pnl_usdt": -1.0,
            "max_drawdown": 0.11,
            "avg_r": -0.1,
            "profit_factor": 0.9,
        },
        settings,
    )

    assert not not_ready.eligible
    assert len(not_ready.failed_checks) == 6


def test_readiness_passes_only_when_every_live_gate_metric_passes():
    result = assess_readiness(
        {
            "forward_days": 14,
            "closed_trades": 30,
            "net_pnl_usdt": 10.0,
            "max_drawdown": 0.10,
            "avg_r": 0.1,
            "profit_factor": 1.1,
            "scanner_stats": [],
        },
        Settings(paper_min_forward_days=14, paper_min_closed_trades=30, paper_max_drawdown=0.10),
    )

    assert result.eligible
    assert result.failed_checks == ()

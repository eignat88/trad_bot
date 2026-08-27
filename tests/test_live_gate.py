import pytest

from app.config import Settings
from app.execution.live_exchange import LiveExchange


class FakeRepository:
    def __init__(self, summary):
        self.summary = summary

    def get_paper_forward_summary(self):
        return self.summary


def _live_settings() -> Settings:
    return Settings(
        trading_mode="live",
        live_trading_enabled=True,
        paper_min_forward_days=14,
        paper_min_closed_trades=30,
        paper_max_drawdown=0.10,
    )


def test_live_exchange_rejects_insufficient_paper_forward_test():
    repo = FakeRepository(
        {
            "forward_days": 1,
            "closed_trades": 0,
            "net_pnl_usdt": 0,
            "max_drawdown": 0,
        }
    )

    with pytest.raises(RuntimeError, match="paper forward-test gate"):
        LiveExchange(object(), _live_settings(), repo)


def test_live_exchange_requires_passing_paper_forward_test():
    repo = FakeRepository(
        {
            "forward_days": 14,
            "closed_trades": 30,
            "net_pnl_usdt": 1,
            "max_drawdown": 0.10,
        }
    )

    exchange = LiveExchange(object(), _live_settings(), repo)

    assert exchange.client is not None

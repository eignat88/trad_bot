import pytest

from app.config import Settings
from app.execution.live_exchange import LiveExchange, LiveTradingBlocked


class SpyClient:
    def __init__(self):
        self.calls = []

    def create_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("unsafe live order was sent")


class FakeRepository:
    def get_paper_forward_summary(self):
        return {
            "forward_days": 100,
            "closed_trades": 100,
            "net_pnl_usdt": 1_000,
            "max_drawdown": 0.01,
        }


def _live_settings(**changes) -> Settings:
    values = {"trading_mode": "live", "live_trading_enabled": True}
    values.update(changes)
    return Settings(**values)


def test_live_mode_is_fail_closed_even_with_passing_paper_results():
    client = SpyClient()
    with pytest.raises(LiveTradingBlocked, match="protected order execution is not implemented"):
        LiveExchange(client, _live_settings(), FakeRepository())
    assert client.calls == []


def test_live_mode_requires_explicit_enablement():
    with pytest.raises(LiveTradingBlocked, match="explicit enablement"):
        LiveExchange(SpyClient(), Settings(trading_mode="live"), FakeRepository())


def test_fill_confirmation_requires_exchange_order_id():
    assert not LiveExchange.confirmed_fill({"orderStatus": "Filled"})
    assert LiveExchange.confirmed_fill({"orderId": "abc", "orderStatus": "Filled"})

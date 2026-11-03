import pytest
import requests

from app.config import Settings
from app.exchange.offline_limiter import (
    _map_exception,
    guarded_bybit_attempt,
)


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, "RATE_LIMIT"),
        (400, "ERROR"),
        (500, "ERROR"),
    ],
)
def test_requests_http_error_mapping(status, expected):
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.bybit.com/v5/market/kline"

    with pytest.raises(requests.exceptions.HTTPError) as exc:
        response.raise_for_status()

    assert _map_exception(exc.value) == expected


def test_http429_reaches_shared_limiter(monkeypatch, tmp_path):
    import app.exchange.global_rate_limiter as grl

    records = []

    class Grant:
        granted = True
        grant_id = "test-grant"
        waited_ms = 0

    class SpyLimiter:
        def __init__(self, config):
            assert config.enabled

        def acquire(self, priority, **kwargs):
            return Grant()

        def prepare_send(self, grant_id):
            assert grant_id == "test-grant"
            return True

        def record_result(self, grant_id, priority, outcome):
            records.append((grant_id, priority, outcome))

    monkeypatch.setattr(
        grl, "GlobalBybitRateLimiter", SpyLimiter
    )

    settings = Settings(
        global_bybit_rate_limiter_enabled=True,
        global_limiter_state_path=str(tmp_path / "state.json"),
    )

    response = requests.Response()
    response.status_code = 429
    response.url = "https://api.bybit.com/v5/market/kline"

    with pytest.raises(requests.exceptions.HTTPError):
        with guarded_bybit_attempt(
            "/v5/market/kline", settings=settings
        ):
            response.raise_for_status()

    assert records == [
        ("test-grant", "P3", "RATE_LIMIT")
    ]

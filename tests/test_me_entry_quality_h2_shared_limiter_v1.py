"""H2 shared Bybit limiter: offline P3, no fallback, no burst retry."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.config.settings import Settings
from app.exchange.bybit_client import BybitClient, BybitRateLimitError
from app.research.me_entry_quality_h2_prospective_v1.candle_adapter import (
    BybitClosedCandleProvider, CandleApiBudget, REASON_FETCH_ERROR,
    observation_grid,
)
from app.research.me_entry_quality_h2_prospective_v1.config import H2ProspectiveConfig
from app.research.me_entry_quality_h2_prospective_v1.shared_candle_client import (
    H2SharedBybitClient, build_h2_shared_client,
)


def test_every_h2_public_get_is_forced_to_p3(monkeypatch):
    seen = []
    def fake_parent(self, endpoint, *, retry_priority=None, **kwargs):
        seen.append((endpoint, retry_priority, kwargs))
        return {"result": {"list": []}}
    monkeypatch.setattr(BybitClient, "_public_get", fake_parent)
    client = H2SharedBybitClient(Settings(
        global_bybit_rate_limiter_enabled=True,
        bybit_max_attempts=1, bybit_rate_limit_max_retries=0,
    ))
    client._public_get("/v5/market/kline", retry_priority="P0", symbol="BTCUSDT")
    assert seen == [(
        "/v5/market/kline", "P3", {"symbol": "BTCUSDT"}
    )]


def test_missing_canonical_contract_refuses_client_without_request(tmp_path):
    cfg = H2ProspectiveConfig(
        shared_limiter_contract_path=str(tmp_path / "missing.json")
    )
    with pytest.raises(ValueError):
        build_h2_shared_client(cfg)
    with pytest.raises(ValueError, match="CONTRACT_REQUIRED"):
        build_h2_shared_client(H2ProspectiveConfig())


def test_absolute_canonical_contract_enables_global_limiter(tmp_path):
    state = tmp_path / "state.json"
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps({
        "_comment": "production equivalent budget",
        "global_limiter_state_path": str(state),
        "global_limiter_g_rps": 8.0,
        "global_limiter_burst": 12.0,
    }), encoding="utf-8")
    client = build_h2_shared_client(H2ProspectiveConfig(
        shared_limiter_contract_path=str(contract_path)
    ))
    assert isinstance(client, H2SharedBybitClient)
    assert client.settings.global_bybit_rate_limiter_enabled is True
    assert client.settings.global_limiter_state_path == str(state)
    assert client.settings.bybit_max_attempts == 1
    assert client.settings.bybit_rate_limit_max_retries == 0
    assert not state.exists()  # client creation does not acquire or send


@pytest.mark.parametrize("message", [
    "shared limiter SKIPPED",
    "Bybit retCode=10006 Too many visits. Exceeded the API Rate Limit.",
])
def test_shared_limiter_denial_never_retried(message):
    class Denied:
        count = 0
        def get_klines_range(self, *args, **kwargs):
            self.count += 1
            raise BybitRateLimitError(message, limiter_result="SKIPPED")

    client = Denied()
    budget = CandleApiBudget(
        max_requests_per_cycle=10, max_requests_per_hour=240,
        max_retries=2, request_pause_s=0, retry_backoff_s=0
    )
    provider = BybitClosedCandleProvider(
        budget=budget, enabled=True, client_factory=lambda: client
    )
    budget.start_cycle()
    ms = int(datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    result = provider.fetch_closed_5m(
        symbol="BTCUSDT",
        grid=observation_grid((ms + 60_000) * 1000),
        evaluation_asof_ms=ms + 6 * 300_000,
    )
    assert result.reason == REASON_FETCH_ERROR
    assert not result.ok
    assert result.rows == ()
    assert result.retries == 0
    assert result.rate_limit_skips == 1
    assert client.count == 1


def test_candle_go_without_contract_fails_at_config_boundary():
    from app.research.me_entry_quality_h2_prospective_v1.config import (
        load_config, H2ProspectiveConfigError,
    )
    with pytest.raises(H2ProspectiveConfigError, match="CONTRACT_REQUIRED"):
        load_config({
            "H2_PROSPECTIVE_ENABLED": "1",
            "H2_PROSPECTIVE_CLEAN_ACTIVATION_TS": "2026-11-26T10:00:00Z",
            "H2_PROSPECTIVE_ALLOW_UNFROZEN_POLICIES": "1",
            "H2_CANDLE_API_BUDGET_CONFIRMED": "1",
        })


def test_bybit_10006_without_exception_type_never_retried():
    class Ret10006:
        count = 0
        def get_klines_range(self, *args, **kwargs):
            self.count += 1
            raise RuntimeError("Bybit ret_code=10006 Too many visits")
    client = Ret10006()
    budget = CandleApiBudget(
        max_requests_per_cycle=10, max_requests_per_hour=240,
        max_retries=2, request_pause_s=0, retry_backoff_s=0,
    )
    provider = BybitClosedCandleProvider(
        budget=budget, enabled=True, client_factory=lambda: client
    )
    budget.start_cycle()
    ms = int(datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    outcome = provider.fetch_closed_5m(
        symbol="BTCUSDT",
        grid=observation_grid((ms + 60_000) * 1000),
        evaluation_asof_ms=ms + 6 * 300_000,
    )
    assert not outcome.ok
    assert client.count == 1
    assert outcome.retries == 0


def test_relative_contract_cannot_accidentally_use_isolated_state():
    with pytest.raises(ValueError, match="must be absolute"):
        H2ProspectiveConfig(shared_limiter_contract_path="limiter_contract.v1.json")
